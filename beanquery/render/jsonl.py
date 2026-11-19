from ..query_render_jsonl import render_jsonl


def render(desc, rows, file, *, dcontext, **kwargs):
    return render_jsonl(desc, rows, dcontext, file, **kwargs)
