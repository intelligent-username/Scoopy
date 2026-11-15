"""Build app icon assets from the source art in src/imgs/.

Run once with any Python that has Pillow (system Python is fine; this is a
build step, not a runtime dependency):
    python scripts/make_icon.py

Outputs (committed):
    src/scoopy/assets/app-icon.png   256px, background made transparent
    src/scoopy/assets/app-icon.ico   multi-size icon for window/taskbar/exe
"""
import os

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'src', 'imgs', 'icon.png')
ASSETS = os.path.join(ROOT, 'src', 'scoopy', 'assets')

# Includes the 20/40/96 frames Windows asks for at 125%/250% display scaling.
# Without them the shell scales a neighbouring frame and the titlebar icon
# looks soft on high-DPI screens.
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)

image = Image.open(SOURCE).convert('RGBA')

# Make a near-white background transparent so the glyph works on any
# titlebar/taskbar color instead of showing a white tile.
pixels = image.load()
for y in range(image.height):
    for x in range(image.width):
        r, g, b, a = pixels[x, y]
        if r > 242 and g > 242 and b > 242:
            pixels[x, y] = (r, g, b, 0)


def small_size_boost(size: int) -> float:
    """How much to thicken strokes after downscaling thin line art.

    At titlebar sizes the thin outline averages down to partially transparent
    pixels, which reads as a gray smudge. Restoring the alpha keeps the glyph
    crisp without touching the hue (so it still works in the taskbar).
    """
    if size >= 48:
        return 1.0
    return {16: 2.4, 20: 2.2, 24: 2.0, 32: 1.7, 40: 1.5}.get(size, 1.5)


def frame_for(size: int) -> Image.Image:
    frame = image.resize((size, size), Image.LANCZOS)
    boost = small_size_boost(size)
    if boost == 1.0:
        return frame
    red, green, blue, alpha = frame.split()
    alpha = alpha.point(lambda value: min(255, round(value * boost)))
    return Image.merge('RGBA', (red, green, blue, alpha))


os.makedirs(ASSETS, exist_ok=True)

png_path = os.path.join(ASSETS, 'app-icon.png')
image.resize((256, 256), Image.LANCZOS).save(png_path)
print('wrote', png_path)

# The ICO writer only considers sizes up to the size of the image it is given,
# so hand it the 256px frame and supply the smaller frames as append_images.
frames = {size: frame_for(size) for size in ICO_SIZES}
ico_path = os.path.join(ASSETS, 'app-icon.ico')
frames[256].save(
    ico_path, format='ICO',
    sizes=[(size, size) for size in ICO_SIZES],
    append_images=[frames[size] for size in ICO_SIZES if size != 256],
)
print('wrote', ico_path)
