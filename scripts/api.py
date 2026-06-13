"""
api.py — Flask HTTP API so n8n can trigger pipeline steps via HTTP Request nodes.
Runs inside the worker container alongside Celery.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/output")
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "/templates")


def _run(cmd: list[str], timeout: int = 300) -> dict:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/app",
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr or result.stdout}
        return {"ok": True, "stdout": result.stdout, "stderr": result.stderr}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Command timed out after {timeout}s"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/needs-run")
def needs_run():
    """
    Catch-up check for the daily pipeline. Returns needs_run=true when today's
    content has not been generated yet, so n8n can run a missed day on wake.
    """
    today = date.today().isoformat()
    exists = Path(f"{OUTPUT_DIR}/content/content_{today}.json").exists()
    return jsonify({"ok": True, "date": today, "needs_run": not exists})


def _parse_content_json(stdout: str) -> dict | None:
    """Extract the last JSON object from script output (structlog lines precede it)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


@app.post("/generate-content")
def generate_content():
    """Generate band ideas + images. Body: { bands?: string[] }"""
    body = request.get_json(silent=True) or {}
    bands = body.get("bands", [])

    cmd = [sys.executable, "scripts/generate_content.py",
           "--output-dir", f"{OUTPUT_DIR}/content", "--json-out"]
    if bands:
        cmd += ["--bands"] + bands

    result = _run(cmd, timeout=300)
    if not result["ok"]:
        return jsonify(result), 500

    content = _parse_content_json(result["stdout"])
    if content is None:
        return jsonify({"ok": False, "error": "No JSON found in output", "raw": result["stdout"][:500]}), 500

    return jsonify({"ok": True, **content})


@app.post("/regen-image")
def regen_image():
    """
    Regenerate a single band image.
    Body: { date: str, band_index: int }
    Loads today's content, regenerates only the specified band's image,
    updates the content JSON, and returns the full updated content.
    """
    body = request.get_json(silent=True) or {}
    date = body.get("date")
    band_index = body.get("band_index")

    if date is None or band_index is None:
        return jsonify({"ok": False, "error": "date and band_index required"}), 400

    band_index = int(band_index)
    content_file = Path(f"{OUTPUT_DIR}/content/content_{date}.json")
    if not content_file.exists():
        return jsonify({"ok": False, "error": f"No content file for {date}"}), 404

    content = json.loads(content_file.read_text())
    bands = [b["name"] for b in content["bands"]]

    if band_index < 0 or band_index >= len(bands):
        return jsonify({"ok": False, "error": f"band_index {band_index} out of range"}), 400

    # Replace only the target band with __REGEN__ so the script regenerates just that one
    regen_bands = [b if i != band_index else "__REGEN__" for i, b in enumerate(bands)]

    cmd = [sys.executable, "scripts/generate_content.py",
           "--output-dir", f"{OUTPUT_DIR}/content", "--json-out",
           "--bands"] + regen_bands

    result = _run(cmd, timeout=300)
    if not result["ok"]:
        return jsonify(result), 500

    updated = _parse_content_json(result["stdout"])
    if updated is None:
        return jsonify({"ok": False, "error": "No JSON found in output", "raw": result["stdout"][:500]}), 500

    return jsonify({"ok": True, **updated})


@app.post("/send-preview")
def send_preview():
    """Send Telegram preview. Body: { date: str, resume_url: str }"""
    body = request.get_json(silent=True) or {}
    date = body.get("date")
    resume_url = body.get("resume_url")

    if not date or not resume_url:
        return jsonify({"ok": False, "error": "date and resume_url required"}), 400

    content_file = f"{OUTPUT_DIR}/content/content_{date}.json"
    cmd = [
        sys.executable, "scripts/telegram_bot.py",
        "--action", "send_preview",
        "--content-file", content_file,
        "--resume-url", resume_url,
        "--chat-id", os.environ["TELEGRAM_CHAT_ID"],
    ]

    result = _run(cmd, timeout=60)
    return jsonify(result), 200 if result["ok"] else 500


def _render_and_notify(image_paths, titles, captions, band_names,
                       difficulties=None, hook=None, cta=None, hook_variant=None):
    """Run render in background thread, then send Telegram notification."""
    import threading
    import requests as req_lib

    def _run_render():
        cmd = [
            sys.executable, "scripts/generate_video.py",
            "--images", *image_paths,
            "--titles", *titles,
            "--captions", *captions,
            "--output-dir", OUTPUT_DIR,
            "--beat-sound", f"{TEMPLATES_DIR}/audio/beat.wav",
            "--ding-sound", f"{TEMPLATES_DIR}/audio/ding.wav",
            "--whoosh-sound", f"{TEMPLATES_DIR}/audio/whoosh.wav",
            "--scratch-sound", f"{TEMPLATES_DIR}/audio/scratch.wav",
            "--sting-sound", f"{TEMPLATES_DIR}/audio/sting.wav",
            "--outro-sound", f"{TEMPLATES_DIR}/audio/outro.wav",
            "--font", f"{TEMPLATES_DIR}/fonts/bold.ttf",
        ]
        if band_names and len(band_names) == len(image_paths):
            cmd += ["--band-names", *band_names]
        if difficulties and len(difficulties) == len(image_paths):
            cmd += ["--difficulties", *difficulties]
        if hook:
            cmd += ["--hook", hook]
        if cta:
            cmd += ["--cta", cta]
        if hook_variant:
            cmd += ["--hook-variant", hook_variant]
        result = _run(cmd, timeout=600)
        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]

        if not result["ok"]:
            req_lib.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"❌ Render failed:\n{result['error'][:500]}"},
                timeout=10,
            )
            return

        msg = f"✅ Videos ready!\n\nBands: {', '.join(band_names)}\n\nUploading..."
        req_lib.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        # YouTube Shorts render disabled for now — only ship the TikTok/Reels cut.
        for path in [f"{OUTPUT_DIR}/tiktok_reels.mp4"]:
            if os.path.exists(path):
                with open(path, "rb") as f:
                    req_lib.post(
                        f"https://api.telegram.org/bot{bot_token}/sendVideo",
                        data={"chat_id": chat_id},
                        files={"video": f},
                        timeout=120,
                    )

    threading.Thread(target=_run_render, daemon=True).start()


@app.post("/render-videos")
def render_videos():
    """
    Start async render.
    Body: { image_paths, titles, captions, band_names,
            difficulties?, hook?, cta? }
    """
    body = request.get_json(silent=True) or {}
    image_paths = body.get("image_paths", [])
    titles = body.get("titles", [])
    captions = body.get("captions", [])
    band_names = body.get("band_names", titles)
    difficulties = body.get("difficulties")
    hook = body.get("hook")
    cta = body.get("cta")
    hook_variant = body.get("hook_variant") or _variant_from_content(image_paths)

    _render_and_notify(image_paths, titles, captions, band_names,
                       difficulties=difficulties, hook=hook, cta=cta,
                       hook_variant=hook_variant)
    return jsonify({"ok": True, "status": "rendering_started", "hook_variant": hook_variant})


def _variant_from_content(image_paths: list[str]) -> str | None:
    """Read the hook_variant stamped into content_<date>.json (source of truth).
    Date is parsed from the image filename (e.g. .../2026-06-09_band_1.png)."""
    for p in image_paths:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", str(p))
        if not m:
            continue
        cf = Path(f"{OUTPUT_DIR}/content/content_{m.group(1)}.json")
        if cf.exists():
            try:
                return json.loads(cf.read_text()).get("hook_variant")
            except Exception:
                return None
    return None


VISION_PROMPT = (
    "This is a screenshot of an Instagram Reel insights 'What affects your views' "
    "panel. It lists rates as percentages (e.g. 'Skip rate 78.0%', 'Like rate 0.9%'). "
    "Extract these rates as plain numbers (percent value without the % sign). "
    "Return ONLY a JSON object, no prose:\n"
    '{"skip_rate": number, "like_rate": number, "comment_rate": number, '
    '"share_rate": number, "save_rate": number}\n'
    "Rules: skip_rate is the most important — read it exactly. For share_rate use the "
    "'Share rate'; if only 'Repost rate' is shown, use that. If a metric is not visible, "
    "use 0. Example: 'Skip rate 78.0%' -> skip_rate: 78.0, 'Like rate 0.9%' -> like_rate: 0.9."
)


def _tg_send(text: str) -> None:
    import requests as req_lib
    req_lib.post(
        f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text}, timeout=10,
    )


def _ingest_screenshot(message: dict) -> dict:
    """Download the photo from a Telegram message, read its metrics via Claude
    vision, and record them against a date (caption, else most-recent un-scored)."""
    import base64
    import requests as req_lib
    from anthropic import Anthropic
    import experiment

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    content_dir = Path(f"{OUTPUT_DIR}/content")

    # Largest rendition of the photo.
    photos = message.get("photo") or []
    if not photos:
        return {"ok": False, "error": "no photo in message"}
    file_id = photos[-1]["file_id"]

    gf = req_lib.get(f"https://api.telegram.org/bot{token}/getFile",
                     params={"file_id": file_id}, timeout=15).json()
    file_path = gf.get("result", {}).get("file_path")
    if not file_path:
        return {"ok": False, "error": f"getFile failed: {gf}"}
    img = req_lib.get(f"https://api.telegram.org/file/bot{token}/{file_path}", timeout=30).content
    media_type = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=300,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.standard_b64encode(img).decode()}},
            {"type": "text", "text": VISION_PROMPT},
        ]}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.startswith("json") else raw
    metrics = json.loads(raw.strip())

    # Date: caption (YYYY-MM-DD) else most-recent un-scored post.
    caption = message.get("caption", "") or ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", caption)
    date_str = m.group(0) if m else experiment.most_recent_unscored(content_dir)
    if not date_str:
        return {"ok": False, "error": "no date in caption and no un-scored post found"}

    def _num(key):
        try:
            return float(metrics.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    row = experiment.record_metrics(
        content_dir, date_str,
        _num("skip_rate"), _num("like_rate"), _num("comment_rate"),
        _num("share_rate"), _num("save_rate"),
    )
    hold = round(100.0 - row["skip_rate"], 1)
    _tg_send(
        f"📊 Recorded for {date_str} ({row['variant']}): skip {row['skip_rate']}% "
        f"(hold {hold}%) | like {row['like_rate']}% comment {row['comment_rate']}% "
        f"share {row['share_rate']}% save {row['save_rate']}%."
    )
    return {"ok": True, **row}


@app.post("/ingest-screenshot")
def ingest_screenshot():
    """Body: a Telegram message object containing a photo (forwarded by n8n)."""
    body = request.get_json(silent=True) or {}
    message = body.get("message") or body
    try:
        result = _ingest_screenshot(message)
    except Exception as exc:
        _tg_send(f"❌ Couldn't read that screenshot: {exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(result), 200 if result.get("ok") else 400


def _send_results() -> dict:
    """Compute the hook leaderboard and send it to Telegram. Returns the summary."""
    import experiment
    summary = experiment.summary(Path(f"{OUTPUT_DIR}/content"))
    variants = summary["variants"]
    if not variants:
        _tg_send("📈 No data yet. Send an IG Reel-insights screenshot to start.")
        return summary
    # Rank by hold rate (lowest skip = best hook).
    lines = [f"  {v}: hold {d['mean_hold']}% (skip {d['mean_skip']}%), "
             f"eng {d['mean_engagement']}% — {d['n']} posts"
             for v, d in sorted(variants.items(), key=lambda kv: kv[1]["mean_skip"])]
    _tg_send("📈 Hook performance (higher hold = better):\n" + "\n".join(lines) +
             (f"\n\n🏆 Best hook: {summary['leader']}" if summary["leader"] else ""))
    return summary


@app.post("/results")
def results():
    """Summarize per-variant engagement and send the leaderboard to Telegram."""
    return jsonify({"ok": True, **_send_results()})


@app.post("/notify")
def notify():
    """Send Telegram notification. Body: { message: str, files?: str[] }"""
    body = request.get_json(silent=True) or {}
    message = body.get("message", "")
    files = body.get("files", [])

    cmd = [
        sys.executable, "scripts/telegram_bot.py",
        "--action", "send_notification",
        "--chat-id", os.environ["TELEGRAM_CHAT_ID"],
        "--message", message,
    ]
    if files:
        cmd += ["--files"] + files

    result = _run(cmd, timeout=120)
    return jsonify(result), 200 if result["ok"] else 500


@app.post("/handle-callback")
def handle_callback():
    """
    Receives raw Telegram update from n8n webhook, handles callback_query,
    reads the resume sidecar, and POSTs to the n8n Wait node resume URL.
    Body: the raw Telegram update object (forwarded by n8n).
    """
    import requests as req_lib

    body = request.get_json(silent=True) or {}
    # n8n wraps webhook body under 'body' key
    update = body.get("body") or body

    # Photo message → engagement-screenshot ingestion (A/B feedback loop).
    message = update.get("message") or {}
    if message.get("photo"):
        try:
            result = _ingest_screenshot(message)
        except Exception as exc:
            _tg_send(f"❌ Couldn't read that screenshot: {exc}")
            return jsonify({"ok": False, "error": str(exc)}), 500
        return jsonify(result), 200 if result.get("ok") else 400

    # "/results" text command → send the hook leaderboard.
    if (message.get("text") or "").strip().lower().startswith("/results"):
        return jsonify({"ok": True, **_send_results()})

    callback = update.get("callback_query")

    if not callback:
        return jsonify({"ok": False, "error": "No callback_query in update"}), 400

    callback_id = callback["id"]
    data = callback.get("data", "")
    parts = data.split("|")
    if len(parts) < 2:
        return jsonify({"ok": False, "error": f"Unexpected callback data: {data}"}), 400

    action = parts[0]
    date_key = parts[1]
    band_index = int(parts[2]) if len(parts) > 2 else None

    # Read the resume URL written by telegram_bot.py
    sidecar = Path(f"{OUTPUT_DIR}/content/resume_{date_key}.txt")
    if not sidecar.exists():
        return jsonify({"ok": False, "error": f"No resume sidecar for {date_key}"}), 404

    resume_url = sidecar.read_text().strip().replace("http://localhost:5678", "http://n8n:5678")

    # Acknowledge the Telegram button tap
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    req_lib.post(
        f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
        data={"callback_query_id": callback_id, "text": "Got it!"},
        timeout=5,
    )

    # Resume the paused n8n execution — Wait node is configured as GET
    resp = req_lib.get(
        resume_url,
        params={"action": action, "date": date_key, "band_index": band_index},
        timeout=10,
    )

    return jsonify({"ok": True, "action": action, "date": date_key, "band_index": band_index, "resume_status": resp.status_code})


# ── Reliable daily trigger (replaces n8n's flaky schedule trigger) ──────────────
# n8n's internal schedule trigger silently stops firing after a while. Instead,
# this always-on worker checks /needs-run on an interval during the active window
# and pokes an n8n webhook to run the normal generate→preview→approve→render flow.
# A dead-man's-switch alerts on Telegram if a day still isn't generated past a cutoff.

def _scheduler_loop():
    import time
    import requests as req_lib
    from datetime import datetime, timezone

    start_h = int(os.getenv("TRIGGER_START_UTC", "9"))     # window start (UTC hour)
    end_h = int(os.getenv("TRIGGER_END_UTC", "22"))        # window end   (UTC hour)
    deadman_h = int(os.getenv("DEADMAN_UTC", "14"))        # alert after this UTC hour
    interval = int(os.getenv("TRIGGER_INTERVAL_SEC", "900"))
    retry = int(os.getenv("TRIGGER_RETRY_SEC", "60"))      # short retry after an error
    webhook = os.getenv("N8N_DAILY_WEBHOOK", "http://n8n:5678/webhook/daily-trigger")

    poked_on: str | None = None     # date we last poked n8n for
    alerted_on: str | None = None   # date we last sent a dead-man alert for

    print(f"[scheduler] started — window {start_h}-{end_h} UTC, every {interval}s", flush=True)
    while True:
        sleep_for = interval
        try:
            now = datetime.now(timezone.utc)
            today = now.date().isoformat()
            if start_h <= now.hour <= end_h:
                nr = req_lib.get("http://localhost:8080/needs-run", timeout=10).json()
                if nr.get("needs_run"):
                    # Dead-man: a prior poke today didn't result in a generated video.
                    if now.hour >= deadman_h and poked_on == today and alerted_on != today:
                        _tg_send(f"⚠️ Daily video for {today} still not generated by "
                                 f"{now.hour:02d}:00 UTC. Pipeline may be stuck — "
                                 f"check n8n + ngrok.")
                        alerted_on = today
                    req_lib.post(webhook, timeout=15)
                    poked_on = today
                    print(f"[scheduler] poked daily trigger for {today}", flush=True)
        except Exception as e:
            # n8n/network not ready (e.g. both just booted) — retry soon, not in 15 min.
            print(f"[scheduler] error: {e} — retrying in {retry}s", flush=True)
            sleep_for = retry
        time.sleep(sleep_for)


def _start_scheduler():
    import threading
    threading.Thread(target=_scheduler_loop, daemon=True).start()


if __name__ == "__main__":
    _start_scheduler()
    app.run(host="0.0.0.0", port=8080, debug=False)
