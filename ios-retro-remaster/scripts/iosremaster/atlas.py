"""Sprite sheets and bitmap fonts of the iOS 5-7 era.

Why this matters: upscaling a whole atlas in one go lets the network "see"
neighbouring frames, which bleeds colour across frame borders and leaves
seams once the engine cuts the frames back out. Instead every frame region
is upscaled on its own (padded with its own edge pixels) and pasted back at
``factor x`` its old position, so the layout and every coordinate in the
descriptor simply scale by the same integer.

Descriptors handled:
  * cocos2d / Zwoptex / TexturePacker .plist, metadata.format 0, 1, 2, 3
  * SpriteKit compiled atlases (Foo.atlasc/Foo.plist, iOS 7)
  * Sparrow / Starling XML atlases (<TextureAtlas imagePath=...>)
  * BMFont .fnt, text and XML flavour (Hiero, Glyph Designer, bmGlyph)
"""
from __future__ import annotations

import plistlib
import re
from dataclasses import dataclass
from pathlib import Path

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class Region:
    name: str
    x: int
    y: int
    w: int      # width as stored in the texture (already swapped when rotated)
    h: int
    rotated: bool = False


@dataclass
class AtlasDesc:
    kind: str                   # cocos-plist, spritekit, bmfont
    path: Path
    textures: list[str]         # texture file names referenced, relative to path.parent
    regions: dict[str, list[Region]]  # texture name -> regions


def _nums(s: str) -> list[float]:
    return [float(x) for x in _NUM.findall(s)]


def _scale_str(s: str, f: float) -> str:
    def rep(m: re.Match) -> str:
        v = float(m.group(0)) * f
        return str(int(round(v))) if abs(v - round(v)) < 1e-6 else f"{v:g}"
    return _NUM.sub(rep, s)


# --------------------------------------------------------------------------- cocos2d plist

_SCALE_KEYS = {
    "frame", "offset", "sourceSize", "sourceColorRect", "textureRect", "spriteOffset",
    "spriteSize", "spriteSourceSize", "x", "y", "width", "height", "offsetX", "offsetY",
    "originalWidth", "originalHeight",
}


def _rect(s: str) -> tuple[int, int, int, int]:
    v = _nums(s)
    return int(v[0]), int(v[1]), int(v[2]), int(v[3])


def parse_cocos_plist(path: Path) -> AtlasDesc | None:
    try:
        d = plistlib.loads(Path(path).read_bytes())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict) or "frames" not in d or not isinstance(d["frames"], dict):
        return None
    meta = d.get("metadata", {}) or {}
    fmt = int(meta.get("format", 0))
    tex = meta.get("realTextureFileName") or meta.get("textureFileName")
    if not tex:
        tex = Path(path).with_suffix(".png").name
    regions: list[Region] = []
    for name, fr in d["frames"].items():
        if not isinstance(fr, dict):
            continue
        rotated = bool(fr.get("rotated") or fr.get("textureRotated"))
        if fmt == 0 or "x" in fr:
            x, y, w, h = int(fr["x"]), int(fr["y"]), int(fr["width"]), int(fr["height"])
        elif fmt == 3 or "textureRect" in fr:
            x, y, _w, _h = _rect(fr["textureRect"])
            sw = _nums(fr.get("spriteSize", "{%d,%d}" % (_w, _h)))
            w, h = int(sw[0]), int(sw[1])
        else:
            x, y, w, h = _rect(fr["frame"])
        if rotated:
            w, h = h, w  # stored on its side in the texture
        regions.append(Region(name, x, y, w, h, rotated))
    return AtlasDesc("cocos-plist", Path(path), [tex], {tex: regions})


def scale_cocos_plist(path: Path, factor: int, rename: dict[str, str]) -> bytes:
    d = plistlib.loads(Path(path).read_bytes())
    for fr in d["frames"].values():
        if not isinstance(fr, dict):
            continue
        for k, v in list(fr.items()):
            if k not in _SCALE_KEYS:
                continue
            if isinstance(v, str):
                fr[k] = _scale_str(v, factor)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                fr[k] = type(v)(v * factor)
    meta = d.get("metadata")
    if isinstance(meta, dict):
        if isinstance(meta.get("size"), str):
            meta["size"] = _scale_str(meta["size"], factor)
        for key in ("textureFileName", "realTextureFileName"):
            if meta.get(key) in rename:
                meta[key] = rename[meta[key]]
    fmt = plistlib.FMT_BINARY if Path(path).read_bytes()[:6] == b"bplist" else plistlib.FMT_XML
    return plistlib.dumps(d, fmt=fmt)


# --------------------------------------------------------------------------- SpriteKit .atlasc

def parse_spritekit(path: Path) -> AtlasDesc | None:
    try:
        d = plistlib.loads(Path(path).read_bytes())
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict) or "images" not in d:
        return None
    textures, regions = [], {}
    for img in d["images"]:
        tex = img.get("path")
        if not tex:
            continue
        textures.append(tex)
        rs = []
        for sub in img.get("subimages", []):
            x, y, w, h = _rect(sub["textureRect"])
            rot = bool(sub.get("textureRotated"))
            if rot:
                w, h = h, w
            rs.append(Region(sub.get("name", "?"), x, y, w, h, rot))
        regions[tex] = rs
    return AtlasDesc("spritekit", Path(path), textures, regions)


def scale_spritekit(path: Path, factor: int, rename: dict[str, str]) -> bytes:
    d = plistlib.loads(Path(path).read_bytes())
    for img in d.get("images", []):
        if isinstance(img.get("size"), str):
            img["size"] = _scale_str(img["size"], factor)
        if img.get("path") in rename:
            img["path"] = rename[img["path"]]
        for sub in img.get("subimages", []):
            for k in ("textureRect", "spriteOffset", "spriteSourceSize"):
                if isinstance(sub.get(k), str):
                    sub[k] = _scale_str(sub[k], factor)
    return plistlib.dumps(d, fmt=plistlib.FMT_BINARY)


# --------------------------------------------------------------------------- BMFont

_ATTR = re.compile(r'(\w+)=("([^"]*)"|[^\s/>]+)')
_FNT_SCALE = {
    "info": {"size", "padding", "spacing", "outline"},
    "common": {"lineHeight", "base", "scaleW", "scaleH"},
    "char": {"x", "y", "width", "height", "xoffset", "yoffset", "xadvance"},
    "kerning": {"amount"},
}


def _fnt_tag(line: str) -> str | None:
    m = re.match(r"\s*<?\s*(\w+)", line)
    return m.group(1) if m else None


def parse_bmfont(path: Path) -> AtlasDesc | None:
    try:
        text = Path(path).read_text("utf-8", errors="replace")
    except OSError:
        return None
    if "char " not in text and "<char " not in text:
        return None
    pages: dict[int, str] = {}
    chars: dict[int, list[Region]] = {}
    for line in text.splitlines():
        tag = _fnt_tag(line)
        attrs = {m.group(1): (m.group(3) if m.group(3) is not None else m.group(2)) for m in _ATTR.finditer(line)}
        if tag == "page":
            pages[int(attrs.get("id", 0))] = attrs.get("file", "")
        elif tag == "char" and "x" in attrs:
            r = Region(attrs.get("id", "?"), int(attrs["x"]), int(attrs["y"]), int(attrs["width"]), int(attrs["height"]))
            if r.w > 0 and r.h > 0:
                chars.setdefault(int(attrs.get("page", 0)), []).append(r)
    if not pages:
        return None
    regions = {pages[p]: chars.get(p, []) for p in pages}
    return AtlasDesc("bmfont", Path(path), list(pages.values()), regions)


def scale_bmfont(path: Path, factor: int, rename: dict[str, str]) -> bytes:
    out = []
    for line in Path(path).read_text("utf-8", errors="replace").splitlines():
        tag = _fnt_tag(line)
        keys = _FNT_SCALE.get(tag or "", set())

        def rep(m: re.Match) -> str:
            k, raw, quoted = m.group(1), m.group(2), m.group(3)
            val = quoted if quoted is not None else raw
            if tag == "page" and k == "file" and val in rename:
                new = rename[val]
                return f'{k}="{new}"' if quoted is not None else f"{k}={new}"
            if k in keys:
                new = _scale_str(val, factor)
                return f'{k}="{new}"' if quoted is not None else f"{k}={new}"
            return m.group(0)
        out.append(_ATTR.sub(rep, line))
    return ("\n".join(out) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- Sparrow XML

_SPARROW_SCALE = {"x", "y", "width", "height", "frameX", "frameY", "frameWidth", "frameHeight"}


def sparrow_frames(path: Path) -> tuple[str, dict[str, dict[str, str]]] | None:
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(Path(path).read_bytes())
    except Exception:  # noqa: BLE001
        return None
    if root.tag != "TextureAtlas" or "imagePath" not in root.attrib:
        return None
    return root.attrib["imagePath"], {st.get("name", "?"): dict(st.attrib) for st in root.iter("SubTexture")}


def parse_sparrow(path: Path) -> AtlasDesc | None:
    got = sparrow_frames(path)
    if not got:
        return None
    tex, frames = got
    regions = []
    for name, a in frames.items():
        rot = a.get("rotated", "false").lower() == "true"
        w, h = int(float(a["width"])), int(float(a["height"]))
        if rot:
            w, h = h, w
        regions.append(Region(name, int(float(a["x"])), int(float(a["y"])), w, h, rot))
    return AtlasDesc("sparrow", Path(path), [tex], {tex: regions})


def scale_sparrow(path: Path, factor: int, rename: dict[str, str]) -> bytes:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(Path(path).read_bytes())
    if root.get("imagePath") in rename:
        root.set("imagePath", rename[root.get("imagePath")])
    for st in root.iter("SubTexture"):
        for k in _SPARROW_SCALE:
            if k in st.attrib:
                st.set(k, _scale_str(st.get(k), factor))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- dispatch

def parse_descriptor(path: Path) -> AtlasDesc | None:
    p = Path(path)
    if p.suffix.lower() == ".fnt":
        return parse_bmfont(p)
    if p.suffix.lower() == ".xml":
        return parse_sparrow(p)
    if p.suffix.lower() == ".plist":
        if p.parent.suffix.lower() == ".atlasc":
            return parse_spritekit(p)
        return parse_cocos_plist(p) or parse_spritekit(p)
    return None


def scale_descriptor(desc: AtlasDesc, factor: int, rename: dict[str, str]) -> bytes:
    if desc.kind == "bmfont":
        return scale_bmfont(desc.path, factor, rename)
    if desc.kind == "spritekit":
        return scale_spritekit(desc.path, factor, rename)
    if desc.kind == "sparrow":
        return scale_sparrow(desc.path, factor, rename)
    return scale_cocos_plist(desc.path, factor, rename)
