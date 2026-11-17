# ios-retro-remaster：iOS 5–7 老游戏贴图重制 Skill

给 Claude Code 用的 skill：把 2011–2013 年（iOS 5 / 6 / 7，iPhone 4/4S/5、iPad 2/3/4 时代）的 iOS 游戏的 2D 美术素材解包、清理、高清化，导出成可以直接用于复刻的素材包。

你只要跟 Claude 说「帮我把这个 IPA 的贴图高清化」「复刻这个老游戏，先把素材弄清楚」，它会按 `SKILL.md` 的流程跑完：扫描 → 估时 → 小样对比 → 你确认 → 全量 → 导出。

## 能处理什么（针对那个年代专门做的）

| 那个年代的东西 | 处理方式 |
|---|---|
| Xcode 压过的 PNG（CgBI：BGRA + 预乘 alpha + 无头 deflate） | 直接解码，普通看图软件打不开的也能读 |
| `.pvr` / `.pvr.ccz` / `.pvr.gz`（PVR v2/v3，PVRTC 2/4bpp、RGBA4444、RGB565、RGBA8888…） | 全部解码；预乘 alpha 自动识别并还原 |
| cocos2d / TexturePacker / Zwoptex 图集 `.plist`（format 0–3，含旋转、裁边帧） | **按帧单独放大**，帧之间不串色；plist 坐标同步乘倍数 |
| SpriteKit `.atlasc`（iOS 7）、Sparrow XML 图集 | 同上 |
| BMFont `.fnt` 位图字体 | 按字形放大，`.fnt` 坐标同步改写 |
| `-hd` / `-ipad` / `-ipadhd` / `@2x` / `~ipad` / `-568h@2x` 多分辨率 | 自动只挑最高清的那一份当源 |
| PVRTC 糊块、4444 色带和抖动噪点、JPEG 方块 | 放大**之前**先清理，避免把瑕疵一起放大 |
| 像素风素材（包括被最近邻放大过的「胖像素」） | 先还原真实像素，再用 Scale2x/3x，不用 AI 乱补细节 |
| 可平铺背景 | 环绕填充，放大后接缝不裂 |
| Unity 3.x/4.x 游戏 | 用 UnityPy 导出 Texture2D 再处理 |
| 扫描报告 | 识别引擎、32 位架构、FairPlay 加密、最低系统版本 |

模型（按画风自动选，可手动改）：

- `anime6b`（Real-ESRGAN 动漫版）：卡通、UI、字体。那个年代 iOS 游戏的主流画风，默认用它。
- `x4plus`（Real-ESRGAN 通用版）：手绘、写实背景，能扛 JPEG/PVRTC 损伤。
- `animevideo`：快 10 倍以上，适合只有 CPU 的电脑。
- `ultrasharp` / `remacri`：细节最多，但是**非商用许可**。

## 安装

```bash
git clone -b claude/ios-retro-game-textures-668s46 https://github.com/hukairui228/pree-html-station.git
mkdir -p ~/.claude/skills
cp -R pree-html-station/ios-retro-remaster ~/.claude/skills/
~/.claude/skills/ios-retro-remaster/install.sh        # Mac 会自动用 Apple 芯片的 GPU (MPS)
```

装完重启 Claude Code，输入 `/skills` 能看到 `ios-retro-remaster` 就好了。

## 手动用法（不经过 Claude 也行）

```bash
R=~/.claude/skills/ios-retro-remaster/scripts/remaster
$R scan  MyGame.ipa ./work                 # 解包 + 解码 + 分类，生成 work/report.md
$R bench ./work                            # 测速 + 估算全量耗时
$R upscale ./work --sample 3               # 每类先放大 3 张最大的
open work/compare/index.html               # 滑块对比原版 / 重制版
$R upscale ./work                          # 全量（可中断续跑）
$R export ./work --to xcassets             # SpriteKit/SwiftUI 复刻用
$R export ./work --to cocos                # cocos2d-x 复刻用（plist/fnt 已改好坐标）
```

## 需要知道的限制

- **原版游戏在新 iPhone 上跑不起来。** 这个年代的游戏大多只有 32 位（armv7/armv7s），iOS 11 起就不支持了；touchHLE 模拟器只支持 iPhone OS 2–3 的游戏。所以复刻是现实的路线。
- 图片里画死的小字，AI 能修，但最好还是用真字体重新排。
- 老游戏是 3:2 / 16:9 / 4:3，现在的手机是 19.5:9，背景两边需要补（加边框、平铺，或者用 AI 扩图，后者要单独确认）。
- 3D 游戏（Unity 3D 模型、Unreal 3 比如 Infinity Blade）只处理贴图，不处理模型。
- 素材版权归原开发商：自己玩没问题，公开发布复刻版需要授权。

## 参考和致谢

这个 skill 是看完下面几个项目之后，专门针对 iOS 5–7 时代重新写的：

- [texup](https://github.com/veryCoolTimo/texture-auto-upscaler)（MIT）：分块推理、fp16 回退、按类别选模型、先对比再应用的流程
- [pixel-art-scaler](https://skills.sh/erichowens/some_claude_skills/pixel-art-scaler)：像素风不用 AI、用 EPX/Scale2x 的思路（它的 hqx/xBR 脚本仓库里其实没有，这里自己实现了 Scale2x/3x）
- [Game Assets Enhancer](https://github.com/adamlyttleapps/claude-skill-game-assets-enhancer)（MIT）：「风格锚点」思路，用于背景扩图那一步
- [universal-modder](https://github.com/rehan-remade/universal-modder)（MIT）：「先从游戏本身学格式，再转换」以及 UnityPy 的经验

测试：`python -m pytest tests`（会自动生成一个模拟的 iOS 6 游戏 IPA，覆盖所有格式）。
