"""Persistent receipts for import and archival runs.

A receipt records, for each downloaded document, whether transactions
were successfully extracted from it and whether the document has been
filed away.  It is meant to be written by automation and read back on
the next run so that a partially completed import can be resumed
without extracting or filing documents twice.

The receipt is a JSON file with a stable, versioned layout::

    {
      "version": 1,
      "files": {
        "/path/to/download/statement.csv": {
          "source": "/path/to/download/statement.csv",
          "sha1": "...",
          "size": 1234,
          "importer": "example.Importer",
          "account": "Assets:Bank",
          "status": "archived",
          "extracted": {
            "at": "2026-11-20T00:00:00Z",
            "output": "output.beancount",
            "entries": 3,
            "duplicates": 0
          },
          "archive": {
            "status": "archived",
            "at": "2026-11-20T00:00:01Z",
            "destination": "/path/to/documents/Assets/Bank/2026-11-19.statement.csv"
          }
        }
      }
    }

``status`` is ``"pending"`` while the extracted entries are awaiting
confirmation and the source document is awaiting archival. Immediately
before a move starts, the record is marked ``"moving"`` together with
the intended destination. It becomes ``"archived"`` once the source
document has been moved successfully. A failed move leaves the record
``"pending"`` and adds an ``archive`` block with status ``"failed"``
and the error message. If a run is interrupted mid-move, the
``"moving"`` record lets the next run compare the source and the
destination and either finish or restart the move. Records are keyed
by source path, so retrying a failed move updates the existing record
instead of creating a duplicate.

"""

import datetime
import json
import os
import tempfile

from typing import Any, Dict, List, Optional


__copyright__ = "Copyright (C) 2026  The beangulp authors"
__license__ = "GNU GPLv2"

# Version of the on-disk receipt format.
VERSION = 1

# Default receipt file name. The command line interface only uses this
# when the caller asks for a receipt without providing an explicit path.
FILENAME = "beangulp-receipt.json"

STATUS_PENDING = "pending"
STATUS_MOVING = "moving"
STATUS_ARCHIVED = "archived"
STATUS_FAILED = "failed"


def _now() -> str:
    """Return the current time as an ISO 8601 UTC timestamp."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")


def _normalize(source: str) -> str:
    """Return the canonical source path used as receipt key."""
    return os.path.abspath(os.path.expanduser(source))


def _atomic_write(filepath: str, data: Dict[str, Any]) -> None:
    """Write JSON data atomically, creating parent directories as needed."""
    directory = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(directory, exist_ok=True)

    prefix = f".{os.path.basename(filepath)}."
    fd, tmpname = tempfile.mkstemp(prefix=prefix, dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as outfile:
            json.dump(data, outfile, indent=2, sort_keys=True)
            outfile.write("\n")
            outfile.flush()
            os.fsync(outfile.fileno())
        os.replace(tmpname, filepath)
    except BaseException:
        try:
            os.unlink(tmpname)
        except FileNotFoundError:
            pass
        raise


class Receipt:
    """A collection of per-document import and archival records."""

    def __init__(self, filepath: Optional[str] = None, files: Optional[Dict] = None):
        self.filepath = filepath
        self.files: Dict[str, Dict[str, Any]] = files if files is not None else {}

    @classmethod
    def load(cls, filepath: str) -> "Receipt":
        """Load a receipt from a JSON file.

        A missing receipt is treated as an empty receipt. This makes the
        first run of an automated import work without a bootstrap step.

        """
        try:
            with open(filepath, encoding="utf-8") as infile:
                data = json.load(infile)
        except FileNotFoundError:
            return cls(filepath=filepath)

        if not isinstance(data, dict):
            raise ValueError(f"Invalid receipt file: {filepath}")
        if data.get("version") != VERSION:
            raise ValueError(
                f"Unsupported receipt version: {data.get('version')!r} in {filepath}"
            )

        files = data.get("files", {})
        if not isinstance(files, dict):
            raise ValueError(f"Invalid receipt file: {filepath}")
        for record in files.values():
            if not isinstance(record, dict):
                raise ValueError(f"Invalid receipt record in {filepath}")

        return cls(filepath=filepath, files=files)

    def save(self, filepath: Optional[str] = None) -> None:
        """Save the receipt atomically."""
        filepath = filepath or self.filepath
        if filepath is None:
            raise ValueError("No receipt file path specified.")
        _atomic_write(
            filepath,
            {
                "version": VERSION,
                "files": self.files,
            },
        )
        self.filepath = filepath

    def get(self, source: str) -> Optional[Dict[str, Any]]:
        """Return the record for a source document, if present."""
        return self.files.get(_normalize(source))

    def pending(self) -> List[Dict[str, Any]]:
        """Return all records whose entries are still pending."""
        return [
            record
            for record in self.files.values()
            if record.get("status") in (STATUS_PENDING, STATUS_MOVING)
        ]

    def archived(self) -> List[Dict[str, Any]]:
        """Return all archived records."""
        return [
            record
            for record in self.files.values()
            if record.get("status") == STATUS_ARCHIVED
        ]

    def record_extracted(
        self,
        source: str,
        sha1: str,
        *,
        size: Optional[int] = None,
        importer: Optional[str] = None,
        account: Optional[str] = None,
        output: Optional[str] = None,
        entries: int = 0,
        duplicates: int = 0,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record that entries were successfully extracted from a document.

        Re-extracting an unchanged document updates the extraction
        details in place and preserves the original extraction
        timestamp. If the document contents changed, its old record is
        replaced, because it is a new document from the receipt's point
        of view.

        """
        source = _normalize(source)
        timestamp = timestamp or _now()
        existing = self.files.get(source)
        unchanged = existing is not None and existing.get("sha1") == sha1

        if unchanged:
            record = existing
            extracted = dict(record.get("extracted") or {})
            extracted["at"] = extracted.get("at") or timestamp
        else:
            record = {
                "source": source,
                "sha1": sha1,
                "status": STATUS_PENDING,
            }
            extracted = {"at": timestamp}

        record.update(
            {
                "source": source,
                "sha1": sha1,
                "size": size,
                "importer": importer,
                "account": account,
            }
        )
        extracted.update(
            {
                "output": output,
                "entries": entries,
                "duplicates": duplicates,
            }
        )
        record["extracted"] = extracted

        # Re-extracting an unchanged document must preserve its
        # archival state, including a failed or interrupted move.
        if not unchanged:
            record["status"] = STATUS_PENDING

        self.files[source] = record
        return record

    def record_moving(
        self,
        source: str,
        destination: str,
        *,
        sha1: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record that a move to destination has started.

        This is written before the filesystem move so that a run
        interrupted mid-move can be reconciled by the next run.

        """
        source = _normalize(source)
        record = self._record(source, sha1)
        record["status"] = STATUS_MOVING
        record["archive"] = {
            "status": STATUS_MOVING,
            "at": timestamp or _now(),
            "destination": destination,
        }
        self.files[source] = record
        return record

    def record_archived(
        self,
        source: str,
        destination: str,
        *,
        sha1: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record that a document was moved to its archival location.

        The extraction record is retained unchanged; only the status
        and archive block are updated.

        """
        source = _normalize(source)
        record = self._record(source, sha1)
        record["status"] = STATUS_ARCHIVED
        record["archive"] = {
            "status": STATUS_ARCHIVED,
            "at": timestamp or _now(),
            "destination": destination,
        }
        self.files[source] = record
        return record

    def record_archive_failed(
        self,
        source: str,
        destination: str,
        error: str,
        *,
        sha1: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a failed archival move.

        The document remains pending so that the next run retries the
        move. Any previous extraction record is preserved.

        """
        source = _normalize(source)
        record = self._record(source, sha1)
        record["status"] = STATUS_PENDING
        record["archive"] = {
            "status": STATUS_FAILED,
            "at": timestamp or _now(),
            "destination": destination,
            "error": error,
        }
        self.files[source] = record
        return record

    def _record(self, source: str, sha1: Optional[str]) -> Dict[str, Any]:
        """Return the existing record, checking its content hash if given."""
        record = self.files.get(source)
        if record is None:
            record = {
                "source": source,
                "sha1": sha1,
                "status": STATUS_PENDING,
            }
        elif sha1 is not None and record.get("sha1") != sha1:
            raise ValueError(f"Document content changed since it was imported: {source}")
        return record
