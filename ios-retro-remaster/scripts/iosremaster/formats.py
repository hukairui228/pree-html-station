"""Decoders for the texture containers iOS 5-7 era games shipped with.

Everything decodes to a straight-alpha RGBA ``numpy.uint8`` array of shape
(H, W, 4) plus a ``TexInfo`` describing where the pixels came from, because the
source format decides how the pixels must be cleaned before upscaling
(PVRTC smear, RGBA4444 banding, premultiplied alpha, ...).

Covered:
  * CgBI PNG       - Xcode's `pngcrush -iphone` output (BGRA, premultiplied,
                     raw deflate). Every PNG inside a shipped .app is one of
                     these unless the developer turned COMPRESS_PNG_FILES off.
  * PVR v2 / v3    - Apple `texturetool`, PVRTexTool, TexturePacker, cocos2d.
                     PVRTC 2/4bpp, RGBA8888/BGRA8888/4444/5551/565/888, A8,
                     I8, AI88, ETC1/2 and BCn when present.
  * .pvr.ccz       - cocos2d zlib wrapper (CCZ!). Encrypted CCZp is reported.
  * .pvr.gz        - gzip wrapper.
  * raw .pvrtc     - headerless texturetool output; the size is guessed.
  * plain PNG/JPG/TGA/BMP/GIF via Pillow.
"""
from __future__ import annotations

import gzip
import io
import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class DecodeError(Exception):
    pass


@dataclass
class TexInfo:
    container: str              # png, cgbi, pvr2, pvr3, pvrtc-raw, jpeg, ...
    pixel_format: str           # RGBA8888, RGBA4444, PVRTC4_RGBA, ...
    width: int
    height: int
    premultiplied: bool = False  # was the *stored* data premultiplied (already undone)
    wrapper: str | None = None   # ccz, gz
    notes: list[str] = field(default_factory=list)

    @property
    def lossy(self) -> str | None:
        """Which artifact family the source carries, if any."""
        f = self.pixel_format.upper()
        if f.startswith("PVRTC"):
            return "pvrtc"
        if f in ("RGBA4444", "RGBA5551", "RGB565", "RGB555"):
            return "quantized16"
        if f.startswith(("ETC", "BC", "DXT")):
            return "block"
        if self.container == "jpeg":
            return "jpeg"
        return None


# --------------------------------------------------------------------------- utils

def unpremultiply(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32)
    rgb = rgba[..., :3].astype(np.float32)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(a > 0, np.clip(rgb * 255.0 / np.maximum(a, 1), 0, 255), 0)
    res = rgba.copy()
    res[..., :3] = (out + 0.5).astype(np.uint8)
    return res


def premultiply(rgba: np.ndarray) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    res = rgba.copy()
    res[..., :3] = (rgba[..., :3].astype(np.float32) * a + 0.5).astype(np.uint8)
    return res


def looks_premultiplied(rgba: np.ndarray, tolerance: int = 2) -> bool:
    """Premultiplied data can never have a colour channel brighter than alpha.

    Only meaningful when the image actually has partial transparency; an
    image with every pixel at alpha 255 is reported as not premultiplied.
    """
    a = rgba[..., 3].astype(np.int16)
    translucent = a < 250
    if translucent.sum() < 16:
        return False
    mx = rgba[..., :3].max(axis=-1).astype(np.int16)
    return bool(np.all(mx[translucent] <= a[translucent] + tolerance))


def _bgra_to_rgba(buf: bytes, w: int, h: int) -> np.ndarray:
    arr = np.frombuffer(buf, dtype=np.uint8)[: w * h * 4].reshape(h, w, 4)
    return arr[..., [2, 1, 0, 3]].copy()


# --------------------------------------------------------------------------- wrappers

def unwrap(data: bytes, name: str = "") -> tuple[bytes, str | None]:
    """Strip cocos2d CCZ / gzip wrappers. Returns (payload, wrapper)."""
    if data[:4] == b"CCZ!":
        ctype, _ver, _res, length = struct.unpack(">HHII", data[4:16])
        if ctype != 0:
            raise DecodeError(f"CCZ compression type {ctype} unsupported")
        out = zlib.decompress(data[16:])
        if len(out) != length:
            raise DecodeError("CCZ length mismatch")
        return out, "ccz"
    if data[:4] == b"CCZp":
        raise DecodeError(
            "encrypted .pvr.ccz (CCZp): the 128-bit key is set in the game binary via "
            "ZipUtils::setPvrEncryptionKeyPart; supply it with --ccz-key to decode")
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data), "gz"
    return data, None


def decrypt_cczp(data: bytes, key: list[int]) -> bytes:
    """cocos2d-x ZipUtils encryption: XXTEA-like stream over the first 512 u32 blocks.

    Mirrors ZipUtils::decodeEncodedPvr. ``key`` is the four 32-bit parts.
    Returns a plain CCZ! blob.
    """
    if data[:4] != b"CCZp":
        return data
    enclen = (len(data) - 12) // 4
    words = list(struct.unpack(f"<{enclen}I", data[12: 12 + enclen * 4]))
    keylen = 1024
    ks = [0] * keylen
    delta = 0x9E3779B9
    sum_ = 0
    z = ks[keylen - 1]
    rounds = 6
    while rounds:
        sum_ = (sum_ + delta) & 0xFFFFFFFF
        e = (sum_ >> 2) & 3
        for p in range(keylen):
            y = ks[(p + 1) % keylen]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_ ^ y) + (key[(p & 3) ^ e] ^ z))
            ks[p] = (ks[p] + mx) & 0xFFFFFFFF
            z = ks[p]
        rounds -= 1
    b = 0
    i = 0
    seclen = 512
    while i < seclen and i < enclen:
        words[i] ^= ks[b]
        i += 1
        b += 1
        if b >= keylen:
            b = 0
    while i < enclen:  # after the first 512 words only every 64th word is encrypted
        words[i] ^= ks[b]
        i += 64
        b += 1
        if b >= keylen:
            b = 0
    body = struct.pack(f"<{enclen}I", *words) + data[12 + enclen * 4:]
    return b"CCZ!" + data[4:12] + body


# --------------------------------------------------------------------------- CgBI PNG

def is_cgbi(data: bytes) -> bool:
    return data[:8] == PNG_SIG and data[12:16] == b"CgBI"


def _png_chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos: pos + 4])
        ctype = data[pos + 4: pos + 8]
        body = data[pos + 8: pos + 8 + length]
        yield ctype, body
        pos += 12 + length
        if ctype == b"IEND":
            break


def _chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF)


def cgbi_to_png_bytes(data: bytes) -> tuple[bytes, int, int, int]:
    """Rewrite an Apple CgBI PNG into a standard PNG (still BGRA order).

    Trick: re-wrap the raw deflate stream as zlib and let Pillow do the
    scanline unfiltering in C; only the channel swap and the
    un-premultiply happen in numpy afterwards.
    """
    ihdr = None
    idat = bytearray()
    for ctype, body in _png_chunks(data):
        if ctype == b"IHDR":
            ihdr = body
        elif ctype == b"IDAT":
            idat += body
    if ihdr is None:
        raise DecodeError("CgBI PNG without IHDR")
    w, h, depth, ctype_, _c, _f, interlace = struct.unpack(">IIBBBBB", ihdr)
    if depth != 8:
        raise DecodeError(f"CgBI bit depth {depth} unsupported")
    try:
        raw = zlib.decompressobj(-15).decompress(bytes(idat))
    except zlib.error:
        raw = zlib.decompress(bytes(idat))  # some files keep the zlib header
    png = PNG_SIG + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 1)) + _chunk(b"IEND", b"")
    return png, w, h, ctype_


def decode_cgbi(data: bytes) -> tuple[np.ndarray, TexInfo]:
    png, w, h, ctype = cgbi_to_png_bytes(data)
    img = Image.open(io.BytesIO(png))
    img.load()
    if ctype == 6:
        arr = np.asarray(img.convert("RGBA"))[..., [2, 1, 0, 3]].copy()  # BGRA -> RGBA
        arr = unpremultiply(arr)
        info = TexInfo("cgbi", "RGBA8888", w, h, premultiplied=True)
    elif ctype == 2:
        arr = np.asarray(img.convert("RGB"))[..., [2, 1, 0]]
        arr = np.dstack([arr, np.full(arr.shape[:2], 255, np.uint8)])
        info = TexInfo("cgbi", "RGB888", w, h)
    else:
        arr = np.asarray(img.convert("RGBA")).copy()
        info = TexInfo("cgbi", f"PNG_ct{ctype}", w, h)
    info.notes.append("Xcode-crushed PNG (CgBI): BGRA + premultiplied alpha undone")
    return arr, info


# --------------------------------------------------------------------------- PVR

_PVR2_FORMATS = {
    0x10: "RGBA4444", 0x11: "RGBA5551", 0x12: "RGBA8888", 0x13: "RGB565",
    0x14: "RGB555", 0x15: "RGB888", 0x16: "I8", 0x17: "AI88",
    0x18: "PVRTC2_RGBA", 0x19: "PVRTC4_RGBA", 0x1A: "BGRA8888", 0x1B: "A8",
    0x36: "ETC1",
    # MGL legacy ids that Apple/TexturePacker files occasionally carry
    0x00: "ARGB4444", 0x01: "ARGB1555", 0x02: "RGB565", 0x04: "RGB888",
    0x0C: "PVRTC2_RGBA", 0x0D: "PVRTC4_RGBA",
}
_PVR2_FLAG_VFLIP = 0x10000

_PVR3_COMPRESSED = {
    0: "PVRTC2_RGB", 1: "PVRTC2_RGBA", 2: "PVRTC4_RGB", 3: "PVRTC4_RGBA",
    6: "ETC1", 7: "BC1", 9: "BC2", 11: "BC3", 22: "ETC2_RGB", 23: "ETC2_RGBA",
    24: "ETC2_RGB_A1",
}
_PVR3_FLAG_PREMULT = 0x02


def _unpack16(buf: bytes, w: int, h: int, bits: tuple[int, ...], order: str) -> np.ndarray:
    """Packed 16-bit pixels, first channel in the most significant bits."""
    v = np.frombuffer(buf, dtype="<u2")[: w * h].reshape(h, w).astype(np.uint32)
    out = np.zeros((h, w, 4), np.uint8)
    out[..., 3] = 255
    shift = 16
    for ch, nb in zip(order, bits):
        shift -= nb
        val = (v >> shift) & ((1 << nb) - 1)
        val8 = (val * 255 + ((1 << nb) - 1) // 2) // ((1 << nb) - 1)
        idx = "rgba".index(ch)
        out[..., idx] = val8.astype(np.uint8)
    return out


def _decode_payload(fmt: str, buf: bytes, w: int, h: int) -> np.ndarray:
    import texture2ddecoder as t2d

    if fmt.startswith("PVRTC"):
        is2 = fmt.startswith("PVRTC2")
        rgba = _bgra_to_rgba(t2d.decode_pvrtc(buf, w, h, is2), w, h)
        if fmt.endswith("_RGB"):
            rgba[..., 3] = 255
        return rgba
    if fmt == "ETC1":
        return _bgra_to_rgba(t2d.decode_etc1(buf, w, h), w, h)
    if fmt == "ETC2_RGB":
        return _bgra_to_rgba(t2d.decode_etc2(buf, w, h), w, h)
    if fmt == "ETC2_RGBA":
        return _bgra_to_rgba(t2d.decode_etc2a8(buf, w, h), w, h)
    if fmt == "ETC2_RGB_A1":
        return _bgra_to_rgba(t2d.decode_etc2a1(buf, w, h), w, h)
    if fmt == "BC1":
        return _bgra_to_rgba(t2d.decode_bc1(buf, w, h), w, h)
    if fmt == "BC3":
        return _bgra_to_rgba(t2d.decode_bc3(buf, w, h), w, h)
    n = w * h
    if fmt == "RGBA8888":
        return np.frombuffer(buf, np.uint8)[: n * 4].reshape(h, w, 4).copy()
    if fmt == "BGRA8888":
        return np.frombuffer(buf, np.uint8)[: n * 4].reshape(h, w, 4)[..., [2, 1, 0, 3]].copy()
    if fmt == "RGB888":
        rgb = np.frombuffer(buf, np.uint8)[: n * 3].reshape(h, w, 3)
        return np.dstack([rgb, np.full((h, w), 255, np.uint8)])
    if fmt == "RGBA4444":
        return _unpack16(buf, w, h, (4, 4, 4, 4), "rgba")
    if fmt == "ARGB4444":
        return _unpack16(buf, w, h, (4, 4, 4, 4), "argb")
    if fmt == "RGBA5551":
        return _unpack16(buf, w, h, (5, 5, 5, 1), "rgba")
    if fmt == "ARGB1555":
        return _unpack16(buf, w, h, (1, 5, 5, 5), "argb")
    if fmt == "RGB565":
        return _unpack16(buf, w, h, (5, 6, 5), "rgb")
    if fmt == "RGB555":
        return _rgb555(buf, w, h)
    if fmt in ("I8", "L8"):
        g = np.frombuffer(buf, np.uint8)[:n].reshape(h, w)
        return np.dstack([g, g, g, np.full((h, w), 255, np.uint8)])
    if fmt == "A8":
        a = np.frombuffer(buf, np.uint8)[:n].reshape(h, w)
        return np.dstack([np.full((h, w, 3), 255, np.uint8), a])
    if fmt in ("AI88", "LA88"):
        la = np.frombuffer(buf, np.uint8)[: n * 2].reshape(h, w, 2)
        if fmt == "AI88":
            g, a = la[..., 1], la[..., 0]
        else:
            g, a = la[..., 0], la[..., 1]
        return np.dstack([g, g, g, a])
    raise DecodeError(f"pixel format {fmt} unsupported")


def _rgb555(buf: bytes, w: int, h: int) -> np.ndarray:
    out = _unpack16(buf, w, h, (1, 5, 5, 5), "argb")
    out[..., 3] = 255
    return out


def _payload_size(fmt: str, w: int, h: int) -> int:
    if fmt.startswith("PVRTC2"):
        return max(w, 16) * max(h, 8) // 4
    if fmt.startswith("PVRTC4"):
        return max(w, 8) * max(h, 8) // 2
    if fmt in ("ETC1", "ETC2_RGB", "ETC2_RGB_A1", "BC1"):
        return ((w + 3) // 4) * ((h + 3) // 4) * 8
    if fmt in ("ETC2_RGBA", "BC2", "BC3"):
        return ((w + 3) // 4) * ((h + 3) // 4) * 16
    bpp = {"RGBA8888": 4, "BGRA8888": 4, "RGB888": 3, "I8": 1, "L8": 1, "A8": 1}.get(fmt, 2)
    return w * h * bpp


def _pvr3_generic(pf: int) -> tuple[str, str] | None:
    chars = bytes((pf >> (8 * i)) & 0xFF for i in range(4)).rstrip(b"\x00").decode("ascii", "replace")
    bits = tuple((pf >> (32 + 8 * i)) & 0xFF for i in range(len(chars)))
    key = chars + "".join(map(str, bits))
    table = {
        "rgba8888": "RGBA8888", "bgra8888": "BGRA8888", "argb8888": "ARGB8888",
        "rgb888": "RGB888", "rgba4444": "RGBA4444", "argb4444": "ARGB4444",
        "rgba5551": "RGBA5551", "argb1555": "ARGB1555", "rgb565": "RGB565",
        "l8": "L8", "i8": "I8", "a8": "A8", "la88": "LA88",
    }
    return (table[key], key) if key in table else None


def decode_pvr(data: bytes) -> tuple[np.ndarray, TexInfo]:
    if len(data) >= 52 and struct.unpack("<I", data[:4])[0] == 0x03525650:
        (_v, flags, pf, _cs, _ct, h, w, _d, _ns, _nf, _mm, meta) = struct.unpack("<IIQIIIIIIIII", data[:52])
        if pf >> 32 == 0:
            fmt = _PVR3_COMPRESSED.get(pf)
            if fmt is None:
                raise DecodeError(f"PVR3 compressed format id {pf} unsupported")
        else:
            g = _pvr3_generic(pf)
            if g is None:
                raise DecodeError(f"PVR3 channel layout {pf:#x} unsupported")
            fmt = g[0]
        body = data[52 + meta:]
        if fmt == "ARGB8888":
            px = np.frombuffer(body, np.uint8)[: w * h * 4].reshape(h, w, 4)[..., [1, 2, 3, 0]].copy()
        else:
            px = _decode_payload(fmt, body[: _payload_size(fmt, w, h)], w, h)
        info = TexInfo("pvr3", fmt, w, h)
        if flags & _PVR3_FLAG_PREMULT:
            px = unpremultiply(px)
            info.premultiplied = True
            info.notes.append("PVR3 premultiplied flag set: undone")
        return px, info

    if len(data) >= 52 and data[44:48] == b"PVR!":
        (hl, h, w, _mm, flags, dlen, _bpp, _r, _g, _b, amask, _tag, _ns) = struct.unpack("<IIIIIIIIIII4sI", data[:52])
        code = flags & 0xFF
        fmt = _PVR2_FORMATS.get(code)
        if fmt is None:
            raise DecodeError(f"PVR2 format {code:#x} unsupported")
        body = data[hl:]
        px = _decode_payload(fmt, body[: _payload_size(fmt, w, h)], w, h)
        if fmt.startswith("PVRTC") and amask == 0 and not (flags & 0x8000):
            px[..., 3] = 255
        if flags & _PVR2_FLAG_VFLIP:
            px = px[::-1].copy()
        info = TexInfo("pvr2", fmt, w, h)
        if looks_premultiplied(px):
            # PVR2 carries no premultiply flag; TexturePacker's "premultiply alpha"
            # option and cocos2d's PVRImagesHavePremultipliedAlpha were common.
            px = unpremultiply(px)
            info.premultiplied = True
            info.notes.append("PVR2 data looked premultiplied (rgb<=a everywhere): undone")
        return px, info
    raise DecodeError("not a PVR v2/v3 file")


def guess_raw_pvrtc(data: bytes) -> list[tuple[int, int, bool]]:
    """Headerless texturetool output: square power-of-two, 2 or 4 bpp."""
    out = []
    for is2 in (False, True):
        px = len(data) * (4 if is2 else 2)
        side = int(math.isqrt(px))
        if side * side == px and side & (side - 1) == 0 and side >= 8:
            out.append((side, side, is2))
    return out


def decode_raw_pvrtc(data: bytes) -> tuple[np.ndarray, TexInfo]:
    cands = guess_raw_pvrtc(data)
    if not cands:
        raise DecodeError("raw PVRTC: size is not a square power of two")
    w, h, is2 = cands[0]
    fmt = "PVRTC2_RGBA" if is2 else "PVRTC4_RGBA"
    px = _decode_payload(fmt, data, w, h)
    info = TexInfo("pvrtc-raw", fmt, w, h)
    if len(cands) > 1:
        info.notes.append(f"raw PVRTC size ambiguous, assumed {w}x{h} {'2' if is2 else '4'}bpp")
    return px, info


# --------------------------------------------------------------------------- entry

TEXTURE_EXTS = (".png", ".pvr", ".pvr.ccz", ".pvr.gz", ".pvrtc", ".jpg", ".jpeg",
                ".tga", ".bmp", ".gif", ".ccz")


def is_texture_path(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith(TEXTURE_EXTS)


def decode_bytes(data: bytes, name: str = "", ccz_key: list[int] | None = None) -> tuple[np.ndarray, TexInfo]:
    if data[:4] == b"CCZp" and ccz_key:
        data = decrypt_cczp(data, ccz_key)
    payload, wrapper = unwrap(data, name)
    lname = name.lower()
    if is_cgbi(payload):
        px, info = decode_cgbi(payload)
    elif (len(payload) >= 52 and (payload[44:48] == b"PVR!" or payload[:4] == b"PVR\x03")):
        px, info = decode_pvr(payload)
    elif lname.endswith(".pvrtc") or (lname.endswith(".pvr") and not payload[:4].isascii()):
        px, info = decode_raw_pvrtc(payload)
    else:
        try:
            img = Image.open(io.BytesIO(payload))
            img.load()
        except Exception as e:  # noqa: BLE001
            raise DecodeError(f"unrecognised image data: {e}") from e
        kind = (img.format or "image").lower()
        px = np.asarray(img.convert("RGBA")).copy()
        info = TexInfo("jpeg" if kind == "jpeg" else kind, f"{kind.upper()}:{img.mode}", img.width, img.height)
    info.wrapper = wrapper
    return px, info


def decode_file(path: Path, ccz_key: list[int] | None = None) -> tuple[np.ndarray, TexInfo]:
    return decode_bytes(Path(path).read_bytes(), Path(path).name, ccz_key)


# --------------------------------------------------------------------------- encoders

def encode_pvr3_rgba8888(rgba: np.ndarray, premultiply_alpha: bool = False) -> bytes:
    """Uncompressed PVR v3 that cocos2d-iphone 2.x / cocos2d-x load directly."""
    h, w = rgba.shape[:2]
    px = premultiply(rgba) if premultiply_alpha else rgba
    pf = (ord("r") | ord("g") << 8 | ord("b") << 16 | ord("a") << 24) | (8 | 8 << 8 | 8 << 16 | 8 << 24) << 32
    flags = _PVR3_FLAG_PREMULT if premultiply_alpha else 0
    header = struct.pack("<IIQIIIIIIIII", 0x03525650, flags, pf, 0, 0, h, w, 1, 1, 1, 1, 0)
    return header + np.ascontiguousarray(px, np.uint8).tobytes()


def wrap_ccz(payload: bytes) -> bytes:
    return b"CCZ!" + struct.pack(">HHII", 0, 2, 0, len(payload)) + zlib.compress(payload, 9)
