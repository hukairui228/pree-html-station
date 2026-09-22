#!/usr/bin/env python3
"""Generate macOS app icon for HTML 编辑器 (1024x1024, Big Sur tile spec)."""
from PIL import Image, ImageDraw, ImageFont

S = 1024
R = 190
M = 92  # tile margin: content 840x840

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# --- gradient tile ---
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
c1 = (30, 41, 59)    # #1e293b
c2 = (15, 23, 42)    # #0f172a
c3 = (11, 17, 32)    # #0b1120
for y in range(S):
    t = y / S
    if t < 0.55:
        k = t / 0.55
        col = tuple(int(c1[i] + (c2[i] - c1[i]) * k) for i in range(3))
    else:
        k = (t - 0.55) / 0.45
        col = tuple(int(c2[i] + (c3[i] - c2[i]) * k) for i in range(3))
    gd.line([(0, y), (S, y)], fill=col + (255,))

mask = Image.new("L", (S, S), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([M, M, S - M, S - M], radius=R, fill=255)
img.paste(grad, (0, 0), mask)

# --- inner highlight (subtle top edge) ---
hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
hd = ImageDraw.Draw(hl)
hd.rounded_rectangle([M + 3, M + 3, S - M - 3, M + 90], radius=R - 40, fill=(255, 255, 255, 14))
img = Image.alpha_composite(img, hl)

draw = ImageDraw.Draw(img)

# --- glyph </> ---
font = None
for path, idx in [("/System/Library/Fonts/Menlo.ttc", 1), ("/System/Library/Fonts/Menlo.ttc", 0),
                  ("/System/Library/Fonts/Helvetica.ttc", 1)]:
    try:
        f = ImageFont.truetype(path, 330, index=idx)
        bbox = draw.textbbox((0, 0), "</>", font=f)
        if bbox[2] - bbox[0] > 50:
            font = f
            break
    except Exception:
        continue
if font is None:
    font = ImageFont.load_default()

cx, cy = S / 2, S / 2 - 55
text = "</>"
# measure full string for centering
tb = draw.textbbox((0, 0), text, font=font)
w = tb[2] - tb[0]
h = tb[3] - tb[1]
x0 = cx - w / 2 - tb[0]
y0 = cy - h / 2 - tb[1]
# "<" sky blue
draw.text((x0, y0), "<", font=font, fill=(56, 189, 248, 255))
# "/" white — advance width of "<"
wa = draw.textlength("<", font=font)
draw.text((x0 + wa, y0), "/", font=font, fill=(248, 250, 252, 255))
# ">" sky blue
wb = draw.textlength("/", font=font)
draw.text((x0 + wa + wb, y0), ">", font=font, fill=(56, 189, 248, 255))

# --- pen bar ---
py = 830
pd = ImageDraw.Draw(img)
pd.rounded_rectangle([252, py, 432, py + 16], radius=8, fill=(56, 189, 248, 255))
pd.rounded_rectangle([432, py, 592, py + 16], radius=8, fill=(129, 140, 248, 255))

img.save("icon_raw.png")
print("icon_raw.png written", img.size)
