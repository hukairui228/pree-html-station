---
name: ios-retro-remaster
description: >-
  Remaster the 2D art of an old iOS game from the iOS 5 / 6 / 7 era (2011-2013,
  iPhone 4/4S/5, iPad 2/3/4) for a faithful remake on modern devices. Unpacks
  the user's own .ipa/.app, decodes the era's texture formats (Xcode CgBI PNG,
  PVRTC / RGBA4444 .pvr, cocos2d .pvr.ccz, TexturePacker/Zwoptex plists,
  SpriteKit .atlasc, BMFont .fnt, Unity 3/4 Texture2D), picks the best
  -hd/-ipadhd/@2x source, cleans the compression damage, upscales frame by
  frame with an art-style-appropriate model, and exports a cocos2d tier or an
  Xcode asset catalog. Use when the user says "复刻/重制 iOS 老游戏", "高清化贴图",
  "材质优化", "upscale the sprites of my old iPhone game", "remaster this IPA",
  "make this cocos2d game HD", or hands over an .ipa from that period.
---

# ios-retro-remaster

Turn "this old iPhone game looks blurry on my new phone" into a set of clean,
high-resolution assets ready for a remake, without the user touching a
terminal. You run the bundled `remaster` CLI, explain what it found in the
user's language, show before/after comparisons, and only scale up to the
full game after they approve a sample.

Everything runs locally: Apple Silicon (MPS), NVIDIA (CUDA) or CPU.

## 0. Setup (once)

```bash
SKILL=~/.claude/skills/ios-retro-remaster      # or wherever this folder lives
"$SKILL/install.sh"                            # creates $SKILL/.venv, ~1-2 GB with torch
"$SKILL/scripts/remaster" doctor               # deps, device, cached models
```

`install.sh --cpu` installs the small CPU-only torch on Linux. Without torch
only `--preset classic` (Lanczos + ScaleNx) works; say so if install fails.
Models download on first use (anime6b 18 MB, x4plus 64 MB) into
`~/.cache/ios-retro-remaster/models`.

## 1. Get the game

Ask for the path to the `.ipa` (or an extracted `.app` / folder). Don't guess
paths. If they don't have the IPA any more, tell them where old purchases
usually still exist: `~/Music/iTunes/iTunes Media/Mobile Applications/` on an
old Mac/PC (iTunes ≤ 12.6), an Apple Configurator 2 download, or a backup
drive. **FairPlay encryption does not matter here**: it only encrypts the
executable, never the images, plists and fonts. Only work on copies the user
owns.

## 2. Scan

```bash
"$SKILL/scripts/remaster" scan "/path/Game.ipa" ./work
```

This writes `work/report.md` (Chinese) and `work/manifest.json`. Relay the
highlights; don't paste the whole report:

- game name, minimum iOS, architectures, engine(s) detected
- how many textures, how many were low-res duplicates (`-hd` beats SD,
  `-ipadhd` beats `-hd`: only the best copy of each asset gets processed)
- which artifacts were found (PVRTC, RGBA4444 banding, JPEG) and that they
  are cleaned before upscaling
- any decode errors (`CCZp` = encrypted cocos2d-x sheets, see §7)

Then check the classification. `remaster list ./work` shows each texture's
class; if something is obviously wrong (a background tagged `ui`, a
pixel-art game tagged `cartoon`), fix it:
`remaster set-class ./work painted 12 13 14`.

| class | what it is in games of this era | default engine (`faithful`) |
|---|---|---|
| cartoon | flat-shaded vector-style sprites (the dominant 2011-13 look) | `anime6b` |
| ui | buttons, HUD, panels, icons | `anime6b` |
| font | BMFont pages, digit strips | `anime6b`, per glyph |
| painted | hand-painted / rendered backgrounds | `x4plus` |
| photo | photographic / JPEG backgrounds | `x4plus` |
| pixel | pixel art, incl. art already blown up with nearest neighbour | `pixel` (Scale2x/3x, no AI) |

## 3. Estimate, then a sample

```bash
"$SKILL/scripts/remaster" bench ./work                 # s per megapixel + estimate per preset
"$SKILL/scripts/remaster" upscale ./work --sample 3    # 3 largest textures per class
```

Quote the estimate before any long run. Rough numbers: on a 4-core CPU
anime6b takes ~50 s per input megapixel (×2 for sprites, which also get an
alpha pass); Apple Silicon / CUDA are 10-30× faster. A typical cocos2d game
of the era has 5-40 MP of best-variant art.

**Show, don't tell.** Open `work/compare/index.html` (slider: original
nearest-neighbour vs remaster) or render a couple of comparisons for the user
yourself. Ask what they think before the full run. Knobs if they don't like it:

- too smooth / plasticky → `--preset detailed` (adds texture; **NC licence**,
  see below) or lower `--clean 0.5`
- too much invented detail → `--preset faithful` (default) or `--engine anime6b`
- pixel art got smoothed → `set-class ... pixel`
- slow machine → `--preset fast` (animevideo, ~15× faster, a bit softer)

## 4. Full run

```bash
"$SKILL/scripts/remaster" upscale ./work                # resumable; re-run after Ctrl+C
```

Scale defaults to `--target 4`: every asset ends at 4 px per original iPhone
point (SD art ×4, `-hd` ×2, `-ipadhd` ×1 cleanup only). That covers iPhone
@3x and iPad Pro. Use `--target 6` for 4K/TV, or `--scale N` to force a
factor. Atlas factors are always integers so the plist maths stays exact.

## 5. Export for the remake

Ask which engine the remake uses:

```bash
"$SKILL/scripts/remaster" export ./work --to xcassets   # SwiftUI / SpriteKit remake
"$SKILL/scripts/remaster" export ./work --to cocos      # cocos2d-x / Cocos Creator / port of the original
```

- **xcassets**: `Assets.xcassets` with an `.imageset` per image (@2x + @3x
  derived from the master) and a `.spriteatlas` per sprite sheet: frames are
  cut out, un-rotated and un-trimmed back to their original logical size, so
  `SKSpriteNode(imageNamed:)` sizes match the original game's points. Bitmap
  fonts go to `BitmapFonts/` (`.fnt` + page).
- **cocos**: same folder layout and names minus `-hd/-ipadhd/@2x`, `.plist`
  and `.fnt` coordinates multiplied, `.pvr.ccz` sources re-emitted as
  `.pvr.ccz` (PVR3 RGBA8888, premultiplied if the original was). `EXPORT.md`
  states the content-scale factor to set.

`export --dest` refuses a non-empty folder it didn't create; export into the
work dir, then let the user copy it into their project.

## 6. What to tell the user honestly

- **The original game won't run on a modern iPhone.** Games of this era are
  32-bit (armv7/armv7s; arm64 only arrived with the iPhone 5s in late 2013)
  and iOS 11+ refuses them. touchHLE only targets iPhone OS 2-3 games, so for
  iOS 5-7 titles a remake/port is the realistic path. The scan report says
  which archs the binary has.
- **Text baked into images stays a weakness.** AI rebuilds glyph shapes
  well at ×2-×4, but menus with lots of small text look best re-typeset with a
  real font. Point those textures out.
- **Aspect ratio.** Backgrounds were made for 3:2 (480×320, 960×640),
  16:9 (iPhone 5's 1136×640) or 4:3 (iPad). Modern phones are ~19.5:9. Options:
  letter/pillarbox (faithful), stretch tiles, or extend backgrounds
  generatively (outpainting, e.g. fal.ai with the original as style anchor):
  a separate, approved step, never automatic.
- **Licences.** `faithful`/`fast` use BSD-3 Real-ESRGAN models: fine for
  anything. `detailed` uses 4x-UltraSharp/Remacri (CC BY-NC-SA): personal or
  non-commercial remakes only. And the art itself belongs to the original
  studio: a remake shared or sold publicly needs their permission; remastered
  assets of a game the user owns are for their own use.
- **Already-HD games gain little.** If the game shipped `-ipadhd` art
  (2048×1536 era), the output is mostly cleanup (PVRTC/4444 artifacts), not
  new resolution. Say so instead of overselling.

## 7. Red flags: stop and ask

- `scan` finds zero textures: wrong path, or the art is inside a container
  this tool doesn't open (see below). Don't invent paths.
- `CCZp` decode errors: cocos2d-x encrypted sprite sheets. The 128-bit key is
  set in the game binary via `ZipUtils::setPvrEncryptionKeyPart`; it can be
  recovered only from a **decrypted** binary. If the user has the 4 parts:
  `scan ... --ccz-key 0x....,0x....,0x....,0x....`.
- A long full run that the user hasn't seen an estimate for.
- The user wants to publish the remake with the original art: point at the
  licence note above before continuing.

## Not covered (say so, suggest the right tool)

- **3D games** (Unity 3D meshes, Unreal Engine 3 titles like Infinity Blade,
  `CookedIPhone/*.xxx`): extract with UE Viewer (umodel) / AssetRipper, then a
  texture-pack tool such as texup. This skill only reads Unity Texture2D
  (via UnityPy) and treats them as standalone images.
- **Assets.car** (Xcode 5 asset catalogs): needs macOS
  (`xcrun --sdk iphoneos assetutil --info`, acextract). In games of this era it
  usually holds only icons and launch images.
- Marmalade `.group.bin`, Corona `resource.car`: detected and reported, not
  unpacked.
- Audio, video, code.

## Files

- `scripts/remaster`: CLI wrapper (uses `.venv` if present)
- `scripts/iosremaster/`: `formats.py` (CgBI, PVR2/3, CCZ, PVRTC),
  `atlas.py` (plist formats 0-3, SpriteKit, BMFont), `bundle.py` (IPA,
  Mach-O, engines, suffixes), `imageops.py` (classes, cleanup, alpha,
  ScaleNx), `upscalers.py` (models/presets), `pipeline.py`, `export.py`
- `references/ios-5-7-era.md`: formats, suffixes, devices, engine fingerprints
- `references/remake-notes.md`: using the output in SpriteKit / cocos2d-x
- `tests/`: synthetic iOS 6 IPA builder + pytest suite
