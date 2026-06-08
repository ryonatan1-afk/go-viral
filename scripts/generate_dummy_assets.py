"""
generate_dummy_assets.py
========================
Creates placeholder test images and silent audio stubs so the pipeline can be
exercised without real content. Run once to bootstrap a local test run.

    python scripts/generate_dummy_assets.py
"""

import os
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
    import numpy as np
    from pydub import AudioSegment
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False
    print("Install requirements.txt first: pip install -r requirements.txt")
    raise SystemExit(1)

COLORS = [(220, 50, 50), (50, 180, 220), (80, 200, 80), (200, 150, 30),
          (150, 80, 200), (40, 120, 160)]
LABELS = ["Puzzle 1", "Puzzle 2", "Puzzle 3", "Puzzle 4", "Puzzle 5", "Puzzle 6"]
OUT_IMAGES = Path("./test_assets/images")
OUT_AUDIO = Path("./templates/audio")
OUT_IMAGES.mkdir(parents=True, exist_ok=True)
OUT_AUDIO.mkdir(parents=True, exist_ok=True)


def make_test_image(idx: int) -> str:
    img = Image.new("RGB", (1080, 1920), COLORS[idx])
    draw = ImageDraw.Draw(img)
    draw.rectangle([80, 80, 1000, 400], fill=(0, 0, 0, 128))
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 120)
    except Exception:
        font = ImageFont.load_default()
    draw.text((540, 220), LABELS[idx], fill=(255, 255, 255), font=font, anchor="mm")
    path = str(OUT_IMAGES / f"puzzle_{idx + 1}.png")
    img.save(path)
    print(f"  Created: {path}")
    return path


def make_silent_audio(filename: str, duration_ms: int = 5000) -> None:
    path = OUT_AUDIO / filename
    if path.exists():
        print(f"  Skipped (exists): {path}")
        return
    silence = AudioSegment.silent(duration=duration_ms)
    silence.export(str(path), format="mp3")
    print(f"  Created: {path}")


def main() -> None:
    print("Generating test images…")
    images = [make_test_image(i) for i in range(6)]

    print("\nReady. Test run:")
    print(
        f"python scripts/generate_video.py \\\n"
        f"  --images {' '.join(images)} \\\n"
        f"  --titles 'Riddle 1' 'Riddle 2' 'Riddle 3' 'Riddle 4' 'Riddle 5' 'Riddle 6' \\\n"
        f"  --captions 'Drop your guess!' 'Comment below!' 'Can you solve it?' 'Reply now!' 'Name it!' 'Last one!' \\\n"
        f"  --output-dir ./output"
    )


if __name__ == "__main__":
    main()
