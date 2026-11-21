"""Render CrowKit PNG and ICO assets from a deterministic vector-like design."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app" / "web" / "assets"
SIZE = 1024


def lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main() -> None:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gradient = Image.new("RGBA", (SIZE, SIZE))
    pixels = gradient.load()
    starts = [(0.0, (11, 24, 48)), (0.58, (18, 61, 120)), (1.0, (73, 42, 158))]
    for y in range(SIZE):
        for x in range(SIZE):
            t = max(0.0, min(1.0, (x + y) / (2 * (SIZE - 1))))
            for index in range(len(starts) - 1):
                a, color_a = starts[index]
                b, color_b = starts[index + 1]
                if a <= t <= b:
                    pixels[x, y] = lerp(
                        color_a, color_b, (t - a) / (b - a)
                    ) + (255,)
                    break
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (32, 32, SIZE - 32, SIZE - 32), radius=232, fill=255
    )
    image.alpha_composite(Image.composite(gradient, Image.new("RGBA", image.size), mask))

    draw = ImageDraw.Draw(image)
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((80, -40, 760, 620), fill=(83, 181, 255, 45))
    image.alpha_composite(Image.composite(glow, Image.new("RGBA", image.size), mask))

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (368, 326, 656, 380), radius=27, outline=(196, 224, 255, 255), width=54
    )
    draw.rounded_rectangle(
        (224, 376, 800, 860), radius=72, fill=(249, 252, 255, 255)
    )
    draw.rounded_rectangle(
        (224, 376, 800, 520), radius=72, fill=(234, 243, 255, 255)
    )
    draw.rounded_rectangle((224, 472, 800, 520), radius=0, fill=(234, 243, 255, 255))
    draw.rounded_rectangle((312, 520, 712, 534), radius=7, fill=(200, 218, 245, 255))

    font_path = Path(r"C:\Windows\Fonts\segoeuib.ttf")
    if not font_path.exists():
        font_path = Path(r"C:\Windows\Fonts\arialbd.ttf")
    font = ImageFont.truetype(str(font_path), 218)
    text = "CK"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    width, height = right - left, bottom - top
    x = (SIZE - width) / 2 - left
    y = 744 - height / 2 - top

    mark = Image.new("RGBA", image.size, (0, 0, 0, 0))
    mark_draw = ImageDraw.Draw(mark)
    mark_draw.text((x, y), text, font=font, fill=(105, 105, 255, 255))
    mark_mask = mark.getchannel("A")
    mark_gradient = Image.new("RGBA", image.size)
    mark_pixels = mark_gradient.load()
    for py in range(SIZE):
        for px in range(SIZE):
            t = max(0.0, min(1.0, (px + py - 600) / 850))
            mark_pixels[px, py] = lerp(
                (70, 184, 255), (116, 86, 255), t
            ) + (255,)
    image.alpha_composite(Image.composite(mark_gradient, Image.new("RGBA", image.size), mark_mask))

    OUT.mkdir(parents=True, exist_ok=True)
    image.resize((512, 512), Image.Resampling.LANCZOS).save(OUT / "crowkit.png")
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512):
        image.resize((size, size), Image.Resampling.LANCZOS).save(
            OUT / f"crowkit-{size}.png"
        )
    image.save(
        OUT / "crowkit.ico",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
               (48, 48), (64, 64), (128, 128), (256, 256)],
    )


if __name__ == "__main__":
    main()
