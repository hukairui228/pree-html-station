"""Super-resolution back-ends.

Model choice is tuned for iOS 5-7 era 2D art, which is mostly clean
vector-style cartoon art (Illustrator/Flash exports), painted backgrounds,
and some pixel art - not the photographic PC textures texup targets.
"""
from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import imageops

CACHE_DIR = Path(os.environ.get("IOSREMASTER_MODELS", Path.home() / ".cache" / "ios-retro-remaster" / "models"))


@dataclass(frozen=True)
class ModelSpec:
    url: str
    filename: str
    scale: int
    license: str
    note: str


MODELS: dict[str, ModelSpec] = {
    "anime6b": ModelSpec(
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth",
        "RealESRGAN_x4plus_anime_6B.pth", 4, "BSD-3-Clause",
        "Real-ESRGAN anime 6B: clean flat colour + crisp outlines. Best default for cartoon sprites/UI. 18 MB."),
    "animevideo": ModelSpec(
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth",
        "realesr-animevideov3.pth", 4, "BSD-3-Clause",
        "Real-ESRGAN compact (SRVGG): ~10x faster, slightly softer. For CPU-only machines / big games. 2.5 MB."),
    "x4plus": ModelSpec(
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
        "RealESRGAN_x4plus.pth", 4, "BSD-3-Clause",
        "Real-ESRGAN general: painted / photographic backgrounds, tolerant of JPEG and PVRTC damage. 64 MB."),
    "ultrasharp": ModelSpec(
        "https://huggingface.co/lokCX/4x-Ultrasharp/resolve/main/4x-UltraSharp.pth",
        "4x-UltraSharp.pth", 4, "CC-BY-NC-SA-4.0 (non-commercial)",
        "4x-UltraSharp: most added detail. Non-commercial licence - not for a remake you will sell. 64 MB."),
    "remacri": ModelSpec(
        "https://huggingface.co/FacehugmanIII/4x_foolhardy_Remacri/resolve/main/4x_foolhardy_Remacri.pth",
        "4x_foolhardy_Remacri.pth", 4, "CC-BY-NC-SA-4.0 (non-commercial)",
        "Remacri: natural texture detail for painted backgrounds. Non-commercial licence. 64 MB."),
}

# class -> engine name. "pixel" and "lanczos" are deterministic, everything else a model.
PRESETS: dict[str, dict[str, str]] = {
    # Commercial-safe (BSD models only). Default.
    "faithful": {"cartoon": "anime6b", "ui": "anime6b", "font": "anime6b",
                 "painted": "x4plus", "photo": "x4plus", "pixel": "pixel"},
    # More invented detail; NC-licensed models for painted/photo.
    "detailed": {"cartoon": "anime6b", "ui": "anime6b", "font": "anime6b",
                 "painted": "remacri", "photo": "ultrasharp", "pixel": "pixel"},
    # CPU-friendly.
    "fast": {"cartoon": "animevideo", "ui": "animevideo", "font": "animevideo",
             "painted": "animevideo", "photo": "animevideo", "pixel": "pixel"},
    # No neural network at all (no torch needed).
    "classic": {"cartoon": "lanczos", "ui": "lanczos", "font": "lanczos",
                "painted": "lanczos", "photo": "lanczos", "pixel": "pixel"},
}


def model_path(name: str, download: bool = True) -> Path:
    spec = MODELS[name]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dst = CACHE_DIR / spec.filename
    if not dst.exists():
        if not download:
            raise FileNotFoundError(dst)
        tmp = dst.with_suffix(".part")
        urllib.request.urlretrieve(spec.url, tmp)  # noqa: S310 - fixed URLs above
        tmp.rename(dst)
    return dst


def pick_device(pref: str = "auto") -> str:
    import torch
    if pref != "auto":
        return pref
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class NeuralUpscaler:
    """Tiled spandrel inference (tile/overlap/fp16-fallback approach from texup)."""

    def __init__(self, name: str, device: str = "auto", tile: int = 384):
        import torch
        from spandrel import ModelLoader

        self.name = name
        self.device = pick_device(device)
        desc = ModelLoader().load_from_file(str(model_path(name)))
        self.scale = desc.scale
        self.model = desc.model.eval().to(self.device)
        self.tile = tile
        self.overlap = 12
        self.fp16 = False
        if self.device in ("mps", "cuda"):
            try:
                self.model = self.model.half()
                self.fp16 = True
            except Exception:  # noqa: BLE001
                self.fp16 = False
        if self.device == "cpu":
            torch.set_num_threads(max(1, os.cpu_count() or 1))

    def _infer(self, rgb: np.ndarray) -> np.ndarray:
        import torch
        with torch.inference_mode():
            t = torch.from_numpy(rgb.astype(np.float32) / 255.0).permute(2, 0, 1)[None]
            t = t.to(self.device, dtype=torch.float16 if self.fp16 else torch.float32)
            out = self.model(t).float().clamp(0, 1)[0].permute(1, 2, 0).cpu().numpy()
        if self.fp16 and (np.isnan(out).any() or (rgb.any() and not out.any())):
            self.model = self.model.float()
            self.fp16 = False
            return self._infer(rgb)
        return (out * 255.0 + 0.5).astype(np.uint8)

    def run_rgb(self, rgb: np.ndarray) -> np.ndarray:
        h, w = rgb.shape[:2]
        s, tile, ov = self.scale, self.tile, self.overlap
        if h <= tile and w <= tile:
            return self._infer(rgb)
        out = np.zeros((h * s, w * s, 3), np.uint8)
        for y0 in range(0, h, tile):
            for x0 in range(0, w, tile):
                y1, x1 = min(h, y0 + tile), min(w, x0 + tile)
                ys, xs, ye, xe = max(0, y0 - ov), max(0, x0 - ov), min(h, y1 + ov), min(w, x1 + ov)
                patch = self._infer(rgb[ys:ye, xs:xe])
                oy, ox = (y0 - ys) * s, (x0 - xs) * s
                out[y0 * s:y1 * s, x0 * s:x1 * s] = patch[oy:oy + (y1 - y0) * s, ox:ox + (x1 - x0) * s]
        return out

    def upscale_rgb(self, rgb: np.ndarray, factor: float) -> np.ndarray:
        """Run the x4 network as many times as needed, then land on `factor` with Lanczos/area."""
        h, w = rgb.shape[:2]
        tw, th = max(1, round(w * factor)), max(1, round(h * factor))
        cur = rgb
        while cur.shape[1] < tw:
            cur = self.run_rgb(cur)
        if cur.shape[1] != tw or cur.shape[0] != th:
            mode = "area" if cur.shape[1] > tw else "lanczos"
            cur = imageops.resize(cur, tw, th, mode)
        return cur


class _Cache:
    engines: dict[str, NeuralUpscaler] = {}


def get_engine(name: str, device: str = "auto") -> NeuralUpscaler:
    if name not in _Cache.engines:
        _Cache.engines[name] = NeuralUpscaler(name, device)
    return _Cache.engines[name]


def upscale_rgb(engine: str, rgb: np.ndarray, factor: float, device: str = "auto") -> np.ndarray:
    h, w = rgb.shape[:2]
    tw, th = max(1, round(w * factor)), max(1, round(h * factor))
    if factor == 1:
        return rgb.copy()
    if engine == "lanczos":
        return imageops.unsharp(imageops.resize(rgb, tw, th, "lanczos"), 0.25)
    if engine == "pixel":
        raise ValueError("pixel engine works on RGBA, call imageops.pixel_upscale")
    return get_engine(engine, device).upscale_rgb(rgb, factor)


def content_key(rgba: np.ndarray, *parts: object) -> str:
    h = hashlib.sha1(np.ascontiguousarray(rgba).tobytes())
    h.update(repr((rgba.shape, parts)).encode())
    return h.hexdigest()[:20]
