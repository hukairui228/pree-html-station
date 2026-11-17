# iOS 5-7 era (2011-2013) technical reference

## Devices and resolutions

| Device | Years | Screen (px) | Points | Scale | Asset suffixes seen |
|---|---|---|---|---|---|
| iPhone 3GS | 2009-, iOS ≤ 6 | 480×320 | 480×320 | 1× | none (SD) |
| iPhone 4 / 4S | 2010-11 | 960×640 | 480×320 | 2× | `@2x`, cocos2d `-hd` |
| iPhone 5 / 5c / 5s | 2012-13 | 1136×640 | 568×320 | 2× | `-568h@2x` (launch image), `-hd`, `-iphone5hd`/`-widehd` (cocos2d 2.1 / custom) |
| iPad 1 / 2 / mini | 2010-12 | 1024×768 | 1024×768 | 1× | `~ipad`, cocos2d `-ipad` |
| iPad 3 / 4 / Air | 2012-13 | 2048×1536 | 1024×768 | 2× | `@2x~ipad`, cocos2d `-ipadhd` |

The toolkit measures density in **pixels per original iPhone point**: SD = 1,
`-hd`/`@2x`/`-ipad`/`~ipad` = 2, `-ipadhd`/`@2x~ipad` = 4. The default target
(4) therefore means "iPad-retina quality everywhere", enough for iPhone @3x.

Modern targets for comparison: iPhone 15/16 Pro Max 2796×1290 @3x
(932×430 pt, ~19.5:9), iPad Pro 13" 2752×2064 @2x.

## CPU architecture

- armv7 (iPhone 3GS-4S, iPad 1-3), armv7s (iPhone 5/5c, iPad 4), arm64 from
  iPhone 5s / iPad Air (Sept 2013, Xcode 5.0.1).
- iOS 11 (2017) dropped 32-bit apps. A game whose binary has no arm64 slice
  cannot run on any device that runs iOS 11+.
- App Store binaries are FairPlay-encrypted (`LC_ENCRYPTION_INFO` with
  `cryptid=1`). Only the `__TEXT` segment is encrypted; resources are not.

## Texture containers

### CgBI PNG (Xcode `pngcrush -iphone`)
Every `.png` in a shipped `.app` unless `COMPRESS_PNG_FILES = NO`.
- Extra `CgBI` chunk before `IHDR`.
- `IDAT` is **raw deflate** (no zlib header/Adler-32).
- Pixel order **BGRA**, alpha **premultiplied**.
- Standard decoders either fail or show inverted colours / dark halos.
- Decoding here: re-wrap the stream as zlib, let Pillow unfilter, swap
  channels, un-premultiply.

### PVR v2 (legacy, 52-byte header, tag `PVR!` at offset 44)
Written by Apple `texturetool -f PVR`, old PVRTexTool, TexturePacker, Zwoptex.

| flags & 0xFF | format |
|---|---|
| 0x10 | RGBA4444 (u16 LE, R in bits 15-12) |
| 0x11 | RGBA5551 |
| 0x12 | RGBA8888 |
| 0x13 | RGB565 |
| 0x15 | RGB888 |
| 0x16 / 0x17 / 0x1B | I8 / AI88 / A8 |
| 0x18 / 0x19 | PVRTC 2bpp / 4bpp |
| 0x1A | BGRA8888 |

Flag `0x10000` = vertically flipped; `0x8000` = PVRTC has alpha. No
premultiply flag. TexturePacker's "Premultiply alpha" and cocos2d's
`[CCTexture2D PVRImagesHavePremultipliedAlpha:YES]` were common, so the tool
detects it from the data (no colour channel may exceed alpha).

### PVR v3 (`PVR\x03`, 52-byte header + metadata)
`version, flags, pixelFormat(u64), colourSpace, channelType, height, width,
depth, numSurfaces, numFaces, mipMapCount, metaDataSize`.
- pixelFormat high 32 bits = 0 → enum: 0/1 PVRTC 2bpp RGB/RGBA, 2/3 PVRTC 4bpp
  RGB/RGBA, 6 ETC1, 7 BC1, 11 BC3, 22-24 ETC2.
- otherwise: 4 channel chars in the low bytes (`r g b a`) and 4 bit counts in
  the high bytes (8888, 4444, 5551, 565).
- flags & 2 = premultiplied.

### Headerless PVRTC (`texturetool -e PVRTC`, `.pvrtc`)
Raw blocks only. Always square power-of-two; size = w·h/2 (4bpp) or
w·h/4 (2bpp). The tool guesses; ambiguous sizes are noted in the manifest.

### cocos2d wrappers
- `.pvr.ccz`: `CCZ!` + u16 compression (0 = zlib) + u16 version + u32 reserved
  + u32 uncompressed length (big-endian), then a zlib stream.
- `.pvr.gz`: gzip.
- `CCZp`: cocos2d-x `ZipUtils` encryption: XOR with a keystream derived from
  4×u32 key parts (first 512 words fully, then every 64th word), starting at
  byte 12. Key parts are set in code with `setPvrEncryptionKeyPart(i, value)`.

### What each format does to the art (and what the cleanup does)

| Source | Damage | Cleanup before upscaling |
|---|---|---|
| PVRTC 2/4bpp | colour bleeding between neighbouring blocks, smeared edges, grain; 2bpp is much worse | mild non-local-means denoise; alpha smoothing |
| RGBA4444 | 16 levels per channel → banding; TexturePacker added Floyd-Steinberg dither | edge-preserving bilateral blur merges dither back into ramps |
| RGB565 / RGBA5551 | banding (32/64 levels), 1-bit alpha | bilateral; hard-alpha edges rebuilt after upscale |
| JPEG backgrounds | 8×8 blocks, ringing | NLM denoise; x4plus model is trained on JPEG damage |
| premultiplied alpha | colour under translucent edges darkened | un-premultiply on decode, re-premultiply only on cocos export |

## Sprite sheet descriptors

### cocos2d / Zwoptex / TexturePacker `.plist` (`metadata.format`)
- **0** (Zwoptex old): `x y width height offsetX offsetY originalWidth originalHeight`.
- **1**: `frame "{{x,y},{w,h}}"`, `offset "{x,y}"`, `sourceSize "{w,h}"`.
- **2** (most common 2011-13): adds `rotated` (bool) and
  `sourceColorRect "{{x,y},{w,h}}"`.
- **3** (TexturePacker 3+): `textureRect`, `textureRotated`, `spriteOffset`,
  `spriteSize`, `spriteSourceSize`, `aliases`.

Rect sizes are the **unrotated** frame size; a rotated frame occupies h×w in
the texture and is stored 90° clockwise. Offsets are measured from the centre
of the untrimmed frame, **y up**.

### SpriteKit compiled atlas (iOS 7)
`Name.atlasc/Name.plist` + `Name.1.png`, `Name.1@2x.png`, ... Keys: `images[]
→ path, size, subimages[] → name, textureRect, spriteOffset, spriteSourceSize,
textureRotated, isFullyOpaque`.

### BMFont `.fnt`
Text (Hiero, Glyph Designer, bmGlyph) or XML. Coordinates to scale: `info
size padding spacing outline`, `common lineHeight base scaleW scaleH`, `char
x y width height xoffset yoffset xadvance`, `kerning amount`.

## Engine fingerprints

| Engine | Files | Binary strings (only if not encrypted) |
|---|---|---|
| cocos2d-iphone 1.x/2.x | `-hd`, `-ipadhd` suffixes, `.pvr.ccz`, `.plist` sheets, `.fnt` | `cocos2d v`, `CCDirector` |
| cocos2d-x 2.x | same + `.ccbi` (CocosBuilder) | `cocos2d-x` |
| Unity 3.x/4.x | `Data/sharedassets*.assets`, `Data/resources.assets`, `Data/level*` | `UnityAppController` |
| SpriteKit (iOS 7) | `*.atlasc/` | `SKSpriteNode` |
| Sparrow | `.atlas.xml`/`.xml` texture atlases, `@2x` | `SPSprite` |
| Corona SDK | `resource.car` | `CoronaRuntime` |
| Marmalade | `.s3e`, `*.group.bin` | `s3eDevice` |
| Unreal Engine 3 | `CookedIPhone/*.xxx` | |
| GameSalad | `gs_*` files | `GameSalad` |
