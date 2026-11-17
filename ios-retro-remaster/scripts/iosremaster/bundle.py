"""Open an iOS game (IPA / .app / loose folder) and work out what it is."""
from __future__ import annotations

import os
import plistlib
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BundleInfo:
    root: Path                      # directory that holds the game's resources
    app_dir: Path | None = None     # the .app, when there is one
    name: str = ""
    bundle_id: str = ""
    version: str = ""
    min_os: str = ""
    device_family: list[int] = field(default_factory=list)  # 1 iPhone, 2 iPad
    executable: str = ""
    archs: list[str] = field(default_factory=list)
    encrypted: bool | None = None
    engines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- extraction

def extract_ipa(ipa: Path, dest: Path) -> Path:
    """Unzip with path-traversal protection. Returns the extraction dir."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(ipa) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if os.path.commonpath([root, target]) != str(root):
                raise ValueError(f"unsafe path in IPA: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue  # skip symlinks
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
    return dest


def find_app(root: Path) -> Path | None:
    payload = root / "Payload"
    for base in (payload, root):
        if base.is_dir():
            apps = sorted(base.glob("*.app"))
            if apps:
                return apps[0]
    return root if root.suffix == ".app" else None


# --------------------------------------------------------------------------- Mach-O

_CPU = {7: "i386", 12: "arm", 0x0100000C: "arm64", 0x01000007: "x86_64"}
_ARM_SUB = {6: "armv6", 9: "armv7", 10: "armv7f", 11: "armv7s", 12: "armv7k"}


def _thin_info(buf: bytes, off: int) -> tuple[str, bool | None]:
    magic = struct.unpack("<I", buf[off: off + 4])[0]
    if magic not in (0xFEEDFACE, 0xFEEDFACF):
        return "?", None
    is64 = magic == 0xFEEDFACF
    cpu, sub, _ft, ncmds, _sz, _fl = struct.unpack("<iiIIII", buf[off + 4: off + 28])
    arch = _CPU.get(cpu, f"cpu{cpu}")
    if arch == "arm":
        arch = _ARM_SUB.get(sub & 0xFFFFFF, f"arm-{sub}")
    p = off + (32 if is64 else 28)
    enc = None
    for _ in range(ncmds):
        if p + 8 > len(buf):
            break
        cmd, size = struct.unpack("<II", buf[p: p + 8])
        if cmd in (0x21, 0x2C):  # LC_ENCRYPTION_INFO(_64)
            _cryptoff, _cryptsize, cryptid = struct.unpack("<III", buf[p + 8: p + 20])
            enc = bool(cryptid)
        p += max(size, 8)
    return arch, enc


def macho_info(path: Path) -> tuple[list[str], bool | None]:
    try:
        with open(path, "rb") as f:
            head = f.read(1 << 16)
            magic = struct.unpack(">I", head[:4])[0]
            if magic == 0xCAFEBABE:  # fat, big-endian header
                n = struct.unpack(">I", head[4:8])[0]
                archs, encs = [], []
                for i in range(n):
                    _cpu, _sub, off, _size, _align = struct.unpack(">iiIII", head[8 + 20 * i: 28 + 20 * i])
                    f.seek(off)
                    a, e = _thin_info(f.read(1 << 16), 0)
                    archs.append(a)
                    encs.append(e)
                known = [e for e in encs if e is not None]
                return archs, (any(known) if known else None)
            a, e = _thin_info(head, 0)
            return [a], e
    except (OSError, struct.error):
        return [], None


# --------------------------------------------------------------------------- engine detection

_ENGINE_FILE_RULES: list[tuple[str, re.Pattern]] = [
    ("Unity", re.compile(r"(^|/)Data/(sharedassets\d+\.assets|resources\.assets|mainData|level\d+)$")),
    ("Unreal Engine 3", re.compile(r"(^|/)CookedIPhone/|\.xxx$|(^|/)UDKGame/", re.I)),
    ("Corona SDK", re.compile(r"(^|/)resource\.car$")),
    ("Marmalade", re.compile(r"\.s3e$|\.group\.bin$|(^|/)derbh", re.I)),
    ("SpriteKit", re.compile(r"\.atlasc/")),
    ("cocos2d", re.compile(r"-(hd|ipadhd|ipad)\.(png|pvr|pvr\.ccz|plist|fnt)$|\.pvr\.ccz$")),
    ("GameSalad", re.compile(r"(^|/)(gs_|GameSalad)", re.I)),
    ("Sparrow", re.compile(r"\.atlas\.xml$|(^|/)sparrow", re.I)),
]
_ENGINE_BIN_STRINGS = [
    (b"cocos2d-x", "cocos2d-x"), (b"cocos2d-iphone", "cocos2d-iphone"), (b"cocos2d v", "cocos2d-iphone"),
    (b"UnityAppController", "Unity"), (b"UnityEngine", "Unity"), (b"SPSprite", "Sparrow"),
    (b"SKSpriteNode", "SpriteKit"), (b"CoronaRuntime", "Corona SDK"), (b"s3eDevice", "Marmalade"),
    (b"Unreal", "Unreal Engine 3"), (b"GameSalad", "GameSalad"), (b"CCDirector", "cocos2d"),
]


def detect_engines(root: Path, executable: Path | None, encrypted: bool | None) -> list[str]:
    found: list[str] = []
    rels = [str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*") if p.is_file()]
    for engine, rx in _ENGINE_FILE_RULES:
        if any(rx.search(r) for r in rels) and engine not in found:
            found.append(engine)
    if executable and executable.is_file() and encrypted is not True:
        try:
            blob = executable.read_bytes()
            for needle, engine in _ENGINE_BIN_STRINGS:
                if needle in blob and engine not in found:
                    found.append(engine)
        except OSError:
            pass
    if "cocos2d" in found and ("cocos2d-x" in found or "cocos2d-iphone" in found):
        found.remove("cocos2d")
    return found


# --------------------------------------------------------------------------- open

def open_bundle(src: Path, work: Path) -> BundleInfo:
    src = Path(src)
    if src.is_file() and src.suffix.lower() in (".ipa", ".zip"):
        root = extract_ipa(src, work / "bundle")
    elif src.is_dir():
        root = src
    else:
        raise FileNotFoundError(f"{src}: expected an .ipa or a folder")

    app = find_app(root)
    info = BundleInfo(root=app or root, app_dir=app)
    plist_path = (app or root) / "Info.plist"
    if plist_path.is_file():
        try:
            pl = plistlib.loads(plist_path.read_bytes())
            info.name = pl.get("CFBundleDisplayName") or pl.get("CFBundleName") or ""
            info.bundle_id = pl.get("CFBundleIdentifier", "")
            info.version = pl.get("CFBundleShortVersionString") or pl.get("CFBundleVersion") or ""
            info.min_os = pl.get("MinimumOSVersion", "")
            info.device_family = [int(x) for x in pl.get("UIDeviceFamily", [1])]
            info.executable = pl.get("CFBundleExecutable", "")
        except Exception as e:  # noqa: BLE001
            info.notes.append(f"Info.plist unreadable: {e}")
    exe = (app or root) / info.executable if info.executable else None
    if exe and exe.is_file():
        info.archs, info.encrypted = macho_info(exe)
        if info.encrypted:
            info.notes.append(
                "Main binary is FairPlay-encrypted (cryptid=1). Code can't be read without a "
                "decrypted dump, but resources (images, plists, fonts) are not encrypted.")
        if info.archs and "arm64" not in info.archs:
            info.notes.append(
                "32-bit only (" + ", ".join(info.archs) + "): will not launch on iOS 11+. "
                "A remake / port is the realistic way to play it on modern devices.")
    if (info.root / "Assets.car").is_file():
        info.notes.append(
            "Assets.car (Xcode 5 asset catalog) present: decode it on macOS with "
            "`xcrun --sdk iphoneos assetutil --info` / acextract; usually only icons + launch images.")
    info.engines = detect_engines(info.root, exe, info.encrypted)
    return info


# --------------------------------------------------------------------------- resolution variants

# iOS 5-7 resolution suffixes, in the order they are stripped.
#   density = pixels per iPhone-SD point (480x320 = 1)
_VARIANTS = [
    (re.compile(r"-ipadhd$"), 4.0, "pad"),      # cocos2d: iPad retina
    (re.compile(r"-ipad$"), 2.0, "pad"),        # cocos2d: iPad 1/2/mini (1024x768 ~= 2x SD)
    (re.compile(r"-widehd$"), 2.0, "phone"),
    (re.compile(r"-iphone5hd$"), 2.0, "phone"),
    (re.compile(r"-hd$"), 2.0, "phone"),        # cocos2d: retina iPhone
    (re.compile(r"-568h@2x$"), 2.0, "phone"),   # iPhone 5 widescreen
    (re.compile(r"@3x$"), 3.0, "phone"),
    (re.compile(r"@2x$"), 2.0, None),           # UIKit / Sparrow / SpriteKit retina
]
_DEVICE = re.compile(r"~(ipad|iphone)$", re.I)


def split_variant(stem: str) -> tuple[str, float, str | None]:
    """'menu_bg-ipadhd' -> ('menu_bg', 4.0, 'pad'); 'btn@2x~ipad' -> ('btn', 4.0, 'pad')."""
    s = stem
    device = None
    m = _DEVICE.search(s)
    if m:
        device = "pad" if m.group(1).lower() == "ipad" else "phone"
        s = s[: m.start()]
    density = 1.0
    for rx, dens, dev in _VARIANTS:
        m = rx.search(s)
        if m:
            s = s[: m.start()]
            density = dens
            device = device or dev
            break
    m = _DEVICE.search(s)  # 'foo~ipad@2x' ordering
    if m:
        device = "pad" if m.group(1).lower() == "ipad" else "phone"
        s = s[: m.start()]
    if device == "pad" and density == 2.0 and stem.endswith(("@2x~ipad", "~ipad@2x")):
        density = 4.0
    elif device == "pad" and density == 1.0 and "~ipad" in stem.lower():
        density = 2.0
    return s, density, device


def texture_stem(name: str) -> tuple[str, str]:
    """Split compound extensions: 'a-hd.pvr.ccz' -> ('a-hd', '.pvr.ccz')."""
    low = name.lower()
    for ext in (".pvr.ccz", ".pvr.gz"):
        if low.endswith(ext):
            return name[: -len(ext)], name[-len(ext):]
    p = Path(name)
    return p.stem, p.suffix
