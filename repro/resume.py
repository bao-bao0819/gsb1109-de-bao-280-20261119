"""Can a client continue a streamed export without missing or duplicating rows?

The answer depends on the cursor that produced the export. A streamed response
carries no cursor at all, so this module also measures what the non-streamed
paginated form exposes, which is the only supported way to resume today.
"""

from __future__ import annotations

import urllib.parse

from .diagnostics import parse_csv
from .httpwire import capture


def next_link(wire) -> str | None:
    header = wire.header_dict.get("link")
    if not header:
        return None
    start = header.find("<")
    end = header.find(">")
    if start == -1 or end == -1:
        return None
    return header[start + 1 : end]


def to_request_path(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.path + ("?" + parsed.query if parsed.query else "")


async def walk_pages(app, first_path: str, limit: int = 100):
    """Follow Link rel=next and return (pages, ids, duplicate_ids)."""
    pages = []
    ids: list[str] = []
    path = first_path
    while path and len(pages) < limit:
        wire = await capture(app, path)
        parsed = parse_csv(wire.body)
        page_ids = [row[0] for row in parsed.data_rows]
        pages.append(
            {
                "path": path,
                "rows": len(page_ids),
                "ids": page_ids,
                "link": next_link(wire),
                "status": wire.status,
                "complete": wire.complete,
            }
        )
        ids.extend(page_ids)
        link = next_link(wire)
        path = to_request_path(link) if link else None
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    return pages, ids, duplicates
