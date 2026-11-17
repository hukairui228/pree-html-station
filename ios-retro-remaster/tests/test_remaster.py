"""End-to-end and unit tests. The neural path is exercised with --preset classic
so the suite runs without torch or model downloads; AI runs are checked by hand."""
from __future__ import annotations

import plistlib
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_fixture as mf  # noqa: E402
from iosremaster import atlas, bundle, export, formats, imageops, pipeline  # noqa: E402


@pytest.fixture(scope="module")
def game(tmp_path_factory):
    d = tmp_path_factory.mktemp("game")
    ipa = mf.build(d / "TestGame.ipa")
    work = d / "work"
    m = pipeline.scan(ipa, work)
    return ipa, work, m


# ------------------------------------------------------------------ formats

def test_cgbi_roundtrip_exact():
    img = mf.tile_texture(32)
    px, info = formats.decode_bytes(mf.cgbi_png(img), "x.png")
    assert info.container == "cgbi" and info.premultiplied
    assert np.array_equal(px, img)


def test_cgbi_translucent_is_unpremultiplied():
    img = mf.cartoon_sprite(32, (200, 100, 50))
    px, _ = formats.decode_bytes(mf.cgbi_png(img), "x.png")
    m = img[..., 3] > 64  # low alpha loses precision in any premultiplied format
    assert np.abs(px[m].astype(int) - img[m].astype(int)).max() <= 4


def test_pvr2_rgba4444_premultiplied_detected():
    img = mf.cartoon_sprite(32, (200, 100, 50))
    px, info = formats.decode_bytes(formats.wrap_ccz(mf.pvr2_rgba4444(img)), "a.pvr.ccz")
    assert info.wrapper == "ccz" and info.pixel_format == "RGBA4444"
    assert info.premultiplied and info.lossy == "quantized16"
    m = img[..., 3] == 255
    assert np.abs(px[m].astype(int) - img[m].astype(int)).max() <= 9  # 4-bit quantisation


def test_pvr3_rgba8888_roundtrip():
    img = mf.cartoon_sprite(16, (1, 2, 3))
    px, info = formats.decode_bytes(formats.encode_pvr3_rgba8888(img), "a.pvr")
    assert info.container == "pvr3" and np.array_equal(px, img)


def test_pvrtc_decode_shapes():
    data = bytes(range(256)) * 2  # 512 bytes = 32x32 at 4bpp
    for is2, fmtid in ((False, 3), (True, 1)):
        w = 32
        h = 32 if not is2 else 64
        payload = (bytes(range(256)) * 8)[: (w * h // (4 if is2 else 2))]
        pf = fmtid
        import struct
        hdr = struct.pack("<IIQIIIIIIIII", 0x03525650, 0, pf, 0, 0, h, w, 1, 1, 1, 1, 0)
        px, info = formats.decode_bytes(hdr + payload, "t.pvr")
        assert px.shape == (h, w, 4) and info.lossy == "pvrtc"
    assert formats.guess_raw_pvrtc(data)[0][:2] == (32, 32)


def test_cczp_xor_is_symmetric():
    blob = formats.wrap_ccz(b"PVR!" * 1000)
    fake = b"CCZp" + blob[4:]
    key = [0x12345678, 0x9ABCDEF0, 0x0FEDCBA9, 0x87654321]
    enc = formats.decrypt_cczp(fake, key)  # XOR keystream: applying twice is identity
    back = formats.decrypt_cczp(b"CCZp" + enc[4:], key)
    assert back == blob


# ------------------------------------------------------------------ variants / bundle

@pytest.mark.parametrize("stem,base,dens,dev", [
    ("menu-ipadhd", "menu", 4.0, "pad"), ("menu-hd", "menu", 2.0, "phone"), ("menu-ipad", "menu", 2.0, "pad"),
    ("btn@2x", "btn", 2.0, None), ("btn@2x~ipad", "btn", 4.0, "pad"), ("btn~ipad", "btn", 2.0, "pad"),
    ("Default-568h@2x", "Default", 2.0, "phone"), ("plain", "plain", 1.0, None),
])
def test_split_variant(stem, base, dens, dev):
    assert bundle.split_variant(stem) == (base, dens, dev)


def test_bundle_info(game):
    _, _, m = game
    b = m["bundle"]
    assert b["min_os"] == "5.0" and b["archs"] == ["armv7", "armv7s"] and b["encrypted"] is True
    assert "cocos2d" in b["engines"] and "SpriteKit" in b["engines"]


def test_scan_picks_best_variant_and_classes(game):
    _, _, m = game
    by = {r["rel"]: r for r in m["records"]}
    assert by["hero-hd.pvr.ccz"]["best"] and not by["hero.pvr.ccz"]["best"]
    assert by["bg-hd.jpg"]["best"] and by["bg.jpg"]["superseded_by"] == "bg-hd.jpg"
    assert by["coin@2x.png"]["class"] == "pixel"
    assert by["score.png"]["class"] == "font"
    assert by["menu_bg-hd.png"]["tileable"]
    assert by["hero-hd.pvr.ccz"]["atlas"]["regions"] == 3
    assert by["Enemies.atlasc/Enemies.1@2x.png"]["atlas"]["kind"] == "spritekit"
    assert by["items.png"]["atlas"]["kind"] == "sparrow" and by["items.png"]["atlas"]["regions"] == 2


# ------------------------------------------------------------------ atlas

def test_rotated_frame_region_is_swapped(game):
    _, work, m = game
    root = Path(m["bundle"]["root"])
    d = atlas.parse_descriptor(root / "hero-hd.plist")
    tall = next(r for r in d.regions[d.textures[0]] if r.name == "hero_tall.png")
    assert tall.rotated and tall.w > tall.h  # stored on its side


def test_scale_plist_and_fnt(game):
    _, _, m = game
    root = Path(m["bundle"]["root"])
    d = atlas.parse_descriptor(root / "hero-hd.plist")
    out = plistlib.loads(atlas.scale_descriptor(d, 3, {"hero-hd.pvr.ccz": "hero.pvr.ccz"}))
    assert out["metadata"]["size"] == "{768,768}" and out["metadata"]["textureFileName"] == "hero.pvr.ccz"
    fr = out["frames"]["hero_tall.png"]
    assert fr["rotated"] is True and fr["sourceSize"] == "{192,336}"
    f = atlas.parse_descriptor(root / "score.fnt")
    txt = atlas.scale_descriptor(f, 2, {}).decode()
    assert "lineHeight=56" in txt and "scaleW=128" in txt and "xadvance=36" in txt


# ------------------------------------------------------------------ image ops

def test_bleed_alpha_fills_transparent_rgb():
    img = np.zeros((8, 8, 4), np.uint8)
    img[2:4, 2:4] = (200, 10, 10, 255)
    out = imageops.bleed_alpha(img)
    assert (out[..., 3] == img[..., 3]).all()
    assert out[7, 7, 0] == 200  # no black under transparent pixels -> no dark halo


def test_scale2x_keeps_palette():
    coin = mf.pixel_coin()
    up = imageops.pixel_upscale(coin, 2)
    assert up.shape == (40, 40, 4)
    pal = {tuple(p) for p in coin.reshape(-1, 4)}
    assert {tuple(p) for p in up.reshape(-1, 4)} <= pal


def test_classify_photo_vs_cartoon():
    assert imageops.classify(mf.painted_bg(320, 200), "bg.jpg", container="jpeg")[0] in ("photo", "painted")
    assert imageops.classify(mf.cartoon_sprite(64, (10, 200, 10)), "hero.png")[0] == "cartoon"


# ------------------------------------------------------------------ pipeline + export (classic, no torch)

def test_pipeline_classic_and_exports(game):
    _, work, m = game
    st = pipeline.upscale(work, preset="classic", log=lambda *_: None)
    assert st.get("done") == 8 and not st.get("failed")
    m = pipeline.load_manifest(work)
    hero = next(r for r in m["records"] if r["rel"] == "hero-hd.pvr.ccz")
    assert hero["factor"] == 2 and (hero["out_w"], hero["out_h"]) == (512, 512) and hero["regions"] == 3
    assert (work / "compare" / "index.html").is_file()

    # gutters between frames stay transparent (no cross-frame bleeding)
    out = np.asarray(Image.open(work / hero["out"]))
    assert out[:4, :, 3].max() == 0

    files = export.export(work, "cocos")
    assert "hero.plist" in files and "hero.pvr.ccz" in files
    px, info = formats.decode_file(work / "export-cocos" / "hero.pvr.ccz")
    assert px.shape == (512, 512, 4)

    export.export(work, "xcassets")
    tall = work / "export-xcassets/Assets.xcassets/hero.spriteatlas/hero_tall.imageset/hero_tall@3x.png"
    assert Image.open(tall).size == (96, 168)  # 32x56 pt untrimmed + un-rotated, at @3x
    assert (work / "export-xcassets/Assets.xcassets/Enemies.spriteatlas").is_dir()
    gem = work / "export-xcassets/Assets.xcassets/items.spriteatlas/gem.imageset/gem@3x.png"
    assert Image.open(gem).size == (72, 72)  # Sparrow frameWidth 24 px @1x -> 72 px @3x


def test_export_refuses_foreign_folder(game, tmp_path):
    _, work, _ = game
    (tmp_path / "keep.txt").write_text("user data")
    with pytest.raises(FileExistsError):
        export.export(work, "cocos", tmp_path)
    assert (tmp_path / "keep.txt").exists()


def test_ipa_path_traversal_rejected(tmp_path):
    import zipfile
    bad = tmp_path / "bad.ipa"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../../evil.txt", "x")
    with pytest.raises(ValueError):
        bundle.extract_ipa(bad, tmp_path / "out")
