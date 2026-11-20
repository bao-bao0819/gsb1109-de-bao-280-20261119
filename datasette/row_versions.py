"""Optimistic concurrency tokens for individual rows.

A row version is an opaque, signed token that identifies one particular
version of a row. Clients obtain it by reading the row (see the
``row_version`` row extra) and may pass it back when updating the row. The
update only succeeds if the row stored under that primary key is still the
same version the client read - any intervening update, or a delete followed
by re-inserting the same key, invalidates the token even when the visible
column values are byte-for-byte identical.

Version state lives in a hidden table inside each writable database rather
than the internal database, so that the version check and the row write
happen in the same SQLite transaction.
"""

from __future__ import annotations

import itsdangerous

from datasette.utils import escape_sqlite

# Hidden (leading underscore) so Datasette's table listings and catalog
# treat it as internal storage rather than user data.
ROW_VERSIONS_TABLE = "_datasette_row_versions"
ROW_VERSIONS_NAMESPACE = "row-version"

# Token payload keys, kept short because they are serialized into every token.
_PAYLOAD_TABLE = "t"
_PAYLOAD_PKS = "k"
_PAYLOAD_VERSION = "v"


class RowVersionError(Exception):
    """Base class for row version token failures."""


class RowVersionTokenInvalid(RowVersionError):
    """The supplied token is malformed, badly signed or for another row."""


class RowVersionConflict(RowVersionError):
    """The row changed (or was deleted and re-created) after the token was issued."""


def _versions_table_exists(conn):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (ROW_VERSIONS_TABLE,),
        ).fetchone()
        is not None
    )


def ensure_row_versions_table(conn):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {escape_sqlite(ROW_VERSIONS_TABLE)} (
            table_name TEXT NOT NULL,
            row_pk TEXT NOT NULL,
            version INTEGER NOT NULL,
            PRIMARY KEY (table_name, row_pk)
        )
        """
    )


def _pk_key(pk_values):
    # NUL cannot appear in TEXT values, so it is an unambiguous separator
    # for the components of a composite primary key.
    return "\x00".join("" if value is None else str(value) for value in pk_values)


def _read_version_row(conn, table_name, pk_values):
    if not _versions_table_exists(conn):
        return None
    return conn.execute(
        f"SELECT version FROM {escape_sqlite(ROW_VERSIONS_TABLE)} "
        "WHERE table_name = ? AND row_pk = ?",
        (table_name, _pk_key(pk_values)),
    ).fetchone()


def issue_row_version(datasette, table_name, pk_values, version):
    """Build the opaque token handed to clients for one row version."""
    payload = {
        _PAYLOAD_TABLE: table_name,
        _PAYLOAD_PKS: [str(value) for value in pk_values],
        _PAYLOAD_VERSION: version,
    }
    return datasette.sign(payload, namespace=ROW_VERSIONS_NAMESPACE)


def decode_row_version(datasette, table_name, pk_values, token):
    """Validate a token's signature and that it targets this table and row.

    Raises RowVersionTokenInvalid on any mismatch. Returns the integer
    version number recorded in the token on success.
    """
    if not isinstance(token, str) or not token:
        raise RowVersionTokenInvalid("Row version token must be a non-empty string")
    try:
        payload = datasette.unsign(token, namespace=ROW_VERSIONS_NAMESPACE)
    except itsdangerous.BadSignature:
        raise RowVersionTokenInvalid("Invalid row version token signature")
    if not isinstance(payload, dict):
        raise RowVersionTokenInvalid("Malformed row version token")
    version = payload.get(_PAYLOAD_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise RowVersionTokenInvalid("Malformed row version token")
    if payload.get(_PAYLOAD_TABLE) != table_name:
        raise RowVersionTokenInvalid(
            "Row version token was issued for a different table"
        )
    token_pks = payload.get(_PAYLOAD_PKS)
    if not isinstance(token_pks, list) or [
        str(value) for value in pk_values
    ] != token_pks:
        raise RowVersionTokenInvalid(
            "Row version token was issued for a different row"
        )
    return version


def record_row_version(conn, table_name, pk_values):
    """Increment (or initialize) the stored version after a row write.

    Runs inside the caller's write transaction and returns the new version
    number. A deleted row keeps its version entry so that a later re-insert
    under the same primary key increments it rather than restarting at 1.
    """
    ensure_row_versions_table(conn)
    pk_key = _pk_key(pk_values)
    existing = conn.execute(
        f"SELECT version FROM {escape_sqlite(ROW_VERSIONS_TABLE)} "
        "WHERE table_name = ? AND row_pk = ?",
        (table_name, pk_key),
    ).fetchone()
    if existing is None:
        new_version = 1
        conn.execute(
            f"INSERT INTO {escape_sqlite(ROW_VERSIONS_TABLE)} "
            "(table_name, row_pk, version) VALUES (?, ?, ?)",
            (table_name, pk_key, new_version),
        )
    else:
        new_version = existing[0] + 1
        conn.execute(
            f"UPDATE {escape_sqlite(ROW_VERSIONS_TABLE)} "
            "SET version = ? WHERE table_name = ? AND row_pk = ?",
            (new_version, table_name, pk_key),
        )
    return new_version


def check_row_version(conn, table_name, pk_values, expected_version):
    """Assert the row is still at expected_version, inside a write transaction."""
    existing = _read_version_row(conn, table_name, pk_values)
    if existing is None or existing[0] != expected_version:
        # No version row means the key was deleted and re-created through a
        # path that did not carry the old state, or never had a version
        # recorded - either way the old token cannot match the new row.
        raise RowVersionConflict("Row version conflict: the row has changed")


def clear_table_row_versions(conn, table_name):
    """Drop every version row for a table that is being dropped or renamed."""
    if not _versions_table_exists(conn):
        return
    conn.execute(
        f"DELETE FROM {escape_sqlite(ROW_VERSIONS_TABLE)} WHERE table_name = ?",
        (table_name,),
    )


async def get_row_version_token(datasette, db, table_name, pk_values):
    """Return a current token for the row, allocating version 1 if needed.

    The caller must already have enforced view-table permission, since the
    token itself discloses that the row exists.
    """

    def lookup(conn):
        return _read_version_row(conn, table_name, pk_values)

    existing = await db.execute_fn(lookup)
    if existing is not None:
        return issue_row_version(datasette, table_name, pk_values, existing[0])
    if not db.is_mutable:
        # Rows in an immutable database can never change, so a stable token
        # without any persisted state is just as good and needs no write.
        return issue_row_version(datasette, table_name, pk_values, 1)

    def allocate(write_conn):
        ensure_row_versions_table(write_conn)
        row = _read_version_row(write_conn, table_name, pk_values)
        if row is None:
            write_conn.execute(
                f"INSERT INTO {escape_sqlite(ROW_VERSIONS_TABLE)} "
                "(table_name, row_pk, version) VALUES (?, ?, 1)",
                (table_name, _pk_key(pk_values)),
            )
            return 1
        return row[0]

    version = await db.execute_write_fn(allocate)
    return issue_row_version(datasette, table_name, pk_values, version)
