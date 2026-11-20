from ..query_render import render_json


def render(desc, rows, file, *, dcontext, **kwargs):
    return render_json(desc, rows, dcontext, file, **kwargs)
