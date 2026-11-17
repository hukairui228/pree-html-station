"""Build a small synthetic iOS 6-era game IPA that exercises every code path.

Contents of Payload/TestGame.app:
  Info.plist (binary)        MinimumOSVersion 5.0, iPhone+iPad
  TestGame                   fat Mach-O armv7+armv7s, FairPlay cryptid=1
  bg.jpg / bg-hd.jpg         painted background (JPEG)
  menu_bg-hd.png             CgBI (Xcode-crushed) PNG, opaque, tileable
  hero.pvr.ccz / -hd         TexturePacker sheet, PVR2 RGBA4444, premultiplied,
  hero.plist / -hd           format 2, one frame rotated, frames trimmed
  ui.pvr.ccz                 PVR3 RGBA8888 + format-3 plist
  score.fnt + score.png      BMFont text format, CgBI page
  Enemies.atlasc/            SpriteKit compiled atlas (iOS 7)
  coin@2x.png                pixel art blown up 2x with nearest neighbour
"""
from __future__ import annotations

import io
import plistlib
import struct
import sys
import zipfile
import zlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from iosremaster import formats as fmt  # noqa: E402


# --------------------------------------------------------------------------- art

def cartoon_sprite(size: int, hue: tuple[int, int, int], seed: int = 0) -> np.ndarray:
    s = size
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    m = s // 10
    d.ellipse([m, m * 2, s - m, s - m // 2], fill=hue + (255,), outline=(20, 20, 30, 255), width=max(1, s // 32))
    d.ellipse([s * 0.3, s * 0.35, s * 0.45, s * 0.55], fill=(255, 255, 255, 255), outline=(20, 20, 30, 255))
    d.ellipse([s * 0.55, s * 0.35, s * 0.7, s * 0.55], fill=(255, 255, 255, 255), outline=(20, 20, 30, 255))
    d.ellipse([s * 0.36, s * 0.42, s * 0.42, s * 0.5], fill=(0, 0, 0, 255))
    d.ellipse([s * 0.6, s * 0.42, s * 0.66, s * 0.5], fill=(0, 0, 0, 255))
    d.arc([s * 0.35, s * 0.5, s * 0.65, s * 0.75], 20, 160, fill=(80, 0, 0, 255), width=max(1, s // 40))
    big = im.resize((s * 4, s * 4), Image.NEAREST).resize((s, s), Image.LANCZOS)  # soft AA edge
    return np.asarray(big)


def painted_bg(w: int, h: int) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 6, (h, w))
    sky = np.stack([90 + 80 * y / h, 150 + 60 * y / h, 230 - 40 * y / h], -1)
    hills = (y > h * 0.6 + 12 * np.sin(x / w * 6.28 * 2)).astype(np.float32)[..., None]
    rgb = sky * (1 - hills) + np.array([60, 140, 60]) * hills + noise[..., None]
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    return np.dstack([rgb, np.full((h, w), 255, np.uint8)])


def tile_texture(n: int) -> np.ndarray:
    y, x = np.mgrid[0:n, 0:n].astype(np.float32)
    v = 128 + 50 * np.sin(x / n * 2 * np.pi * 2) * np.cos(y / n * 2 * np.pi * 3)
    rgb = np.stack([v * 0.9, v * 0.7, v * 0.5], -1).clip(0, 255).astype(np.uint8)
    return np.dstack([rgb, np.full((n, n), 255, np.uint8)])


def pixel_coin() -> np.ndarray:
    pal = {0: (0, 0, 0, 0), 1: (40, 30, 0, 255), 2: (250, 200, 30, 255), 3: (255, 250, 180, 255)}
    rows = ["0011111100", "0122222210", "1223322221", "1232222221", "1232222221",
            "1222222221", "1222222221", "1222222321", "0122222210", "0011111100"]
    a = np.array([[pal[int(c)] for c in r] for r in rows], np.uint8)
    return np.repeat(np.repeat(a, 2, 0), 2, 1)  # shipped as @2x by nearest neighbour


# --------------------------------------------------------------------------- encoders

def cgbi_png(rgba: np.ndarray) -> bytes:
    """What Xcode's pngcrush -iphone produces: BGRA, premultiplied, raw deflate, CgBI chunk."""
    h, w = rgba.shape[:2]
    pm = fmt.premultiply(rgba)[..., [2, 1, 0, 3]]
    raw = b"".join(b"\x00" + pm[y].tobytes() for y in range(h))
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    idat = comp.compress(raw) + comp.flush()
    ch = fmt._chunk
    return (fmt.PNG_SIG + ch(b"CgBI", b"\x50\x00\x20\x02") +
            ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            ch(b"IDAT", idat) + ch(b"IEND", b""))


def pvr2_rgba4444(rgba: np.ndarray, premult: bool = True) -> bytes:
    h, w = rgba.shape[:2]
    px = fmt.premultiply(rgba) if premult else rgba
    q = (px.astype(np.uint16) * 15 + 127) // 255
    v = (q[..., 0] << 12) | (q[..., 1] << 8) | (q[..., 2] << 4) | q[..., 3]
    data = v.astype("<u2").tobytes()
    hdr = struct.pack("<IIIIIIIIIII4sI", 52, h, w, 0, 0x10, len(data), 16, 0xF000, 0x0F00, 0x00F0, 0x000F, b"PVR!", 1)
    return hdr + data


def place_frames(sprites: dict[str, np.ndarray], W: int, H: int, rotate: set[str]):
    """Tiny shelf packer that trims transparent borders and can rotate frames clockwise."""
    canvas = np.zeros((H, W, 4), np.uint8)
    frames = {}
    x = y = 2
    row_h = 0
    for name, spr in sprites.items():
        ys, xs = np.nonzero(spr[..., 3] > 0)
        t, b, l, r = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        trimmed = spr[t:b, l:r]
        sh, sw = spr.shape[:2]
        rot = name in rotate
        stored = np.rot90(trimmed, -1) if rot else trimmed  # clockwise
        hh, ww = stored.shape[:2]
        if x + ww + 2 > W:
            x, y, row_h = 2, y + row_h + 2, 0
        canvas[y:y + hh, x:x + ww] = stored
        tw, th = trimmed.shape[1], trimmed.shape[0]
        offx = (l + tw / 2) - sw / 2
        offy = -((t + th / 2) - sh / 2)
        frames[name] = {
            "frame": "{{%d,%d},{%d,%d}}" % (x, y, tw, th), "offset": "{%g,%g}" % (offx, offy),
            "rotated": rot, "sourceColorRect": "{{%d,%d},{%d,%d}}" % (l, t, tw, th),
            "sourceSize": "{%d,%d}" % (sw, sh)}
        x += ww + 2
        row_h = max(row_h, hh)
    return canvas, frames


def fat_macho(encrypted: bool = True) -> bytes:
    def thin(sub: int) -> bytes:
        lc = struct.pack("<IIIII", 0x21, 20, 0x1000, 0x2000, 1 if encrypted else 0)
        return struct.pack("<IiiIIII", 0xFEEDFACE, 12, sub, 2, 1, len(lc), 0) + lc + b"\0" * 64
    a, b = thin(9), thin(11)
    off1 = 4096
    off2 = off1 + 4096
    hdr = struct.pack(">II", 0xCAFEBABE, 2) + struct.pack(">iiIII", 12, 9, off1, len(a), 12) + \
        struct.pack(">iiIII", 12, 11, off2, len(b), 12)
    return hdr.ljust(off1, b"\0") + a.ljust(4096, b"\0") + b


# --------------------------------------------------------------------------- build

def build(out: Path) -> Path:
    files: dict[str, bytes] = {}
    app = "Payload/TestGame.app/"
    files[app + "Info.plist"] = plistlib.dumps({
        "CFBundleDisplayName": "Test Game", "CFBundleIdentifier": "com.example.testgame",
        "CFBundleShortVersionString": "1.2", "MinimumOSVersion": "5.0", "UIDeviceFamily": [1, 2],
        "CFBundleExecutable": "TestGame"}, fmt=plistlib.FMT_BINARY)
    files[app + "TestGame"] = fat_macho(True) + b"CCDirector cocos2d v2.1"

    for suffix, (w, h) in {"": (240, 160), "-hd": (480, 320)}.items():
        buf = io.BytesIO()
        Image.fromarray(painted_bg(w, h)[..., :3]).save(buf, "JPEG", quality=70)
        files[app + f"bg{suffix}.jpg"] = buf.getvalue()
    files[app + "menu_bg-hd.png"] = cgbi_png(tile_texture(64))

    for suffix, k in {"": 1, "-hd": 2}.items():
        sprites = {"hero_idle.png": cartoon_sprite(48 * k, (230, 120, 40)),
                   "hero_run.png": cartoon_sprite(48 * k, (40, 160, 230)),
                   "hero_tall.png": np.asarray(Image.fromarray(cartoon_sprite(48 * k, (160, 60, 200))).resize((32 * k, 56 * k)))}
        sheet, frames = place_frames(sprites, 128 * k, 128 * k, rotate={"hero_tall.png"})
        files[app + f"hero{suffix}.pvr.ccz"] = fmt.wrap_ccz(pvr2_rgba4444(sheet))
        files[app + f"hero{suffix}.plist"] = plistlib.dumps({"frames": frames, "metadata": {
            "format": 2, "textureFileName": f"hero{suffix}.pvr.ccz", "size": "{%d,%d}" % (128 * k, 128 * k)}})

    ui_sprites = {"btn_play.png": cartoon_sprite(40, (60, 200, 90)), "btn_pause.png": cartoon_sprite(40, (200, 60, 60))}
    ui_sheet, ui_frames = place_frames(ui_sprites, 96, 48, rotate=set())
    f3 = {}
    for n, fr in ui_frames.items():
        nums = [int(float(v)) for v in fr["frame"].replace("{", " ").replace("}", " ").replace(",", " ").split()]
        f3[n] = {"textureRect": "{{%d,%d},{%d,%d}}" % tuple(nums), "spriteSize": "{%d,%d}" % (nums[2], nums[3]),
                 "spriteOffset": fr["offset"], "spriteSourceSize": fr["sourceSize"], "textureRotated": False,
                 "aliases": []}
    files[app + "ui.pvr.ccz"] = fmt.wrap_ccz(fmt.encode_pvr3_rgba8888(ui_sheet))
    files[app + "ui.plist"] = plistlib.dumps({"frames": f3, "metadata": {"format": 3, "textureFileName": "ui.pvr.ccz",
                                                                           "size": "{96,48}"}})

    # BMFont: 3 glyphs drawn as digits
    page = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(page)
    chars = []
    for i, ch in enumerate("012"):
        x0 = 2 + i * 20
        d.text((x0 + 2, 6), ch, fill=(255, 255, 255, 255))
        d.rectangle([x0, 2, x0 + 16, 26], outline=(255, 220, 0, 255))
        chars.append(f"char id={ord(ch)} x={x0} y=2 width=17 height=25 xoffset=0 yoffset=2 xadvance=18 page=0 chnl=0")
    files[app + "score.png"] = cgbi_png(np.asarray(page))
    files[app + "score.fnt"] = ("info face=\"Score\" size=24 bold=0 italic=0 charset=\"\" unicode=0 stretchH=100 smooth=1 aa=1 padding=0,0,0,0 spacing=2,2\n"
                                "common lineHeight=28 base=24 scaleW=64 scaleH=32 pages=1 packed=0\n"
                                "page id=0 file=\"score.png\"\nchars count=3\n" + "\n".join(chars) + "\n").encode()

    # SpriteKit compiled atlas
    sk_sprites = {"bat.png": cartoon_sprite(32, (90, 90, 90)), "ghost.png": cartoon_sprite(32, (240, 240, 250))}
    sk_sheet, sk_frames = place_frames(sk_sprites, 80, 40, rotate=set())
    subs = []
    for n, fr in sk_frames.items():
        nums = [int(float(v)) for v in fr["frame"].replace("{", " ").replace("}", " ").replace(",", " ").split()]
        subs.append({"name": n, "textureRect": "{{%d,%d},{%d,%d}}" % tuple(nums), "spriteOffset": fr["offset"],
                     "spriteSourceSize": fr["sourceSize"], "textureRotated": False, "isFullyOpaque": False})
    files[app + "Enemies.atlasc/Enemies.1@2x.png"] = cgbi_png(sk_sheet)
    files[app + "Enemies.atlasc/Enemies.plist"] = plistlib.dumps(
        {"format": "APPL", "images": [{"path": "Enemies.1@2x.png", "size": "{80,40}", "subimages": subs}]})

    files[app + "coin@2x.png"] = cgbi_png(pixel_coin())

    # Sparrow / Starling XML atlas: frameX/frameY are negative trim offsets
    sp = {"gem": cartoon_sprite(24, (30, 200, 220)), "key": cartoon_sprite(24, (220, 200, 30))}
    sp_sheet, sp_frames = place_frames(sp, 64, 32, rotate=set())
    subs = []
    for n, fr in sp_frames.items():
        x, y, w, h = [int(float(v)) for v in fr["frame"].replace("{", " ").replace("}", " ").replace(",", " ").split()]
        l, t = [int(float(v)) for v in fr["sourceColorRect"].replace("{", " ").replace("}", " ").replace(",", " ").split()][:2]
        subs.append(f'<SubTexture name="{n}" x="{x}" y="{y}" width="{w}" height="{h}" '
                    f'frameX="{-l}" frameY="{-t}" frameWidth="24" frameHeight="24"/>')
    files[app + "items.png"] = cgbi_png(sp_sheet)
    files[app + "items.xml"] = ('<?xml version="1.0" encoding="UTF-8"?>\n<TextureAtlas imagePath="items.png">\n'
                                + "\n".join(subs) + "\n</TextureAtlas>\n").encode()

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return out


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "TestGame.ipa")))
