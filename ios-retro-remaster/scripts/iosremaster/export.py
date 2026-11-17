"""Package upscaled textures for a remake.

  cocos     same folder layout and file names minus the -hd/-ipadhd/@2x suffix,
            descriptors (.plist/.fnt) rewritten with scaled coordinates, .pvr.ccz
            sources re-emitted as .pvr.ccz (PVR3 RGBA8888) so a cocos2d-x port
            can load them unchanged.
  xcassets  Assets.xcassets for a SwiftUI/SpriteKit remake: an .imageset per
            standalone image and a .spriteatlas per sprite sheet (frames cut
            out, untrimmed back to their original logical size), @2x + @3x.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from . import atlas as atl
from . import bundle as bnd
from . import formats as fmt
from . import imageops as ops
from .pipeline import _load_png, load_manifest

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _clean_name(rel: str) -> str:
    stem = bnd.texture_stem(Path(rel).name)[0]
    return bnd.split_variant(stem)[0]


def _density_out(rec: dict) -> float:
    return rec["density"] * rec["factor"]


# --------------------------------------------------------------------------- cocos

def export_cocos(work: Path, dest: Path, keep_ccz: bool = True) -> list[str]:
    m = load_manifest(work)
    root = Path(m["bundle"]["root"])
    dest = Path(dest)
    written = []
    rename_by_desc: dict[str, dict[str, str]] = {}
    factor_by_desc: dict[str, int] = {}
    for r in m["records"]:
        if r.get("status") != "done" or r["container"] == "unity":
            continue
        rel = Path(r["rel"])
        base = _clean_name(r["rel"])
        stem, ext = bnd.texture_stem(rel.name)
        px = _load_png(work / r["out"])
        if ext.lower() in (".pvr.ccz", ".pvr", ".pvr.gz") and keep_ccz:
            name = base + ".pvr.ccz"
            blob = fmt.wrap_ccz(fmt.encode_pvr3_rgba8888(px, premultiply_alpha=r["premultiplied"]))
            (dest / rel.parent).mkdir(parents=True, exist_ok=True)
            (dest / rel.parent / name).write_bytes(blob)
        else:
            name = base + ".png"
            (dest / rel.parent).mkdir(parents=True, exist_ok=True)
            Image.fromarray(px, "RGBA").save(dest / rel.parent / name)
        written.append(str(rel.parent / name))
        own = r.get("atlas")
        if own:
            rename_by_desc.setdefault(own["descriptor"], {})[own["tex_name"]] = name
            factor_by_desc[own["descriptor"]] = r["factor"]
    for drel, rename in rename_by_desc.items():
        d = atl.parse_descriptor(root / drel)
        if not d:
            continue
        blob = atl.scale_descriptor(d, factor_by_desc[drel], rename)
        p = Path(drel)
        stem = bnd.split_variant(p.stem)[0]
        out = dest / p.parent / (stem + p.suffix)
        out.write_bytes(blob)
        written.append(str(out.relative_to(dest)))
    notes = sorted({f"x{r['factor']} -> {_density_out(r):g} px per original iPhone point"
                    for r in m["records"] if r.get("status") == "done"})
    (dest / "EXPORT.md").write_text(
        "# cocos2d export\n\nAll textures and descriptors are one resolution tier, suffixes removed.\n"
        "Densities in this export (1 = original 480x320 iPhone SD art):\n\n" +
        "\n".join(f"- {n}" for n in notes) +
        "\n\nIn cocos2d-x set `director->setContentScaleFactor(<density>)` (or put the folder in a\n"
        "search path whose resolution matches) so sprites keep their original on-screen size.\n", "utf-8")
    return written


# --------------------------------------------------------------------------- xcassets

def _imageset(dir_: Path, name: str, master: np.ndarray, density: float, scales=(2, 3)) -> None:
    """Write name.imageset with @2x/@3x derived from the master (never upscaled)."""
    s = dir_ / f"{name}.imageset"
    s.mkdir(parents=True, exist_ok=True)
    images = []
    h, w = master.shape[:2]
    for sc in scales:
        k = sc / density
        tw, th = max(1, round(w * k)), max(1, round(h * k))
        if k > 1.0001:
            continue  # master too small for this scale: let iOS pick the next one
        arr = master if (tw, th) == (w, h) else ops.resize(master, tw, th, "area")
        fn = f"{name}@{sc}x.png"
        Image.fromarray(np.ascontiguousarray(arr), "RGBA").save(s / fn)
        images.append({"idiom": "universal", "filename": fn, "scale": f"{sc}x"})
    if not images:  # always ship something
        fn = f"{name}@{scales[0]}x.png"
        Image.fromarray(master, "RGBA").save(s / fn)
        images.append({"idiom": "universal", "filename": fn, "scale": f"{scales[0]}x"})
    (s / "Contents.json").write_text(json.dumps({"images": images, "info": {"version": 1, "author": "xcode"}}, indent=2))


def _untrim(frame: np.ndarray, fr: dict, kind: str, f: int, rotated: bool) -> np.ndarray:
    """Restore a trimmed atlas frame to its original (untrimmed) canvas, at xf."""
    if rotated:
        frame = np.rot90(frame, 1)  # TexturePacker stores rotated frames 90 deg clockwise
    h, w = frame.shape[:2]
    nums = lambda k: atl._nums(fr[k]) if isinstance(fr.get(k), str) else None  # noqa: E731
    src = nums("sourceSize") or nums("spriteSourceSize")
    if kind == "bmfont" or not src:
        return frame
    sw, sh = int(src[0] * f), int(src[1] * f)
    color = nums("sourceColorRect")
    if color:
        left, top = int(color[0] * f), int(color[1] * f)
    else:
        off = nums("offset") or nums("spriteOffset") or [0, 0]
        left = int(round((sw - w) / 2 + off[0] * f))
        top = int(round((sh - h) / 2 - off[1] * f))  # cocos offsets are y-up
    if sw < w or sh < h:
        return frame
    left, top = max(0, min(sw - w, left)), max(0, min(sh - h, top))
    canvas = np.zeros((sh, sw, 4), np.uint8)
    canvas[top:top + h, left:left + w] = frame
    return canvas


def _frame_meta(desc_path: Path, own: dict) -> dict[str, dict]:
    """Per-frame trim info in cocos format-2 terms (sourceSize / sourceColorRect / offset)."""
    import plistlib

    if own["kind"] == "sparrow":
        got = atl.sparrow_frames(desc_path)
        out = {}
        for n, a in (got[1] if got else {}).items():
            fw, fh = a.get("frameWidth", a["width"]), a.get("frameHeight", a["height"])
            fx, fy = -float(a.get("frameX", 0)), -float(a.get("frameY", 0))  # frameX/Y are <= 0
            out[n] = {"sourceSize": f"{{{fw},{fh}}}",
                      "sourceColorRect": f"{{{{{fx:g},{fy:g}}},{{{a['width']},{a['height']}}}}}"}
        return out
    raw = plistlib.loads(desc_path.read_bytes())
    if own["kind"] == "spritekit":
        return {s.get("name"): s for img in raw.get("images", []) if img.get("path") == own["tex_name"]
                for s in img.get("subimages", [])}
    return raw.get("frames") if isinstance(raw.get("frames"), dict) else {}


def export_xcassets(work: Path, dest: Path) -> list[str]:
    m = load_manifest(work)
    root = Path(m["bundle"]["root"])
    cat = Path(dest) / "Assets.xcassets"
    cat.mkdir(parents=True, exist_ok=True)
    (cat / "Contents.json").write_text(json.dumps({"info": {"version": 1, "author": "xcode"}}, indent=2))
    written = []
    for r in m["records"]:
        if r.get("status") != "done":
            continue
        master = _load_png(work / r["out"])
        dens = _density_out(r)
        own = r.get("atlas")
        if own and own["kind"] == "spritekit":  # Foo.atlasc/Foo.1@2x.png -> atlas "Foo"
            base = _SAFE.sub("_", Path(own["descriptor"]).parent.stem)
        else:
            base = _SAFE.sub("_", _clean_name(r["rel"]))
        if own and own["kind"] == "bmfont":
            fdir = Path(dest) / "BitmapFonts"
            fdir.mkdir(parents=True, exist_ok=True)
            Image.fromarray(master, "RGBA").save(fdir / f"{base}.png")
            d = atl.parse_descriptor(root / own["descriptor"])
            (fdir / f"{base}.fnt").write_bytes(atl.scale_descriptor(d, r["factor"], {own["tex_name"]: f"{base}.png"}))
            written.append(f"BitmapFonts/{base}.fnt")
            continue
        if own and r.get("regions"):
            frames = _frame_meta(root / own["descriptor"], own)
            d = atl.parse_descriptor(root / own["descriptor"])
            adir = cat / f"{base}.spriteatlas"
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "Contents.json").write_text(json.dumps({"info": {"version": 1, "author": "xcode"}}, indent=2))
            f = r["factor"]
            for reg in d.regions.get(own["tex_name"], []):
                piece = master[reg.y * f:(reg.y + reg.h) * f, reg.x * f:(reg.x + reg.w) * f]
                if piece.size == 0:
                    continue
                full = _untrim(piece, frames.get(reg.name, {}), own["kind"], f, reg.rotated)
                fname = _SAFE.sub("_", Path(reg.name).stem)
                _imageset(adir, fname, full, dens)
            written.append(f"Assets.xcassets/{base}.spriteatlas")
            continue
        _imageset(cat, base, master, dens)
        written.append(f"Assets.xcassets/{base}.imageset")
    return written


def export(work: Path, to: str, dest: Path | None = None) -> list[str]:
    dest = Path(dest or Path(work) / f"export-{to}")
    marker = dest / ".ios-retro-remaster-export"
    if dest.exists() and any(dest.iterdir()):
        if not marker.exists():
            raise FileExistsError(f"{dest} is not empty and was not created by this tool; pick an empty folder")
        shutil.rmtree(dest)  # our own previous export
    dest.mkdir(parents=True, exist_ok=True)
    marker.write_text("created by ios-retro-remaster; safe to delete\n")
    if to == "cocos":
        return export_cocos(work, dest)
    if to == "xcassets":
        return export_xcassets(work, dest)
    raise ValueError(to)
