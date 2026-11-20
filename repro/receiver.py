"""A deliberately naive ingestion receiver.

It is the thing under test: given one HTTP response, it decides whether the
batch is safe to commit. It only uses facts an HTTP client has - status,
headers, whether the message ended cleanly, and the CSV bytes - and never
looks at Datasette's database.

Two policies:

``trusting``
    The buggy policy: commit whenever the status is 200 and the last parsed row
    is well formed. "The last row looked like complete CSV" is not proof.

``strict``
    Commit only after a clean HTTP message terminator and a CSV body with no
    partial trailing record. This catches truncation at the transport level, but
    it still cannot prove that a cleanly terminated 200 contained every page:
    the response carries no cursor, total or page count.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import parse_csv
from .httpwire import WireResponse


@dataclass
class Commit:
    committed: bool
    reason: str
    rows: int
    # True when a 200 was accepted without proof that all pages arrived
    completeness_unproven: bool = False


def receive(wire: WireResponse, *, policy: str = "trusting", into=None) -> Commit:
    if wire.status != 200:
        return Commit(False, f"status {wire.status}", 0)
    parsed = parse_csv(wire.body)
    if parsed.error:
        return Commit(False, f"csv error: {parsed.error}", 0)
    rows = [tuple(row) for row in parsed.data_rows]
    if policy == "trusting":
        if not rows:
            return Commit(False, "no rows", 0)
        if into is not None:
            into.extend(rows)
        return Commit(True, "status 200 and last row parses", len(rows))
    if policy == "strict":
        if not wire.complete:
            return Commit(
                False,
                "HTTP message did not terminate: " + (wire.client_error or "closed"),
                0,
            )
        if parsed.residual:
            return Commit(False, "trailing partial record", 0)
        if into is not None:
            into.extend(rows)
        return Commit(
            True,
            "clean HTTP end and no partial record (dataset completeness unproven)",
            len(rows),
            completeness_unproven=True,
        )
    raise ValueError(policy)
