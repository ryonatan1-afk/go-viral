"""
generate_content.py
===================
Daily content brain: uses Claude to invent 4 band-name visual puzzles,
then generates images for each via fal.ai FLUX.1[dev].

Outputs a JSON file (content_YYYY-MM-DD.json) consumed by the n8n workflow.

Usage:
    python scripts/generate_content.py --output-dir ./output/content
    python scripts/generate_content.py --bands "Radiohead" "The Beatles" "Coldplay" "Nirvana"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import httpx
import structlog
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
log = structlog.get_logger(__name__)

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
FAL_KEY = os.environ["FAL_KEY"]

FAL_FLUX_URL = "https://fal.run/fal-ai/flux/dev"
FAL_HEADERS = {
    "Authorization": f"Key {FAL_KEY}",
    "Content-Type": "application/json",
}

# Image dimensions: tall portrait for 9:16 canvas (fal.ai supports these presets)
IMAGE_WIDTH = 576
IMAGE_HEIGHT = 1024


BAND_PROMPT = """\
You are a creative director for a viral social media account called "Guess the Band".
Each day you pick 4 rock/pop bands whose names can be depicted as a literal visual scene.

Rules:
- Choose bands with visually punnable names (e.g. Radiohead, The Beatles, Gorillaz, Imagine Dragons, Arctic Monkeys, Red Hot Chili Peppers, Foo Fighters, Green Day, Nine Inch Nails, Pearl Jam, Stone Temple Pilots, Soundgarden, Smashing Pumpkins, The Killers, Queens of the Stone Age, System of a Down, Alice in Chains, Black Sabbath, Iron Maiden, Judas Priest, Guns N Roses, Def Leppard, White Stripes, Black Keys, etc.)
- Avoid bands used in previous sessions if possible
- Mix difficulty: 2 easy (mainstream), 1 medium, 1 hard

ON-SCREEN TEXT RULES (critical — text is rendered in a bold condensed font):
- NO emoji and NO special symbols anywhere. Plain ASCII letters/numbers only.
- riddle_title: SHORT and punchy, max ~18 characters, uppercase friendly (e.g. "WHAT BAND IS THIS?", "NAME THIS BAND", "GUESS IT").
- engagement_caption: SHORT, max ~22 characters (e.g. "COMMENT YOUR GUESS", "DROP YOUR ANSWER").
- hook: ONE short scroll-stopping line for the intro card, max ~24 characters (e.g. "99% CANT GET ALL 4", "ONLY REAL FANS WIN").
- cta: ONE short outro line, max ~28 characters (e.g. "FOLLOW FOR DAILY PUZZLES").

Return ONLY valid JSON, no markdown, no explanation:
{{
  "hook": "Short intro hook line, no emoji",
  "cta": "Short outro call-to-action, no emoji",
  "bands": [
    {{
      "name": "Band Name",
      "difficulty": "easy|medium|hard",
      "visual_concept": "One sentence describing the literal visual pun",
      "image_prompt": "Detailed FLUX image generation prompt. Style: bold graphic illustration, vibrant colors, 9:16 vertical format, no text, no words, cinematic lighting. Describe exactly what is shown.",
      "riddle_title": "Short teaser title, no emoji",
      "engagement_caption": "Short engagement text, no emoji"
    }}
  ]
}}

Today's date: {today}
Previously used bands (avoid): {used_bands}
"""

# Sort order so puzzles escalate easy -> hard across the video
DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}


USED_BANDS_CSV = "used_bands.csv"


def _norm_band(name: str) -> str:
    """Normalise a band name for duplicate comparison (case/punct/spacing-insensitive)."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def load_used_bands(output_dir: Path) -> list[str]:
    """
    Build the avoid-list from the persistent CSV log (source of truth),
    plus any past content_*.json files still on disk (belt and suspenders).
    Returns de-duplicated original names, most-recent last.
    """
    used: list[str] = []

    csv_path = output_dir / USED_BANDS_CSV
    if csv_path.exists():
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row.get("band"):
                        used.append(row["band"])
        except Exception as e:
            log.warning("used_csv_read_failed", error=str(e))

    for f in output_dir.glob("content_*.json"):
        try:
            data = json.loads(f.read_text())
            used.extend(b["name"] for b in data.get("bands", []))
        except Exception:
            pass

    # De-dup preserving order
    seen, out = set(), []
    for n in used:
        k = _norm_band(n)
        if k and k not in seen:
            seen.add(k)
            out.append(n)
    return out


def log_bands_csv(output_dir: Path, date_str: str, bands: list[dict]) -> None:
    """Append the day's chosen bands to the persistent CSV log."""
    csv_path = output_dir / USED_BANDS_CSV
    is_new = not csv_path.exists()
    try:
        with open(csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["date", "band", "difficulty"])
            for b in bands:
                w.writerow([date_str, b.get("name", ""), b.get("difficulty", "")])
        log.info("bands_logged_csv", path=str(csv_path), count=len(bands))
    except Exception as e:
        log.warning("used_csv_write_failed", error=str(e))


def _parse_claude_json(raw: str) -> dict:
    """Parse Claude's JSON reply, tolerating markdown code fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def generate_band_ideas(output_dir: Path, override_bands: list[str] | None = None) -> dict:
    if override_bands:
        log.info("using_override_bands", bands=override_bands)
        # Build minimal band dicts from overrides; still call Claude for prompts
        client = Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": f"""Generate image prompts for these specific bands: {override_bands}

ON-SCREEN TEXT RULES: NO emoji or special symbols, plain ASCII only.
riddle_title max ~18 chars, engagement_caption max ~22 chars,
hook max ~24 chars, cta max ~28 chars.

Return ONLY valid JSON:
{{
  "hook": "Short intro hook, no emoji",
  "cta": "Short outro CTA, no emoji",
  "bands": [
    {{
      "name": "Band Name",
      "difficulty": "easy",
      "visual_concept": "one sentence",
      "image_prompt": "Detailed FLUX prompt. Style: bold graphic illustration, vibrant colors, 9:16 vertical format, no text, no words, cinematic lighting.",
      "riddle_title": "Short title, no emoji",
      "engagement_caption": "Short caption, no emoji"
    }}
  ]
}}"""
            }],
        )
        parsed = _parse_claude_json(resp.content[0].text)
        bands = parsed["bands"]
        bands.sort(key=lambda b: DIFFICULTY_RANK.get(str(b.get("difficulty", "")).lower(), 1))
        return {
            "bands": bands,
            "hook": parsed.get("hook", "GUESS THE BAND"),
            "cta": parsed.get("cta", "FOLLOW FOR DAILY PUZZLES"),
        }

    # ── Daily selection: avoid previously-used bands, retry if Claude repeats ──
    used = load_used_bands(output_dir)
    client = Anthropic(api_key=ANTHROPIC_KEY)
    bands, hook, cta = [], "GUESS THE BAND", "FOLLOW FOR DAILY PUZZLES"

    for attempt in range(1, 4):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": BAND_PROMPT.format(
                    today=date.today().isoformat(),
                    used_bands=", ".join(used) if used else "none",
                ),
            }],
        )
        parsed = _parse_claude_json(resp.content[0].text)
        bands = parsed["bands"]
        hook = parsed.get("hook", "GUESS THE BAND")
        cta = parsed.get("cta", "FOLLOW FOR DAILY PUZZLES")

        used_norm = {_norm_band(u) for u in used}
        seen, dups = set(), []
        for b in bands:
            k = _norm_band(b["name"])
            if k in used_norm or k in seen:
                dups.append(b["name"])
            seen.add(k)

        if not dups:
            break
        log.warning("duplicate_bands_retry", attempt=attempt, dups=dups)
        used = used + dups  # strengthen the avoid list for the next attempt
    else:
        log.warning("duplicate_bands_unresolved", names=[b["name"] for b in bands])

    # Sort puzzles to escalate easy -> hard across the video
    bands.sort(key=lambda b: DIFFICULTY_RANK.get(str(b.get("difficulty", "")).lower(), 1))
    log.info("bands_generated", count=len(bands), names=[b["name"] for b in bands])
    return {"bands": bands, "hook": hook, "cta": cta}


def generate_image(prompt: str, band_name: str) -> str:
    """Call fal.ai FLUX.1[dev] and return local file path."""
    log.info("generating_image", band=band_name)
    payload = {
        "prompt": prompt,
        "image_size": {"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT},
        "num_inference_steps": 28,
        "guidance_scale": 3.5,
        "num_images": 1,
        "enable_safety_checker": True,
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(FAL_FLUX_URL, headers=FAL_HEADERS, json=payload)
        resp.raise_for_status()
        data = resp.json()

    image_url = data["images"][0]["url"]
    log.info("image_generated", band=band_name, url=image_url[:60])
    return image_url


def download_image(url: str, dest: Path) -> Path:
    with httpx.Client(timeout=60) as client:
        r = client.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    log.info("image_downloaded", path=str(dest), size_kb=len(r.content) // 1024)
    return dest


def run(output_dir: Path, override_bands: list[str] | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    content_file = output_dir / f"content_{today}.json"

    # Handle __REGEN__ — regenerate only specific band images with a fresh concept
    if content_file.exists() and override_bands and any(b == "__REGEN__" for b in override_bands):
        log.info("regen_mode", path=str(content_file))
        content = json.loads(content_file.read_text())
        client = Anthropic(api_key=ANTHROPIC_KEY)
        for i, flag in enumerate(override_bands):
            if flag == "__REGEN__" and i < len(content["bands"]):
                band = content["bands"][i]
                prev_concept = band.get("visual_concept", "")
                prev_prompt = band.get("image_prompt", "")
                # Ask Claude for a completely different visual concept
                resp = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=500,
                    messages=[{"role": "user", "content": f"""The band "{band['name']}" was visually depicted as: "{prev_concept}"
The user rejected this image and wants something completely different.

Generate a NEW visual concept and image prompt for "{band['name']}" that is VERY different from the previous one.

Return ONLY valid JSON:
{{
  "visual_concept": "one sentence describing the new literal visual pun",
  "image_prompt": "Detailed FLUX prompt. Style: bold graphic illustration, vibrant colors, 9:16 vertical format, no text, no words, cinematic lighting."
}}"""}],
                )
                raw = resp.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                new_data = json.loads(raw.strip())
                band["visual_concept"] = new_data["visual_concept"]
                band["image_prompt"] = new_data["image_prompt"]
                img_path = Path(band["image_path"])
                if img_path.exists():
                    img_path.unlink()
                url = generate_image(band["image_prompt"], band["name"])
                download_image(url, img_path)
                log.info("regen_complete", band=band["name"], index=i, new_concept=new_data["visual_concept"])
        content_file.write_text(json.dumps(content))
        return content

    if content_file.exists():
        log.info("content_exists_loading", path=str(content_file))
        return json.loads(content_file.read_text())

    ideas = generate_band_ideas(output_dir, override_bands)
    bands = ideas["bands"]

    # Generate all 4 images (sequentially to respect fal.ai rate limits)
    for i, band in enumerate(bands):
        img_path = output_dir / f"{today}_band_{i+1}.png"
        if not img_path.exists():
            url = generate_image(band["image_prompt"], band["name"])
            download_image(url, img_path)
            time.sleep(1)  # polite rate limiting
        else:
            log.info("image_cached", path=str(img_path))
        band["image_path"] = str(img_path)
        band["image_index"] = i + 1

    result = {
        "date": today,
        "hook": ideas["hook"],
        "cta": ideas["cta"],
        "bands": bands,
        "image_paths": [b["image_path"] for b in bands],
        "titles": [b["riddle_title"] for b in bands],
        "captions": [b["engagement_caption"] for b in bands],
        "band_names": [b["name"] for b in bands],
        "difficulties": [b.get("difficulty", "") for b in bands],
    }
    content_file.write_text(json.dumps(result, indent=2))
    log.info("content_saved", path=str(content_file))

    # Log the chosen bands to the persistent CSV so future runs avoid repeats
    log_bands_csv(output_dir, today, bands)

    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Generate daily band puzzle content")
    p.add_argument("--output-dir", default="./output/content")
    p.add_argument("--bands", nargs="+", help="Override band names (skips Claude selection)")
    p.add_argument("--json-out", action="store_true", help="Print result JSON to stdout")
    args = p.parse_args()

    result = run(Path(args.output_dir), args.bands)
    if args.json_out:
        print(json.dumps(result))
    else:
        print(f"\nGenerated content for {result['date']}:")
        for b in result["bands"]:
            print(f"  [{b['difficulty']:6}] {b['name']} → {b['image_path']}")


if __name__ == "__main__":
    main()
