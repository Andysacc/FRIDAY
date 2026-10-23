"""Generate deterministic FRIDAY PWA icons from simple vector-like primitives."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "app" / "static" / "icons"
CANVAS = 1024


def build_icon() -> Image.Image:
    image = Image.new("RGB", (CANVAS, CANVAS), "#02070d")
    pixels = image.load()
    center = CANVAS / 2
    for y in range(CANVAS):
        for x in range(CANVAS):
            distance = min(1.0, ((x - center) ** 2 + (y - center * 0.86) ** 2) ** 0.5 / 720)
            strength = (1 - distance) ** 2
            pixels[x, y] = (
                int(2 + 8 * strength),
                int(7 + 34 * strength),
                int(13 + 55 * strength),
            )

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 1021, 1021), radius=220, outline="#0b3857", width=6)
    draw.ellipse((184, 184, 840, 840), outline="#0b3857", width=8)
    draw.ellipse((124, 124, 900, 900), outline="#0a3858", width=3)

    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    arc_box = (184, 184, 840, 840)
    glow_draw.arc(arc_box, 132, 320, fill="#149cff", width=34)
    glow_draw.arc(arc_box, 326, 418, fill="#38f0bd", width=22)
    glow_draw.ellipse((230, 630, 286, 686), fill="#61d8ff")
    glow_draw.ellipse((726, 238, 770, 282), fill="#149cff")
    glow_draw.ellipse((688, 758, 724, 794), fill="#38f0bd")
    image = Image.alpha_composite(image.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(22)))
    image = Image.alpha_composite(image, glow)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((404, 306, 680, 378), radius=8, fill="#eaf8ff")
    draw.rounded_rectangle((404, 306, 486, 744), radius=8, fill="#eaf8ff")
    draw.rounded_rectangle((404, 478, 648, 550), radius=8, fill="#eaf8ff")
    draw.rounded_rectangle((404, 306, 680, 320), radius=7, fill="#61d8ff")

    return image.convert("RGB")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    source = build_icon()
    for size in (180, 192, 512):
        resized = source.resize((size, size), Image.Resampling.LANCZOS)
        resized.save(OUTPUT / f"friday-{size}.png", optimize=True)
    source.resize((512, 512), Image.Resampling.LANCZOS).save(
        OUTPUT / "friday-maskable-512.png",
        optimize=True,
    )


if __name__ == "__main__":
    main()
