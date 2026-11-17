"""Pixel-level helpers: classification, artifact cleanup, alpha handling.

The order of operations used by the pipeline for one image/region:

  straight RGBA (formats.py already undid premultiply/BGRA)
    -> bleed_alpha     colour under transparent pixels = nearest opaque colour
                       (stops dark/white halos: SR models look at those pixels)
    -> clean_source    PVRTC smear / RGBA4444 banding / JPEG blocks
    -> pad             replicate (sprites) or wrap (tileable backgrounds)
    -> upscale RGB     model / pixel-art scaler / lanczos
    -> upscale alpha   separately; hard cut-outs get re-sharpened edges
    -> crop + restore  re-bleed, optional re-premultiply at export time
"""
from __future__ import annotations

import re

import cv2
import numpy as np

CLASSES = ("cartoon", "painted", "photo", "pixel", "ui", "font")

_UI_RX = re.compile(r"(^|[/_\-])(ui|hud|menu|btn|button|icon|title|logo|label|text|score|"
                    r"pause|popup|dialog|panel|bar|frame|arrow|star|coin|digit|number)", re.I)
_FONT_RX = re.compile(r"(font|glyph|digits?|numbers?)", re.I)
_BG_RX = re.compile(r"(^|[/_\-])(bg|background|backdrop|sky|scene|level|world|map|parallax|tile)", re.I)


# --------------------------------------------------------------------------- analysis

def alpha_profile(rgba: np.ndarray) -> dict:
    a = rgba[..., 3]
    n = a.size
    opaque = float((a == 255).sum()) / n
    clear = float((a == 0).sum()) / n
    return {"opaque": opaque, "clear": clear, "partial": 1.0 - opaque - clear,
            "has_alpha": clear + (1.0 - opaque - clear) > 0.001}


def nn_block_factor(rgba: np.ndarray) -> int:
    """Detect art that was nearest-neighbour blown up (fat pixels). Returns k or 1."""
    h, w = rgba.shape[:2]
    for k in (4, 3, 2):
        if h % k or w % k or min(h, w) < 4 * k:
            continue
        small = rgba[::k, ::k]
        if np.array_equal(np.repeat(np.repeat(small, k, 0), k, 1), rgba):
            return k
    return 1


def flatness(rgb: np.ndarray) -> float:
    """Share of pixels sitting in flat colour areas (cartoon / vector-style art)."""
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    mean = cv2.blur(g, (3, 3))
    var = cv2.blur(g * g, (3, 3)) - mean * mean
    return float((var < 4.0).mean())


def unique_colors(rgba: np.ndarray, mask: np.ndarray | None = None, cap: int = 70000) -> int:
    px = rgba.reshape(-1, 4)
    if mask is not None:
        px = px[mask.reshape(-1)]
    if len(px) > 400_000:
        px = px[:: len(px) // 400_000 + 1]
    packed = px[:, 0].astype(np.uint32) << 16 | px[:, 1].astype(np.uint32) << 8 | px[:, 2]
    return min(cap, int(np.unique(packed).size))


def classify(rgba: np.ndarray, name: str, *, is_font: bool = False, container: str = "") -> tuple[str, dict]:
    h, w = rgba.shape[:2]
    ap = alpha_profile(rgba)
    visible = rgba[..., 3] > 16
    if visible.sum() < 16:
        return "ui", {"reason": "almost empty"}
    uc = unique_colors(rgba, visible)
    flat = flatness(rgba[..., :3]) if visible.mean() > 0.05 else 0.0
    fat = nn_block_factor(rgba)
    stats = {"unique_colors": uc, "flatness": round(flat, 3), "fat_pixels": fat, **{k: round(v, 3) for k, v in ap.items() if k != "has_alpha"}}
    hard_alpha = ap["partial"] < 0.01

    if is_font or _FONT_RX.search(name):
        return "font", stats
    if fat > 1 or (uc <= 48 and max(h, w) <= 256 and hard_alpha):
        return "pixel", stats
    opaque_bg = not ap["has_alpha"] and (_BG_RX.search(name) or max(h, w) >= 480)
    if _UI_RX.search(name) and not opaque_bg:
        return "ui", stats
    if not ap["has_alpha"] and (container == "jpeg" or uc > 20000) and flat < 0.45:
        return "photo", stats
    if flat >= 0.55 or uc < 2500:
        return "cartoon", stats
    if opaque_bg:
        return "painted", stats
    return ("cartoon" if flat >= 0.4 else "painted"), stats


def is_tileable(rgba: np.ndarray, thresh: float = 6.0) -> bool:
    """Opaque texture whose opposite edges continue each other."""
    if rgba[..., 3].min() < 250 or min(rgba.shape[:2]) < 16:
        return False
    f = rgba[..., :3].astype(np.float32)
    lr = np.abs(f[:, 0] - f[:, -1]).mean()
    tb = np.abs(f[0] - f[-1]).mean()
    inner = (np.abs(f[:, 0] - f[:, 1]).mean() + np.abs(f[0] - f[1]).mean()) / 2 + 1.0
    return bool(lr < max(thresh, 2.5 * inner) and tb < max(thresh, 2.5 * inner))


# --------------------------------------------------------------------------- cleanup

def bleed_alpha(rgba: np.ndarray, iters: int = 24) -> np.ndarray:
    """Push opaque colours outward into fully transparent pixels."""
    a = rgba[..., 3]
    known = a > 0
    if known.all() or not known.any():
        return rgba
    rgb = rgba[..., :3].astype(np.float32) * known[..., None]
    k = known.astype(np.float32)
    kernel = np.ones((3, 3), np.float32)
    for _ in range(iters):
        if k.min() > 0:
            break
        s = cv2.filter2D(rgb, -1, kernel, borderType=cv2.BORDER_REPLICATE)
        c = cv2.filter2D(k, -1, kernel, borderType=cv2.BORDER_REPLICATE)
        newly = (k == 0) & (c > 0)
        rgb[newly] = s[newly] / c[newly][:, None]
        k[newly] = 1.0
    if k.min() == 0:
        mean = rgb[k > 0].mean(axis=0)
        rgb[k == 0] = mean
    out = rgba.copy()
    out[..., :3] = np.clip(rgb + 0.5, 0, 255).astype(np.uint8)
    return out


def clean_source(rgba: np.ndarray, lossy: str | None, strength: float = 1.0) -> np.ndarray:
    """Remove the artifacts of the source format before they get magnified."""
    if lossy is None or strength <= 0:
        return rgba
    rgb = np.ascontiguousarray(rgba[..., :3])
    if lossy == "quantized16":
        # RGBA4444/565 banding + TexturePacker's Floyd-Steinberg dither: an
        # edge-preserving blur merges the dither back into smooth ramps.
        rgb = cv2.bilateralFilter(rgb, 5, 18 * strength, 3)
    elif lossy == "pvrtc":
        # PVRTC = two low-res colour images + per-pixel modulation: shows up as
        # soft colour bleeding and 4x4/8x4 grain. Mild NLM, keeps line art.
        rgb = cv2.fastNlMeansDenoisingColored(rgb, None, 3 * strength, 3 * strength, 5, 15)
    elif lossy in ("jpeg", "block"):
        rgb = cv2.fastNlMeansDenoisingColored(rgb, None, 4 * strength, 4 * strength, 5, 15)
    out = rgba.copy()
    out[..., :3] = rgb
    if lossy in ("quantized16", "pvrtc"):
        a = rgba[..., 3]
        if np.unique(a).size > 2:  # 4-bit / PVRTC alpha steps -> smooth them a touch
            out[..., 3] = cv2.bilateralFilter(a, 5, 20 * strength, 3)
    return out


def pad(rgba: np.ndarray, n: int, wrap: bool) -> np.ndarray:
    mode = cv2.BORDER_WRAP if wrap else cv2.BORDER_REPLICATE
    if wrap and (n > rgba.shape[0] or n > rgba.shape[1]):
        n = min(rgba.shape[:2]) - 1
    return cv2.copyMakeBorder(rgba, n, n, n, n, mode)


def resize(img: np.ndarray, w: int, h: int, mode: str = "lanczos") -> np.ndarray:
    interp = {"lanczos": cv2.INTER_LANCZOS4, "area": cv2.INTER_AREA,
              "nearest": cv2.INTER_NEAREST, "cubic": cv2.INTER_CUBIC}[mode]
    out = cv2.resize(img, (w, h), interpolation=interp)
    return out


def upscale_alpha(alpha: np.ndarray, w: int, h: int, hard: bool) -> np.ndarray:
    if alpha.min() == alpha.max():
        return np.full((h, w), alpha.flat[0], np.uint8)
    up = resize(alpha, w, h, "cubic").astype(np.float32)
    if hard:
        # Hard cut-out (PVRTC punch-through, 1-bit sprites): rebuild a clean,
        # 1-2px anti-aliased edge instead of a blurry ramp.
        k = max(w / alpha.shape[1], 1.0)
        width = 255.0 / max(1.0, k * 0.75)
        up = (up - 127.5) * (255.0 / width) + 127.5
    return np.clip(up + 0.5, 0, 255).astype(np.uint8)


def sharpen_alpha_edge(a: np.ndarray, factor: float) -> np.ndarray:
    """Tighten a soft upscaled mask to a ~1.3px anti-aliased edge (hard cut-outs)."""
    gain = max(1.0, factor * 0.75)
    up = (a.astype(np.float32) - 127.5) * gain + 127.5
    return np.clip(up + 0.5, 0, 255).astype(np.uint8)


def unsharp(rgb: np.ndarray, amount: float = 0.35, sigma: float = 1.0) -> np.ndarray:
    blur = cv2.GaussianBlur(rgb, (0, 0), sigma)
    return cv2.addWeighted(rgb, 1 + amount, blur, -amount, 0)


# --------------------------------------------------------------------------- pixel art

def scale2x(img: np.ndarray) -> np.ndarray:
    """EPX / Scale2x (AdvMAME2x): only reuses existing colours."""
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    E = p[1:-1, 1:-1]
    B, D, F, H = p[:-2, 1:-1], p[1:-1, :-2], p[1:-1, 2:], p[2:, 1:-1]
    eq = lambda x, y: np.all(x == y, axis=-1)  # noqa: E731
    cond = ~eq(B, H) & ~eq(D, F)
    e0 = np.where((cond & eq(D, B))[..., None], D, E)
    e1 = np.where((cond & eq(B, F))[..., None], F, E)
    e2 = np.where((cond & eq(D, H))[..., None], D, E)
    e3 = np.where((cond & eq(H, F))[..., None], F, E)
    h, w = E.shape[:2]
    out = np.empty((h * 2, w * 2, img.shape[2]), img.dtype)
    out[0::2, 0::2], out[0::2, 1::2], out[1::2, 0::2], out[1::2, 1::2] = e0, e1, e2, e3
    return out


def scale3x(img: np.ndarray) -> np.ndarray:
    p = np.pad(img, ((1, 1), (1, 1), (0, 0)), mode="edge")
    A, B, C = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    D, E, F = p[1:-1, :-2], p[1:-1, 1:-1], p[1:-1, 2:]
    G, H, I = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]  # noqa: E741
    eq = lambda x, y: np.all(x == y, axis=-1)  # noqa: E731
    c = ~eq(B, H) & ~eq(D, F)
    sel = lambda m, x: np.where((c & m)[..., None], x, E)  # noqa: E731
    o = [
        sel(eq(D, B), D),
        sel((eq(D, B) & ~eq(E, C)) | (eq(B, F) & ~eq(E, A)), B),
        sel(eq(B, F), F),
        sel((eq(D, B) & ~eq(E, G)) | (eq(D, H) & ~eq(E, A)), D),
        E,
        sel((eq(B, F) & ~eq(E, I)) | (eq(H, F) & ~eq(E, C)), F),
        sel(eq(D, H), D),
        sel((eq(D, H) & ~eq(E, I)) | (eq(H, F) & ~eq(E, G)), H),
        sel(eq(H, F), F),
    ]
    h, w = E.shape[:2]
    out = np.empty((h * 3, w * 3, img.shape[2]), img.dtype)
    for i, blk in enumerate(o):
        out[i // 3::3, i % 3::3] = blk
    return out


def pixel_upscale(rgba: np.ndarray, factor: int) -> np.ndarray:
    """Integer upscale for pixel art: undo fat pixels first, then ScaleNx."""
    k = nn_block_factor(rgba)
    img = rgba[::k, ::k] if k > 1 else rgba
    target = factor * k
    out = img
    remaining = target
    while remaining > 1:
        if remaining % 3 == 0:
            out, remaining = scale3x(out), remaining // 3
        elif remaining % 2 == 0:
            out, remaining = scale2x(out), remaining // 2
        else:
            out = np.repeat(np.repeat(out, remaining, 0), remaining, 1)
            remaining = 1
    return out
