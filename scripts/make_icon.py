"""Build app icon assets from the source art in src/imgs/.

Run once with any Python that has Pillow (system Python is fine; this is a
build step, not a runtime dependency):
    python scripts/make_icon.py

Outputs (committed):
    src/scoopy/assets/app-icon.png   256px, background made transparent
    src/scoopy/assets/app-icon.ico   multi-size icon for PyInstaller exe builds
"""
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'src', 'imgs', 'icon.png')
ASSETS = os.path.join(ROOT, 'src', 'scoopy', 'assets')

image = Image.open(SOURCE).convert('RGBA')

# Make a near-white background transparent so the glyph works on any
# titlebar/taskbar color instead of showing a white tile.
pixels = image.load()
for y in range(image.height):
    for x in range(image.width):
        r, g, b, a = pixels[x, y]
        if r > 242 and g > 242 and b > 242:
            pixels[x, y] = (r, g, b, 0)

os.makedirs(ASSETS, exist_ok=True)

png_path = os.path.join(ASSETS, 'app-icon.png')
image.resize((256, 256), Image.LANCZOS).save(png_path)
print('wrote', png_path)

ico_path = os.path.join(ASSETS, 'app-icon.ico')
image.save(ico_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                            (64, 64), (128, 128), (256, 256)])
print('wrote', ico_path)
