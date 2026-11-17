# Using the remastered assets in a remake

## SpriteKit / SwiftUI (`export --to xcassets`)

Drag `Assets.xcassets` (or merge its folders) into the Xcode project.

```swift
// Sprite sheet frames keep their original names and logical point sizes:
let atlas = SKTextureAtlas(named: "hero")            // from hero.spriteatlas
let idle  = SKSpriteNode(texture: atlas.textureNamed("hero_idle"))

// Pixel-art classes must not be smoothed by the GPU:
let coin = SKTexture(imageNamed: "coin")
coin.filteringMode = .nearest
```

The original games were laid out for 480×320 / 568×320 points (iPhone) or
1024×768 (iPad). Keep a fixed design size and scale the scene to the screen
with `scene.scaleMode = .aspectFit` (faithful letterbox) or `.aspectFill`
(crop) instead of re-laying out every coordinate.

## cocos2d-x / Cocos Creator (`export --to cocos`)

The export is a single resolution tier whose density is listed in
`EXPORT.md` (e.g. 4 = 4 px per original iPhone point).

```cpp
// design resolution = the original game's point size
glview->setDesignResolutionSize(480, 320, ResolutionPolicy::SHOW_ALL);
director->setContentScaleFactor(4.0f);   // density from EXPORT.md
FileUtils::getInstance()->addSearchPath("hd4");
SpriteFrameCache::getInstance()->addSpriteFramesWithFile("hero.plist");
```

`.pvr.ccz` exports are uncompressed RGBA8888 inside zlib: larger on disk
than PVRTC was, but modern GPUs/RAM handle it easily. For shipping, convert
to ASTC (`astcenc`, or TexturePacker) if size matters.

## Widescreen backgrounds

| Original | Aspect | Modern phone (19.5:9) |
|---|---|---|
| 480×320 / 960×640 | 3:2 | ~30% narrower than the screen |
| 1136×640 | 16:9 | ~18% narrower |
| 1024×768 / 2048×1536 | 4:3 | for iPad, still fine on iPad |

1. **Pillarbox** with a blurred/darkened copy of the background or a
   decorative border: faithful, zero risk.
2. **Tile/extend** when the art is a repeating pattern (`tileable` in the
   manifest).
3. **Outpaint**: generate the missing sides with an image model, using the
   remastered background as the anchor and the same style prompt for all
   levels. Always show the user before/after; never ship without approval.
   Generate per layer (sky, hills, foreground) if the game uses parallax, so
   the layers still move independently.

## Fonts and text

Many games of the era drew text with BMFont pages (`.fnt`). The export keeps
them working (scaled coordinates). For a cleaner result, identify the
original typeface (or a close free one), and re-render the page with
bmGlyph / Hiero / `fontbm` at the new size using the same character set. The
glyph list is in the original `.fnt`.

## Licence checklist before publishing anything

- Models: `faithful`/`fast` = BSD-3 (OK). `detailed` = CC BY-NC-SA (no
  commercial use).
- Art: owned by the original developer/publisher. A public remake needs their
  permission, or replacement art.
