"""
api.py — Flask HTTP API so n8n can trigger pipeline steps via HTTP Request nodes.
Runs inside the worker container alongside Celery.
"""

from __future__ import annotations

import json
import os
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
                       difficulties=None, hook=None, cta=None):
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
            "--outro-sound", f"{TEMPLATES_DIR}/audio/outro.wav",
            "--font", f"{TEMPLATES_DIR}/fonts/bold.ttf",
        ]
        if band_names and len(band_names) == 4:
            cmd += ["--band-names", *band_names]
        if difficulties and len(difficulties) == 4:
            cmd += ["--difficulties", *difficulties]
        if hook:
            cmd += ["--hook", hook]
        if cta:
            cmd += ["--cta", cta]
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

    _render_and_notify(image_paths, titles, captions, band_names,
                       difficulties=difficulties, hook=hook, cta=cta)
    return jsonify({"ok": True, "status": "rendering_started"})


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
