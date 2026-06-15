"""One-time script — generates static/icon.png for dk 投資雷達 (PWA icon)."""
from PIL import Image, ImageDraw
import os

SIZE = 512
NAVY  = (18, 38, 74, 255)
BLUE  = (59, 130, 246, 255)
BLUEA = (59, 130, 246, 60)
GREEN = (16, 185, 129, 255)
WHITE = (255, 255, 255, 255)

img  = Image.new("RGBA", (SIZE, SIZE), NAVY)
draw = ImageDraw.Draw(img)

# Rounded background square
draw.rounded_rectangle([0, 0, SIZE, SIZE], radius=80, fill=NAVY)

cx, cy = SIZE // 2, SIZE // 2

# Radar sweep rings
for r in (210, 150, 90):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLUEA, width=4)

# Radar sweep wedge (translucent quarter)
draw.pieslice([cx - 210, cy - 210, cx + 210, cy + 210], start=-90, end=0, fill=BLUEA)

# Crosshair lines
draw.line([(cx - 210, cy), (cx + 210, cy)], fill=BLUEA, width=3)
draw.line([(cx, cy - 210), (cx, cy + 210)], fill=BLUEA, width=3)

# Sweep needle (bright)
draw.line([(cx, cy), (cx + 195, cy - 130)], fill=BLUE, width=10)

# Center dot
r = 14
draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)

# Blips (detected stocks)
for bx, by, col in [(cx + 95, cy + 60, GREEN), (cx - 70, cy - 90, WHITE), (cx - 110, cy + 110, GREEN)]:
    r = 11
    draw.ellipse([bx - r, by - r, bx + r, by + r], fill=col)

os.makedirs("static", exist_ok=True)
img.save("static/icon.png")
print("static/icon.png written.")
