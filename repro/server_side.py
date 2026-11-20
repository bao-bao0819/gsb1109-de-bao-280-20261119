"""Information the server has that the CSV response does not expose.

`render_with_output_renderer()` passes every output renderer a `page`
dictionary. For a table page it contains `next`, the cursor for the next page,
and `next_url`. The built-in CSV renderer ignores both. This probe renderer
shows what a streaming renderer is told, which measures the protocol gap: the
server knows *before the first chunk* whether more pages exist, yet the CSV
bytes and headers carry no trace of it.
"""

from __future__ import annotations

from contextlib import contextmanager

from datasette import hookimpl
from datasette.plugins import pm
from datasette.utils.asgi import Response


class _ProbeRenderer:
    __name__ = "IngestReproProbeRenderer"

    @hookimpl
    def register_output_renderer(self):
        def render_probe(columns, rows, page, row_stream):
            async def body():
                yield repr(page.get("next"))

            return Response.stream(body(), content_type="text/plain")

        return [{"extension": "probe", "render": render_probe}]


@contextmanager
def probe_renderer():
    """Register the probe before constructing Datasette (renderers are cached)."""
    pm.register(_ProbeRenderer(), name="ingest_repro_probe")
    try:
        yield
    finally:
        pm.unregister(name="ingest_repro_probe")


async def page_next(ds, path: str) -> str:
    response = await ds.client.get(path)
    return response.text
