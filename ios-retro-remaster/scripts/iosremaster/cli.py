"""Command line: python -m iosremaster <command> ...  (or scripts/remaster)."""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path


def cmd_doctor(a) -> int:
    ok = True
    for mod, why in [("numpy", "core"), ("PIL", "core (Pillow)"), ("cv2", "core (opencv-python-headless)"),
                     ("texture2ddecoder", "PVRTC/ETC decoding"), ("torch", "AI upscaling"),
                     ("spandrel", "AI model loading"), ("UnityPy", "Unity games only (optional)")]:
        try:
            m = importlib.import_module(mod)
            print(f"  ok   {mod:17s} {getattr(m, '__version__', '')}")
        except Exception as e:  # noqa: BLE001
            optional = mod in ("UnityPy", "torch", "spandrel")
            ok = ok and optional
            print(f"  {'warn' if optional else 'FAIL'} {mod:17s} missing ({why}): {type(e).__name__}")
    try:
        from .upscalers import pick_device
        print(f"  device: {pick_device()}")
    except Exception:  # noqa: BLE001
        print("  device: none (torch missing) -> only --preset classic works")
    from .upscalers import CACHE_DIR, MODELS
    for k, s in MODELS.items():
        have = (CACHE_DIR / s.filename).exists()
        print(f"  model {k:11s} {'cached' if have else 'not downloaded'}  [{s.license}]")
    return 0 if ok else 1


def cmd_models(a) -> int:
    from .upscalers import MODELS, PRESETS, model_path
    if a.download:
        for name in (MODELS if a.download == ["all"] else a.download):
            print(f"downloading {name} ...", flush=True)
            print("  ->", model_path(name))
        return 0
    for k, s in MODELS.items():
        print(f"{k:11s} {s.license:34s} {s.note}")
    print("\npresets:")
    for p, mp in PRESETS.items():
        print(f"  {p:9s} " + ", ".join(f"{c}={e}" for c, e in mp.items()))
    return 0


def cmd_scan(a) -> int:
    from .pipeline import scan
    key = [int(x, 16) for x in a.ccz_key.split(",")] if a.ccz_key else None
    m = scan(Path(a.source), Path(a.work), key)
    print((Path(a.work) / "report.md").read_text("utf-8"))
    print(f"manifest: {Path(a.work) / 'manifest.json'}")
    return 0 if m["records"] else 2


def cmd_list(a) -> int:
    from .pipeline import load_manifest
    m = load_manifest(Path(a.work))
    for r in m["records"]:
        if not a.all and not r.get("best"):
            continue
        if a.klass and r["class"] != a.klass:
            continue
        extra = f" atlas:{r['atlas']['regions']}" if r.get("atlas") else ""
        print(f"{r['id']:5d} {r['status']:8s} {r['class']:8s} {r['format']:12s} {r['w']:5d}x{r['h']:<5d} "
              f"d{r['density']:g}{extra}  {r['rel']}")
    return 0


def cmd_set_class(a) -> int:
    from .pipeline import load_manifest, save_manifest
    m = load_manifest(Path(a.work))
    ids = set(a.ids)
    for r in m["records"]:
        if r["id"] in ids:
            r["class"] = a.klass
            r["status"] = "pending"
    save_manifest(Path(a.work), m)
    print(f"{len(ids)} record(s) -> {a.klass}")
    return 0


def cmd_upscale(a) -> int:
    from .pipeline import upscale
    st = upscale(Path(a.work), preset=a.preset, engine=a.engine, scale=a.scale, target=a.target,
                 only=a.only, sample=a.sample, ids=a.ids, all_variants=a.all_variants,
                 clean=a.clean, device=a.device, force=a.force)
    print(json.dumps(st))
    print(f"compare: {Path(a.work) / 'compare' / 'index.html'}")
    return 0 if not st.get("failed") else 3


def cmd_bench(a) -> int:
    import numpy as np

    from . import upscalers as ups
    from .pipeline import load_manifest
    m = load_manifest(Path(a.work)) if a.work else None
    rng = np.random.default_rng(0)
    img = (rng.random((128, 128, 3)) * 255).astype(np.uint8)
    print(f"device: {ups.pick_device(a.device)}")
    secs = {}
    for name in a.models:
        eng = ups.get_engine(name, a.device)
        eng.run_rgb(img[:32, :32])  # warm-up
        t = time.time()
        eng.run_rgb(img)
        secs[name] = (time.time() - t) / (128 * 128 / 1e6)
        print(f"  {name:11s} {secs[name]:7.1f} s per input megapixel")
    if m:
        from .upscalers import PRESETS
        for preset in ("faithful", "fast"):
            tot = 0.0
            for r in m["records"]:
                if not r.get("best"):
                    continue
                e = PRESETS[preset][r["class"]]
                if e in secs:
                    passes = 2 if r["stats"].get("opaque", 1) < 0.999 else 1  # + alpha pass
                    tot += secs[e] * passes * r["w"] * r["h"] / 1e6
            known = all(PRESETS[preset][r["class"]] in secs or PRESETS[preset][r["class"]] in ("pixel", "lanczos")
                        for r in m["records"] if r.get("best"))
            print(f"  estimate preset={preset}: ~{tot / 60:.0f} min" + ("" if known else " (partial: bench more models)"))
    return 0


def cmd_gallery(a) -> int:
    from .pipeline import load_manifest, write_gallery
    print(write_gallery(Path(a.work), load_manifest(Path(a.work))))
    return 0


def cmd_export(a) -> int:
    from .export import export
    out = export(Path(a.work), a.to, Path(a.dest) if a.dest else None)
    for o in out:
        print(" ", o)
    print(f"{len(out)} item(s) written")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="remaster", description="Remaster textures of iOS 5-7 era games")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="check dependencies, device and cached models")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("models", help="list models/presets, or --download NAME... | all")
    s.add_argument("--download", nargs="+")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("scan", help="unpack an .ipa/.app/folder, decode + classify every texture")
    s.add_argument("source")
    s.add_argument("work", help="work directory (manifest, decoded/, out/, compare/)")
    s.add_argument("--ccz-key", help="cocos2d-x CCZp key: four hex parts, e.g. 0x1a2b,0x3c4d,0x5e6f,0x7081")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("list", help="list textures in the manifest")
    s.add_argument("work")
    s.add_argument("--all", action="store_true", help="include superseded low-res variants")
    s.add_argument("--class", dest="klass")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("set-class", help="override the classification of some ids")
    s.add_argument("work")
    s.add_argument("klass", choices=["cartoon", "painted", "photo", "pixel", "ui", "font"])
    s.add_argument("ids", type=int, nargs="+")
    s.set_defaults(fn=cmd_set_class)

    s = sub.add_parser("bench", help="time the models on this machine (+ estimate for a scanned game)")
    s.add_argument("work", nargs="?")
    s.add_argument("--models", nargs="+", default=["anime6b", "x4plus", "animevideo"])
    s.add_argument("--device", default="auto")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("upscale", help="upscale (resumable; cached per frame)")
    s.add_argument("work")
    s.add_argument("--preset", default="faithful", choices=["faithful", "detailed", "fast", "classic"])
    s.add_argument("--engine", help="force one engine for everything: anime6b, x4plus, animevideo, "
                                    "ultrasharp, remacri, pixel, lanczos")
    s.add_argument("--scale", type=int, help="fixed integer factor (default: auto from --target)")
    s.add_argument("--target", type=float, default=4.0,
                   help="target density in pixels per original iPhone point (1=480x320 SD). "
                        "4 = iPad-retina/@3x-phone quality (default), 6 = 4K-ready")
    s.add_argument("--only", nargs="+", help="classes to process")
    s.add_argument("--sample", type=int, help="only the N largest textures per class (preview run)")
    s.add_argument("--ids", type=int, nargs="+")
    s.add_argument("--all-variants", action="store_true", help="also process superseded low-res copies")
    s.add_argument("--clean", type=float, default=1.0, help="artifact cleanup strength 0..2 (0=off)")
    s.add_argument("--device", default="auto", help="auto, cpu, mps, cuda")
    s.add_argument("--force", action="store_true", help="redo textures already done")
    s.set_defaults(fn=cmd_upscale)

    s = sub.add_parser("gallery", help="rebuild compare/index.html")
    s.add_argument("work")
    s.set_defaults(fn=cmd_gallery)

    s = sub.add_parser("export", help="package results: cocos (same layout, rewritten plist/fnt) or xcassets")
    s.add_argument("work")
    s.add_argument("--to", required=True, choices=["cocos", "xcassets"])
    s.add_argument("--dest")
    s.set_defaults(fn=cmd_export)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
