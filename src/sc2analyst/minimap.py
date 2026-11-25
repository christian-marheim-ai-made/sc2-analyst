"""The minimap transform (F8, D-5): map cells -> minimap pixels.

MEASURED 2026-10-26 on all 38 maps: the minimap image draws the map's CAMERA BOUNDS (MapInfo, read
with sc2reader's own parser), centred in a square of a power-of-two number of cells per axis - the
smallest power of two at least as large as the camera's extent on that axis; the image's pixels per
cell follow from the image size. Game y grows upward, image y downward.

  Old Sun Temple: camera 26..182 x 24..180 (156 x 156 cells) -> 256 x 256 cells -> a 256 px image,
  the terrain drawn from pixel 50 to 206 on both axes. 10000 Feet: a 128 x 256 image because its
  camera is under 128 cells wide.

Verification (`verify`): the bounding box of the image's non-black pixels equals the camera rectangle
under the transform - exact on 34 maps, within 3 px on 3, 21 px of 1024 on Megastructure (whose
terrain drawing has black margins inside its bounds). The non-square minimaps (128 x 256, 256 x 128)
are covered by the same rule; nothing assumes square.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _pow2(n: float) -> int:
    k = 1
    while k < n:
        k *= 2
    return k


def camera_bounds(map_name: str):
    """left, bottom, right, top - from the map archive's MapInfo, or None when it is absent."""
    import mpyq
    from sc2reader.objects import MapInfo
    src = REPO / "canon/maps/source" / (map_name + ".SC2Map")
    if not src.exists():
        return None
    info = MapInfo(mpyq.MPQArchive(str(src)).read_file("MapInfo"))
    return info.camera_left, info.camera_bottom, info.camera_right, info.camera_top


def image_path(map_name: str) -> Path:
    return REPO / "data/maps/minimaps" / (map_name.replace(" ", "_") + ".png")


def to_pixel(x: float, y: float, cam, iw: int, ih: int):
    cl, cb, cr, ct = cam
    cw, ch = cr - cl, ct - cb
    sx, sy = _pow2(cw), _pow2(ch)
    return (x - cl + (sx - cw) / 2) / sx * iw, (1 - (y - cb + (sy - ch) / 2) / sy) * ih


class Transform:
    def __init__(self, map_name: str):
        from PIL import Image
        self.map = map_name
        self.size = Image.open(image_path(map_name)).size
        self.cam = camera_bounds(map_name)
        if self.cam is None:
            raise LookupError("no map archive for %s" % map_name)

    def __call__(self, x: float, y: float):
        return to_pixel(x, y, self.cam, *self.size)


def drawn_box(map_name: str):
    """The bounding box of the minimap's non-black pixels: where the terrain is drawn."""
    from PIL import Image
    im = Image.open(image_path(map_name)).convert("L").point(lambda v: 255 if v > 12 else 0)
    return im.getbbox()


def verify(map_name: str) -> dict:
    t = Transform(map_name)
    cl, cb, cr, ct = t.cam
    x0, y0 = t(cl, ct)
    x1, y1 = t(cr, cb)
    cam_box = (round(x0), round(y0), round(x1), round(y1))
    drawn = drawn_box(map_name)
    err = max(abs(a - b) for a, b in zip(cam_box, drawn))
    return {"map": map_name, "image": list(t.size), "camera": list(t.cam), "drawn": list(drawn),
            "camera_box": list(cam_box), "error_px": err, "error_share": round(err / t.size[0], 4)}
