"""Minimap SVGs: the map image embedded, overlays drawn in map cells through the measured transform.

Standalone by construction (R-E): the image is a data: URI, nothing is fetched.
"""
from __future__ import annotations

import base64
import html
import io

from . import minimap as M

TEAM_COLOURS = ["#3b82f6", "#ef4444", "#22c55e", "#eab308"]
PLAYER_COLOURS = ["#60a5fa", "#f87171", "#34d399", "#fbbf24", "#a78bfa", "#f472b6", "#2dd4bf", "#fb923c"]


def _png_uri(map_name: str, crop) -> tuple[str, tuple]:
    from PIL import Image
    im = Image.open(M.image_path(map_name)).convert("RGB").crop(crop)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"), im.size


class MinimapSVG:
    """Draw in map cells; the SVG shows only the drawn terrain (the black margin cropped)."""

    def __init__(self, map_name: str, width: int = 480, image_ref: str | None = None):
        """`image_ref`: the id of a shared <image> (see `shared_image`) to <use> instead of
        embedding the map again - one copy of the picture per page, however many maps it draws."""
        self.t = M.Transform(map_name)
        box = M.drawn_box(map_name)
        self.crop = box
        self.image_ref = image_ref
        cw, ch = box[2] - box[0], box[3] - box[1]
        self.uri = None if image_ref else _png_uri(map_name, box)[0]
        self.scale = width / cw
        self.w, self.h = width, round(ch * self.scale)
        self.items: list[str] = []

    def xy(self, x, y):
        u, v = self.t(x, y)
        return round((u - self.crop[0]) * self.scale, 1), round((v - self.crop[1]) * self.scale, 1)

    def circle(self, x, y, r, fill, stroke="#000", opacity=0.85, title=None):
        u, v = self.xy(x, y)
        t = "<title>%s</title>" % html.escape(title) if title else ""
        self.items.append('<circle cx="%s" cy="%s" r="%s" fill="%s" stroke="%s" stroke-width="1" '
                          'fill-opacity="%s">%s</circle>' % (u, v, r, fill, stroke, opacity, t))

    def ring(self, x, y, r, stroke, width=2, title=None):
        u, v = self.xy(x, y)
        t = "<title>%s</title>" % html.escape(title) if title else ""
        self.items.append('<circle cx="%s" cy="%s" r="%s" fill="none" stroke="%s" stroke-width="%s">'
                          '%s</circle>' % (u, v, r, stroke, width, t))

    def label(self, x, y, text, fill="#fff", size=11, dx=0, dy=4):
        u, v = self.xy(x, y)
        self.items.append('<text x="%s" y="%s" font-size="%s" text-anchor="middle" fill="%s" '
                          'stroke="#000" stroke-width="2.5" paint-order="stroke" '
                          'font-family="system-ui,sans-serif" font-weight="700">%s</text>'
                          % (u + dx, v + dy, size, fill, html.escape(str(text))))

    def line(self, x0, y0, x1, y1, stroke, width=1.5, dash=None):
        a, b = self.xy(x0, y0)
        c, d = self.xy(x1, y1)
        da = ' stroke-dasharray="%s"' % dash if dash else ""
        self.items.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s"%s '
                          'marker-end="url(#arrow)"/>' % (a, b, c, d, stroke, width, da))

    def svg(self, title: str = "") -> str:
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
                'role="img" aria-label="%s"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" '
                'refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path '
                'd="M 0 0 L 10 5 L 0 10 z" fill="#fff"/></marker></defs>%s%s</svg>'
                % (self.w, self.h, self.w, self.h, html.escape(title), self._picture(),
                   "".join(self.items)))

    def _picture(self) -> str:
        if self.image_ref:
            return ('<g transform="scale(%s)"><use href="#%s"/></g>'
                    % (round(self.scale, 5), self.image_ref))
        return ('<image href="%s" x="0" y="0" width="%d" height="%d" preserveAspectRatio="none"/>'
                % (self.uri, self.w, self.h))


def shared_image(map_name: str, image_id: str) -> str:
    """A hidden SVG holding the cropped minimap once, at its own pixel size, for `<use>`."""
    box = M.drawn_box(map_name)
    uri, (cw, ch) = _png_uri(map_name, box)
    return ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
            '<image id="%s" href="%s" x="0" y="0" width="%d" height="%d" '
            'preserveAspectRatio="none"/></defs></svg>' % (image_id, uri, cw, ch))


def focus_svg(game_map: str, focus_entry: dict, pid: str, colour: str, width: int = 480,
              image_ref: str | None = None) -> str:
    s = MinimapSVG(game_map, width, image_ref)
    for x, y, n in focus_entry["place_centres"]:
        s.ring(x, y, 6 + min(14, n / 4), "#fff", 2, "fight: %d deaths here" % n)
    ds = focus_entry["players"][pid]["dwells"]
    for a, b in zip(ds, ds[1:]):
        s.line(a["x"], a["y"], b["x"], b["y"], colour, 1.2, "3 2")
    for d in ds:
        s.circle(d["x"], d["y"], 7, colour if d["near"] else "#111", "#fff", 0.9,
                 "%d: %.1f s, %s" % (d["n"], d["seconds"], "near the fight" if d["near"] else
                                    "%.0f cells away" % d["dist"]))
        s.label(d["x"], d["y"], d["n"], size=10)
    return s.svg("camera dwells")
