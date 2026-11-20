"""Print the compatibility report for the event ingestion contracts.

Run from the repository root:

    python -m compat.report
"""

from __future__ import annotations

from . import contracts as c

SPEC_DIR = "compat/specs"
OPERATION = "/events"
METHOD = "post"
LOCATION = "paths./events.post"
RESPONSE_LOCATION = "paths./events.post.responses.200"

BULK_OLD = {"marker": "bulk", "count": 1, "items": [{"id": "node-1"}]}
BULK_SINGLE_BRANCH = {"marker": "bulk", "count": 1}
SINGLE_ITEMS = {"marker": "single", "items": [{"id": "node-1"}]}
OLD_ONLY_REQUEST = {
    "header": {"X-Tenant": "tenant-a"},
    "body": {"kind": "single", "payload": "hello"},
}


def _operation(document: c.Document) -> dict:
    return c.operation(document, OPERATION, METHOD)[1]


def _response_schema(document: c.Document) -> object:
    return c.response_schema(document, _operation(document))


def _print(title: str, findings: list[c.Finding]) -> None:
    print(f"\n## {title}")
    print(f"result: {c.summarize(findings).value}")
    for finding in findings:
        if finding.status is c.Status.EQUIVALENT:
            continue
        print(f"- [{finding.status.value}] {finding.location}")
        print(f"  {finding.message}")
        if finding.old is not None or finding.new is not None:
            print(f"  old={finding.old!r}")
            print(f"  new={finding.new!r}")
    print(f"scope: {c.SCOPE_NOTE}")


def main() -> None:
    old = c.load(f"{SPEC_DIR}/old.yaml")
    renamed = c.load(f"{SPEC_DIR}/renamed.yaml")
    narrowed = c.load(f"{SPEC_DIR}/narrowed-bulk.yaml")
    tightened = c.load(f"{SPEC_DIR}/tightened-request.yaml")

    _print(
        "rename circular components and reorder oneOf: request",
        c.compare_request(
            old, renamed, _operation(old), _operation(renamed), LOCATION
        ),
    )
    _print(
        "rename circular components and reorder oneOf: response",
        c.compare_response(
            old,
            renamed,
            _response_schema(old),
            _response_schema(renamed),
            RESPONSE_LOCATION,
            [BULK_SINGLE_BRANCH, SINGLE_ITEMS],
        ),
    )
    _print(
        "remove bulk from the items branch: response counterexample",
        c.compare_response(
            old,
            narrowed,
            _response_schema(old),
            _response_schema(narrowed),
            RESPONSE_LOCATION,
            [BULK_OLD],
        ),
    )
    _print(
        "make region mandatory: request counterexample",
        c.compare_request(
            old,
            tightened,
            _operation(old),
            _operation(tightened),
            LOCATION,
            OLD_ONLY_REQUEST,
        ),
    )


if __name__ == "__main__":
    main()
