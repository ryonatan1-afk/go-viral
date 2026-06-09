"""
health_check.py
===============
Validates the environment before a render run: checks ffmpeg, fonts, audio assets.
Exits 0 if healthy, 1 if anything critical is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Resolve assets relative to TEMPLATES_DIR (worker mounts at /templates); fall back
# to ./templates for local runs from the repo root.
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "./templates")

REQUIRED_AUDIO = [
    f"{TEMPLATES_DIR}/audio/beat.wav",
    f"{TEMPLATES_DIR}/audio/ding.wav",
    f"{TEMPLATES_DIR}/audio/whoosh.wav",
    f"{TEMPLATES_DIR}/audio/scratch.wav",
    f"{TEMPLATES_DIR}/audio/sting.wav",
    f"{TEMPLATES_DIR}/audio/outro.wav",
]
REQUIRED_FONT = f"{TEMPLATES_DIR}/fonts/bold.ttf"
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")


def check_ffmpeg() -> dict:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"ok": False, "detail": "ffmpeg not found in PATH"}
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        version_line = result.stdout.splitlines()[0] if result.stdout else "unknown"
        return {"ok": True, "detail": version_line}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


def check_asset(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return {"ok": True, "size_bytes": p.stat().st_size}
    return {"ok": False, "detail": f"missing: {path}"}


def main() -> None:
    report = {
        "ffmpeg": check_ffmpeg(),
        "font": check_asset(REQUIRED_FONT),
        "audio": {a: check_asset(a) for a in REQUIRED_AUDIO},
        "output_dir": {"ok": True},
    }

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    all_ok = (
        report["ffmpeg"]["ok"]
        and report["font"]["ok"]
        and all(v["ok"] for v in report["audio"].values())
    )

    report["healthy"] = all_ok
    print(json.dumps(report, indent=2))
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
