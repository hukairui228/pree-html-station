"""scan -> upscale, with a resumable JSON manifest in the work directory."""
from __future__ import annotations

import html
import json
import os
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from . import atlas as atl
from . import bundle as bnd
from . import formats as fmt
from . import imageops as ops
from . import upscalers as ups

MANIFEST = "manifest.json"
PIPELINE_VERSION = 3  # bump when region processing changes, invalidates cache/
_LOSSLESS_RANK = {None: 0, "quantized16": 1, "jpeg": 2, "block": 2, "pvrtc": 3}


def _save_png(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr, "RGBA").save(path, compress_level=4)


def _load_png(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGBA")).copy()


def load_manifest(work: Path) -> dict:
    return json.loads((Path(work) / MANIFEST).read_text("utf-8"))


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(type(o).__name__)


def save_manifest(work: Path, m: dict) -> None:
    tmp = Path(work) / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(m, indent=1, ensure_ascii=False, default=_json_default), "utf-8")
    os.replace(tmp, Path(work) / MANIFEST)


# =========================================================================== scan

def _unity_textures(root: Path, work: Path) -> list[dict]:
    try:
        import UnityPy
    except ImportError:
        return [{"error": "Unity game: `pip install UnityPy` to extract Texture2D assets"}]
    out = []
    lossy_map = {"PVRTC": "pvrtc", "RGBA4444": "quantized16", "ARGB4444": "quantized16",
                 "RGB565": "quantized16", "ETC": "block", "DXT": "block"}
    for f in sorted(root.rglob("*")):
        if not f.is_file() or not (f.suffix in (".assets", ".unity3d", ".bundle") or f.name.startswith(("level", "mainData"))):
            continue
        try:
            env = UnityPy.load(str(f))
        except Exception:  # noqa: BLE001
            continue
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            try:
                data = obj.read()
                img = data.image
                name = getattr(data, "m_Name", None) or getattr(data, "name", "tex")
                tf = str(getattr(data, "m_TextureFormat", "?")).split(".")[-1]
            except Exception as e:  # noqa: BLE001
                out.append({"error": f"{f.name}#{obj.path_id}: {e}"})
                continue
            if img is None or img.width < 4:
                continue
            rel = f"__unity__/{f.name}/{name}_{obj.path_id}.png"
            dst = work / "decoded" / rel
            _save_png(np.asarray(img.convert("RGBA")), dst)
            lossy = next((v for k, v in lossy_map.items() if tf.upper().startswith(k)), None)
            out.append({"rel": rel, "unity_format": tf, "lossy": lossy, "w": img.width, "h": img.height})
    return out


def scan(src: Path, work: Path, ccz_key: list[int] | None = None) -> dict:
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    info = bnd.open_bundle(Path(src), work)
    root = info.root

    # descriptors first, so textures know whether they are atlases/font pages
    owner: dict[str, dict] = {}
    descriptors = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in (".plist", ".fnt", ".xml") or not p.is_file() or p.name == "Info.plist":
            continue
        d = atl.parse_descriptor(p)
        if not d:
            continue
        drel = str(p.relative_to(root))
        descriptors.append({"rel": drel, "kind": d.kind, "textures": d.textures})
        for tex in d.textures:
            trel = str((p.parent / tex).relative_to(root)) if (p.parent / tex).exists() else None
            if trel is None:  # cocos2d often names "sheet.png" while shipping "sheet.pvr.ccz"
                stem = bnd.texture_stem(tex)[0]
                for cand in p.parent.glob(stem + ".*"):
                    if fmt.is_texture_path(cand) and bnd.texture_stem(cand.name)[0] == stem:
                        trel = str(cand.relative_to(root))
                        break
            if trel:
                owner[trel] = {"descriptor": drel, "kind": d.kind, "tex_name": tex,
                               "regions": len(d.regions.get(tex, []))}

    records = []
    errors = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or not fmt.is_texture_path(p):
            continue
        rel = str(p.relative_to(root))
        if rel.startswith("__MACOSX"):
            continue
        try:
            rgba, ti = fmt.decode_file(p, ccz_key)
        except Exception as e:  # noqa: BLE001
            errors.append({"rel": rel, "error": str(e)})
            continue
        dec_rel = rel + ".png"
        _save_png(rgba, work / "decoded" / dec_rel)
        stem, ext = bnd.texture_stem(p.name)
        base, density, device = bnd.split_variant(stem)
        own = owner.get(rel)
        klass, stats = ops.classify(rgba, rel, is_font=bool(own and own["kind"] == "bmfont"), container=ti.container)
        records.append({
            "id": len(records), "rel": rel, "decoded": dec_rel, "w": ti.width, "h": ti.height,
            "container": ti.container, "format": ti.pixel_format, "wrapper": ti.wrapper,
            "premultiplied": ti.premultiplied, "lossy": ti.lossy, "class": klass, "stats": stats,
            "tileable": ops.is_tileable(rgba) and not own, "density": density, "device": device,
            "group": str(Path(rel).parent / base) + "|" + ("atlas" if own else "img"),
            "atlas": own, "notes": ti.notes, "status": "pending",
        })

    if "Unity" in info.engines:
        for u in _unity_textures(root, work):
            if "error" in u:
                errors.append(u)
                continue
            rgba = _load_png(work / "decoded" / u["rel"])
            klass, stats = ops.classify(rgba, u["rel"])
            records.append({
                "id": len(records), "rel": u["rel"], "decoded": u["rel"], "w": u["w"], "h": u["h"],
                "container": "unity", "format": u["unity_format"], "wrapper": None, "premultiplied": False,
                "lossy": u["lossy"], "class": klass, "stats": stats, "tileable": ops.is_tileable(rgba),
                "density": 1.0, "device": None, "group": u["rel"] + "|img", "atlas": None,
                "notes": [], "status": "pending",
            })

    # pick the best source per resolution-variant group
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[r["group"]].append(r)
    for rs in groups.values():
        best = max(rs, key=lambda r: (r["w"] * r["h"], -_LOSSLESS_RANK.get(r["lossy"], 2)))
        for r in rs:
            r["best"] = r is best
            if r is not best:
                r["superseded_by"] = best["rel"]
        if len(rs) > 1:
            best["variants"] = sorted(r["rel"] for r in rs if r is not best)

    bi = asdict(info)
    bi["root"] = str(info.root)
    bi["app_dir"] = str(info.app_dir) if info.app_dir else None
    m = {"version": 1, "source": str(Path(src).resolve()), "bundle": bi, "descriptors": descriptors,
         "records": records, "errors": errors, "runs": []}
    save_manifest(work, m)
    (work / "report.md").write_text(render_report(m), "utf-8")
    return m


def render_report(m: dict) -> str:
    b = m["bundle"]
    recs = m["records"]
    best = [r for r in recs if r.get("best")]
    L = [f"# 扫描报告：{b.get('name') or Path(m['source']).name}", ""]
    L.append(f"- Bundle ID: `{b.get('bundle_id') or '?'}`  版本: {b.get('version') or '?'}  最低系统: iOS {b.get('min_os') or '?'}")
    fam = {1: "iPhone", 2: "iPad"}
    L.append(f"- 设备: {', '.join(fam.get(x, str(x)) for x in b.get('device_family') or []) or '?'}"
             f"  架构: {', '.join(b.get('archs') or []) or '?'}  FairPlay 加密: {b.get('encrypted')}")
    L.append(f"- 识别到的引擎: {', '.join(b.get('engines') or []) or '未识别'}")
    for n in b.get("notes") or []:
        L.append(f"- ⚠️ {n}")
    L += ["", f"## 贴图：共 {len(recs)} 个文件，去掉低分辨率重复版本后 {len(best)} 个需要处理", ""]
    L.append("| 分类 | 数量 | 默认引擎 (faithful) |")
    L.append("|---|---|---|")
    for k, n in Counter(r["class"] for r in best).most_common():
        L.append(f"| {k} | {n} | {ups.PRESETS['faithful'][k]} |")
    L += ["", "| 源格式 | 数量 | 需要的清理 |", "|---|---|---|"]
    fix = {"pvrtc": "PVRTC 去色块/去糊", "quantized16": "16 位色去色带/去抖动", "jpeg": "去 JPEG 方块",
           "block": "去块压缩痕迹", None: "无"}
    for (f, lossy), n in Counter((r["format"], r["lossy"]) for r in best).most_common():
        L.append(f"| {f} | {n} | {fix.get(lossy, lossy)} |")
    atl_n = sum(1 for r in best if r.get("atlas"))
    if atl_n:
        L += ["", f"- {atl_n} 张是图集/字体页：会按帧单独放大并同步改写 plist/fnt 坐标。"]
    pm = sum(1 for r in recs if r["premultiplied"])
    if pm:
        L.append(f"- {pm} 张存的是预乘 alpha（已还原），放大时不会出现黑边/白边。")
    tl = sum(1 for r in best if r.get("tileable"))
    if tl:
        L.append(f"- {tl} 张可平铺背景：放大时用环绕填充，接缝不会裂开。")
    mp = sum(r["w"] * r["h"] for r in best) / 1e6
    L.append(f"- 需要处理的总像素: {mp:.1f} MP（决定耗时，见 `remaster bench`）")
    if m["errors"]:
        L += ["", f"## 无法解码 ({len(m['errors'])})", ""]
        for e in m["errors"][:30]:
            L.append(f"- `{e.get('rel', '?')}`: {e['error']}")
    return "\n".join(L) + "\n"


# =========================================================================== upscale

def auto_factor(density: float, target: float) -> int:
    return int(max(1, min(8, round(target / max(density, 1.0)))))


def _process_region(px: np.ndarray, rec: dict, factor: int, engine: str, *, clean: float,
                    wrap: bool, device: str, cache: Path, use_cache: bool = True) -> np.ndarray:
    key = ups.content_key(px, PIPELINE_VERSION, engine, factor, clean, wrap, rec["lossy"])
    cf = cache / f"{key}.png"
    if use_cache and cf.exists():
        return _load_png(cf)
    h, w = px.shape[:2]
    if engine == "pixel":
        out = ops.pixel_upscale(px, factor)
        if out.shape[0] != h * factor:  # fat-pixel source: land on the exact size
            out = ops.resize(out, w * factor, h * factor, "nearest")
    else:
        ap = ops.alpha_profile(px)
        pad = 0 if min(h, w) < 3 else min(8, max(2, min(h, w) // 4))
        if ap["has_alpha"]:
            # Trimmed sprites: outside the frame is transparent by definition, so pad
            # with transparency (replicating would smear the outline outward), unless
            # the frame border itself is opaque (a tile inside an atlas).
            border = np.concatenate([px[0, :, 3], px[-1, :, 3], px[:, 0, 3], px[:, -1, 3]])
            if pad and border.min() < 250:
                work = ops.bleed_alpha(np.pad(px, ((pad, pad), (pad, pad), (0, 0))))
            else:
                work = ops.pad(ops.bleed_alpha(px), pad, False) if pad else ops.bleed_alpha(px)
        else:
            work = ops.pad(px, pad, wrap) if pad else px
        padded = ops.clean_source(work, rec["lossy"], clean)
        rgb = ups.upscale_rgb(engine, np.ascontiguousarray(padded[..., :3]), factor, device)
        hard = ap["has_alpha"] and ap["partial"] < 0.01
        if ap["has_alpha"] and engine != "lanczos":
            # Silhouettes matter most on sprites: run the mask through the same
            # network (as grey RGB) so curves come back smooth, not scalloped.
            a3 = np.repeat(padded[..., 3:4], 3, axis=2)
            a = ups.upscale_rgb(engine, np.ascontiguousarray(a3), factor, device).mean(axis=2)
            a = np.clip(a + 0.5, 0, 255).astype(np.uint8)
            a[a >= 250] = 255  # snap network noise at the extremes
            a[a <= 4] = 0
            if hard:
                a = ops.sharpen_alpha_edge(a, factor)
        else:
            a = ops.upscale_alpha(padded[..., 3], rgb.shape[1], rgb.shape[0], hard)
        out = np.dstack([rgb, a])
        if pad:
            p = pad * factor
            out = out[p:p + h * factor, p:p + w * factor]
        out = np.ascontiguousarray(out)
    _save_png(out, cf)
    return out


def _regions_for(rec: dict, root: Path, rgba: np.ndarray) -> tuple[list[atl.Region], str | None]:
    own = rec.get("atlas")
    if not own:
        return [], None
    d = atl.parse_descriptor(root / own["descriptor"])
    if not d:
        return [], "descriptor unreadable, processed as one image"
    regions = d.regions.get(own["tex_name"], [])
    H, W = rgba.shape[:2]
    regions = [r for r in regions if r.w > 0 and r.h > 0 and r.x >= 0 and r.y >= 0 and r.x + r.w <= W and r.y + r.h <= H]
    cover = np.zeros((H, W), bool)
    for r in regions:
        cover[r.y:r.y + r.h, r.x:r.x + r.w] = True
    vis = rgba[..., 3] > 8
    miss = (vis & ~cover).sum() / max(1, vis.sum())
    if not regions or miss > 0.005:
        return [], f"frame rects cover only {100 * (1 - miss):.1f}% of visible pixels: processed as one image"
    return regions, None


def upscale(work: Path, *, preset: str = "faithful", engine: str | None = None, scale: int | None = None,
            target: float = 4.0, only: list[str] | None = None, sample: int | None = None,
            ids: list[int] | None = None, all_variants: bool = False, clean: float = 1.0,
            device: str = "auto", force: bool = False, log=print) -> dict:
    work = Path(work)
    m = load_manifest(work)
    root = Path(m["bundle"]["root"])
    cache = work / "cache"
    recs = [r for r in m["records"] if all_variants or r.get("best")]
    if ids is not None:
        recs = [r for r in m["records"] if r["id"] in ids]
    if only:
        recs = [r for r in recs if r["class"] in only]
    if sample:
        by: dict[str, list] = defaultdict(list)
        for r in sorted(recs, key=lambda r: -r["w"] * r["h"]):
            if len(by[r["class"]]) < sample:
                by[r["class"]].append(r)
        recs = [r for v in by.values() for r in v]
    if not force:
        recs = [r for r in recs if r["status"] != "done" or r.get("preset") != preset]

    total_mp = sum(r["w"] * r["h"] for r in recs) / 1e6
    log(f"{len(recs)} textures, {total_mp:.1f} MP, preset={preset}")
    t0, done_mp, stats = time.time(), 0.0, Counter()
    for i, rec in enumerate(recs, 1):
        try:
            rgba = _load_png(work / "decoded" / rec["decoded"])
            f = scale or auto_factor(rec["density"], target)
            eng = engine or ups.PRESETS[preset][rec["class"]]
            regions, warn = _regions_for(rec, root, rgba)
            if warn:
                rec.setdefault("notes", []).append(warn)
            H, W = rgba.shape[:2]
            if regions:
                out = np.zeros((H * f, W * f, 4), np.uint8)
                for r in regions:
                    piece = _process_region(rgba[r.y:r.y + r.h, r.x:r.x + r.w], rec, f, eng,
                                            clean=clean, wrap=False, device=device, cache=cache,
                                            use_cache=not force)
                    out[r.y * f:(r.y + r.h) * f, r.x * f:(r.x + r.w) * f] = piece
            else:
                out = _process_region(rgba, rec, f, eng, clean=clean, wrap=bool(rec.get("tileable")),
                                      device=device, cache=cache, use_cache=not force)
            dst = work / "out" / (rec["rel"] + ".png")
            _save_png(out, dst)
            rec.update(status="done", factor=f, engine=eng, preset=preset, out=str(dst.relative_to(work)),
                       out_w=out.shape[1], out_h=out.shape[0], regions=len(regions))
            stats["done"] += 1
        except Exception as e:  # noqa: BLE001
            rec.update(status="failed", error=f"{type(e).__name__}: {e}")
            stats["failed"] += 1
            log(f"  ! {rec['rel']}: {e}")
        done_mp += rec["w"] * rec["h"] / 1e6
        el = time.time() - t0
        eta = el / max(done_mp, 1e-6) * (total_mp - done_mp)
        log(f"[{i}/{len(recs)}] {rec['rel']}  {rec.get('engine', '')} x{rec.get('factor', '?')}  "
            f"elapsed {el:.0f}s  eta {eta:.0f}s")
        if i % 5 == 0:
            save_manifest(work, m)
    m["runs"].append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "preset": preset, "engine": engine,
                      "scale": scale, "target": target, "count": len(recs), **stats,
                      "seconds": round(time.time() - t0, 1)})
    save_manifest(work, m)
    write_gallery(work, m)
    return dict(stats)


# =========================================================================== compare gallery

def write_gallery(work: Path, m: dict, max_edge: int = 1400) -> Path:
    gdir = work / "compare"
    gdir.mkdir(exist_ok=True)
    items = []
    for r in m["records"]:
        if r.get("status") != "done":
            continue
        after = _load_png(work / r["out"])
        before = _load_png(work / "decoded" / r["decoded"])
        h, w = after.shape[:2]
        k = min(1.0, max_edge / max(h, w))
        if k < 1:
            after = ops.resize(after, max(1, int(w * k)), max(1, int(h * k)), "area")
        bname, aname = f"{r['id']:05d}_before.png", f"{r['id']:05d}_after.png"
        _save_png(before, gdir / bname)
        _save_png(after, gdir / aname)
        items.append((r, bname, aname, after.shape[1], after.shape[0]))
    cards = []
    for r, b, a, w, h in items:
        meta = (f"{r['class']} · {r['format']} · {r['w']}×{r['h']} → {r['out_w']}×{r['out_h']} · "
                f"{r['engine']} ×{r['factor']}" + (f" · {r['regions']} 帧" if r.get("regions") else ""))
        pix = " pix" if r["class"] == "pixel" else ""
        cards.append(f"""<figure class="card{pix}" style="--ar:{w}/{h}">
  <div class="cmp"><img class="b" src="{b}" alt=""><img class="a" src="{a}" alt="">
  <input type="range" min="0" max="100" value="50" aria-label="对比滑块"></div>
  <figcaption><b>{html.escape(r['rel'])}</b><span>{html.escape(meta)}</span></figcaption></figure>""")
    page = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>贴图对比</title>
<style>
:root{{--bg:#f4f4f2;--fg:#1c1c1c;--muted:#666;--card:#fff;--line:#ddd;--chk1:#e9e9e9;--chk2:#fff}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--muted:#999;--card:#202020;--line:#333;--chk1:#2a2a2a;--chk2:#333}}}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}}
h1{{font-size:18px;margin:0 0 4px}} p{{color:var(--muted);margin:0 0 16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,420px),1fr));gap:16px}}
.card{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}}
.cmp{{position:relative;aspect-ratio:var(--ar);max-height:70vh;margin:auto;
background:repeating-conic-gradient(var(--chk1) 0 25%,var(--chk2) 0 50%) 0 0/16px 16px;--p:50%}}
.cmp img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}}
.cmp .b{{image-rendering:pixelated;clip-path:inset(0 calc(100% - var(--p)) 0 0)}}
.cmp .a{{clip-path:inset(0 0 0 var(--p))}}
.pix .cmp .a{{image-rendering:pixelated}}
.cmp input{{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:ew-resize;margin:0}}
.cmp::after{{content:"";position:absolute;top:0;bottom:0;left:var(--p);width:2px;background:#e33;pointer-events:none}}
figcaption{{padding:8px 10px;display:flex;flex-direction:column;gap:2px;word-break:break-all}}
figcaption span{{color:var(--muted);font-size:12px}}
</style></head><body>
<h1>贴图对比（左：原版，右：重制）</h1><p>拖动图片上的滑块对比。原版按最近邻放大显示，方便看清原始像素。共 {len(items)} 张。</p>
<div class="grid">{''.join(cards)}</div>
<script>document.querySelectorAll('.cmp input').forEach(i=>i.addEventListener('input',e=>e.target.parentElement.style.setProperty('--p',e.target.value+'%')))</script>
</body></html>"""
    out = gdir / "index.html"
    out.write_text(page, "utf-8")
    return out


def clear_outputs(work: Path) -> None:
    for d in ("out", "compare"):
        shutil.rmtree(Path(work) / d, ignore_errors=True)
