"""Turn a set of pixels into exact, non-overlapping outline contours.

Coordinates are pixel-grid corners with y pointing down, as on screen.
Outer contours run clockwise on screen and holes counter-clockwise, which
becomes the TrueType convention (outer clockwise with y up) once the y axis
is flipped, and fills correctly under SVG's default nonzero rule.
"""


def _edges(pixels):
    edges = {}

    def add(a, b):
        edges.setdefault(a, []).append(b)

    for (x, y) in pixels:
        if (x, y - 1) not in pixels:
            add((x, y), (x + 1, y))
        if (x + 1, y) not in pixels:
            add((x + 1, y), (x + 1, y + 1))
        if (x, y + 1) not in pixels:
            add((x + 1, y + 1), (x, y + 1))
        if (x - 1, y) not in pixels:
            add((x, y + 1), (x, y))
    return edges


def _direction(a, b):
    return (b[0] - a[0], b[1] - a[1])


def contours(pixels):
    """Return a list of closed contours, each a list of corner points."""
    pixels = set(pixels)
    edges = _edges(pixels)
    loops = []
    while edges:
        start = min(edges)
        loop = [start]
        prev, cur = start, edges[start].pop()
        if not edges[start]:
            del edges[start]
        while cur != start:
            loop.append(cur)
            options = edges[cur]
            if len(options) > 1:
                # Two pixels touching only at a corner: turn right, hugging
                # the current pixel, so each pixel keeps its own corner.
                dx, dy = _direction(prev, cur)
                right = (cur[0] - dy, cur[1] + dx)
                nxt = right if right in options else options[0]
                options.remove(nxt)
            else:
                nxt = options.pop()
            if not options:
                del edges[cur]
            prev, cur = cur, nxt
        loops.append(_drop_collinear(loop))
    return loops


def _drop_collinear(loop):
    out = []
    n = len(loop)
    for i, p in enumerate(loop):
        if _direction(loop[i - 1], p) != _direction(p, loop[(i + 1) % n]):
            out.append(p)
    return out


def svg_path(pixels):
    parts = []
    for loop in contours(pixels):
        parts.append("M" + " L".join(f"{x} {y}" for x, y in loop) + " Z")
    return " ".join(parts)
