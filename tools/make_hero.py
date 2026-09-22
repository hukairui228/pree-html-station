#!/usr/bin/env python3
"""Turn the raw window capture into a polished README hero image."""
from PIL import Image, ImageDraw, ImageFilter

SRC = "/tmp/shot_raw.png"
OUT = "/Users/a1-6/WorkBuddy/2026-09-21-12-25-10/html-editor-app/screenshot.png"

src = Image.open(SRC).convert("RGBA")
w, h = src.size
target_w = 1280
src = src.resize((target_w, round(h * target_w / w)), Image.LANCZOS)

radius = 18
mask = Image.new("L", src.size, 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, src.size[0] - 1, src.size[1] - 1], radius=radius, fill=255)

pad = 56
shadow_lift = 16
canvas = Image.new("RGBA", (src.width + pad * 2, src.height + pad * 2), (0, 0, 0, 0))

# soft drop shadow
shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
sd = ImageDraw.Draw(shadow)
sd.rounded_rectangle(
    [pad, pad + shadow_lift, pad + src.width, pad + src.height + shadow_lift],
    radius=radius, fill=(0, 0, 0, 170),
)
shadow = shadow.filter(ImageFilter.GaussianBlur(22))
canvas = Image.alpha_composite(canvas, shadow)

canvas.paste(src, (pad, pad), mask)
canvas.save(OUT)
print("hero written:", canvas.size)
