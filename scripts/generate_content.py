"""
generate_content.py
===================
Daily content brain: uses Claude to invent 6 band-name visual puzzles,
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

import experiment

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
Each day you pick 6 rock/pop bands whose names can be depicted as a literal visual scene.

Rules:
- Choose bands with visually punnable names — names that depict a literal scene
  (a radio for a head, a beetle, fighting foos, etc.). The point is the visual pun.
- CRITICAL — NEVER reuse a band: do NOT pick ANY band that appears in the
  "Previously used bands" list below. Those are permanently retired. Every band
  you return must be brand new and absent from that list. This rule is absolute.
- Mix difficulty across the 6 bands: 2 easy (mainstream), 1 medium, 3 hard

ON-SCREEN TEXT RULES (critical — text is rendered in a bold condensed font):
- NO emoji and NO special symbols anywhere. Plain ASCII letters/numbers only.
- riddle_title: SHORT and punchy, max ~18 characters, uppercase friendly (e.g. "WHAT BAND IS THIS?", "NAME THIS BAND", "GUESS IT").
- engagement_caption: SHORT, max ~22 characters (e.g. "COMMENT YOUR GUESS", "DROP YOUR ANSWER").
- hook: ONE short scroll-stopping line for the intro card, max ~24 characters (e.g. "99% CANT GET ALL 6", "ONLY REAL FANS WIN").
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

    # ── Daily selection: accumulate 6 UNIQUE bands, hard-filtering anything in the
    # avoid-list or already chosen. Claude has historically re-suggested retired
    # bands (its own prompt examples), so the filter — not Claude — is the guarantee.
    used = load_used_bands(output_dir)
    avoid_norm = {_norm_band(u) for u in used}
    client = Anthropic(api_key=ANTHROPIC_KEY)

    collected: list[dict] = []
    collected_norm: set[str] = set()
    hook, cta = "GUESS THE BAND", "FOLLOW FOR DAILY PUZZLES"
    NEEDED = 6

    for attempt in range(1, 6):
        if len(collected) >= NEEDED:
            break
        # Exclude both history and what we've already accepted this run.
        exclude = sorted(set(used) | {b["name"] for b in collected})
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": BAND_PROMPT.format(
                    today=date.today().isoformat(),
                    used_bands=", ".join(exclude) if exclude else "none",
                ),
            }],
        )
        parsed = _parse_claude_json(resp.content[0].text)
        if attempt == 1:
            hook = parsed.get("hook", hook)
            cta = parsed.get("cta", cta)

        rejected = []
        for b in parsed.get("bands", []):
            k = _norm_band(b.get("name", ""))
            if not k or k in avoid_norm or k in collected_norm:
                if k:
                    rejected.append(b.get("name", ""))
                continue
            collected.append(b)
            collected_norm.add(k)
            if len(collected) >= NEEDED:
                break
        if rejected:
            log.warning("filtered_repeat_bands", attempt=attempt, rejected=rejected)

    bands = collected[:NEEDED]
    if len(bands) < NEEDED:
        log.warning("insufficient_unique_bands", got=len(bands), needed=NEEDED)

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


def _pick_replacement_band(client: Anthropic, avoid_norm: set[str],
                           difficulty: str) -> dict:
    """Ask Claude for ONE fresh punnable band (preferably of `difficulty`) that is
    not in avoid_norm. Filters deterministically; retries up to 4 times."""
    prompt = (
        'Generate ONE rock/pop band with a visually punnable name for a '
        '"Guess the Band" puzzle. Difficulty: {difficulty}.\n'
        'ABSOLUTE RULE: do NOT pick any band in this retired list: {avoid}\n'
        'ON-SCREEN TEXT: no emoji/special symbols, plain ASCII; riddle_title <=18 '
        'chars, engagement_caption <=22 chars.\n'
        'Return ONLY JSON: {{"name":"","difficulty":"{difficulty}","visual_concept":'
        '"one sentence literal visual pun","image_prompt":"Detailed FLUX prompt. '
        'Style: bold graphic illustration, vibrant colors, 9:16 vertical, no text, '
        'no words, cinematic lighting.","riddle_title":"","engagement_caption":""}}'
    )
    avoid_names = ", ".join(sorted(avoid_norm))[:1500]
    for _ in range(4):
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            messages=[{"role": "user", "content": prompt.format(
                difficulty=difficulty or "medium", avoid=avoid_names)}],
        )
        band = _parse_claude_json(resp.content[0].text)
        if _norm_band(band.get("name", "")) not in avoid_norm:
            return band
        log.warning("replacement_repeat_retry", band=band.get("name"))
    return band  # last attempt, even if imperfect


def run(output_dir: Path, override_bands: list[str] | None = None) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    content_file = output_dir / f"content_{today}.json"

    # Handle __REPLACE__ — swap specific slots for a brand-new band (not just a new
    # image). Updates the band dict AND the parallel arrays the renderer reads.
    if content_file.exists() and override_bands and any(b == "__REPLACE__" for b in override_bands):
        log.info("replace_mode", path=str(content_file))
        content = json.loads(content_file.read_text())
        avoid = {_norm_band(x) for x in load_used_bands(output_dir)}
        avoid |= {_norm_band(b["name"]) for b in content["bands"]}
        client = Anthropic(api_key=ANTHROPIC_KEY)
        for i, flag in enumerate(override_bands):
            if flag != "__REPLACE__" or i >= len(content["bands"]):
                continue
            old = content["bands"][i]
            newb = _pick_replacement_band(client, avoid, old.get("difficulty", "medium"))
            avoid.add(_norm_band(newb["name"]))
            img_path = Path(old.get("image_path") or
                            output_dir / f"{content['date']}_band_{i+1}.png")
            if img_path.exists():
                img_path.unlink()
            url = generate_image(newb["image_prompt"], newb["name"])
            download_image(url, img_path)
            newb["image_path"] = str(img_path)
            newb["image_index"] = i + 1
            content["bands"][i] = newb
            # Keep the renderer's parallel arrays in sync.
            for key, val in [("titles", newb.get("riddle_title", "")),
                             ("captions", newb.get("engagement_caption", "")),
                             ("band_names", newb["name"]),
                             ("difficulties", newb.get("difficulty", "")),
                             ("image_paths", str(img_path))]:
                if isinstance(content.get(key), list) and i < len(content[key]):
                    content[key][i] = val
            log_bands_csv(output_dir, content["date"], [newb])
            log.info("replace_complete", index=i, new=newb["name"])
        content_file.write_text(json.dumps(content))
        return content

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

    # Assign today's hook variant for the A/B loop (idempotent per date).
    hook_variant = experiment.assign_variant(output_dir, today)

    result = {
        "date": today,
        "hook": ideas["hook"],
        "cta": ideas["cta"],
        "hook_variant": hook_variant,
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
