"""
telegram_bot.py
===============
Lightweight Telegram approval bot for the human-in-the-loop review step.

Sends 4 generated band-puzzle images to a Telegram chat with inline buttons:
  [✅ Approve & Render] [🔄 Regen #1] [🔄 Regen #2] [🔄 Regen #3] [🔄 Regen #4]

On approval, POSTs to the n8n resume webhook to continue the paused execution.
On regenerate, calls back to n8n with the band index to regen.

Usage (called by n8n via HTTP Request node after image generation):
    python scripts/telegram_bot.py \
        --action send_preview \
        --content-file ./output/content/content_2026-06-04.json \
        --resume-url "http://n8n:5678/webhook-waiting/abc123" \
        --chat-id 123456789

    python scripts/telegram_bot.py \
        --action send_notification \
        --message "✅ Videos rendered!" \
        --files ./output/tiktok_reels.mp4 ./output/youtube_shorts.mp4 \
        --chat-id 123456789
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


def send_photo(chat_id: str, image_path: str, caption: str, reply_markup: dict | None = None) -> dict:
    with open(image_path, "rb") as f:
        files = {"photo": f}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        with httpx.Client(timeout=60) as client:
            resp = client.post(f"{BASE_URL}/sendPhoto", data=data, files=files)
            resp.raise_for_status()
            return resp.json()


def send_message(chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{BASE_URL}/sendMessage", json=payload)
        resp.raise_for_status()
        return resp.json()


def send_document(chat_id: str, file_path: str, caption: str) -> dict:
    with open(file_path, "rb") as f:
        files = {"document": (Path(file_path).name, f, "video/mp4")}
        data = {"chat_id": chat_id, "caption": caption}
        with httpx.Client(timeout=300) as client:
            resp = client.post(f"{BASE_URL}/sendDocument", data=data, files=files)
            resp.raise_for_status()
            return resp.json()


def send_preview(content_file: str, resume_url: str, chat_id: str) -> None:
    content = json.loads(Path(content_file).read_text())
    bands = content["bands"]

    # Send each image individually (no band name in caption — it's the puzzle!)
    for i, band in enumerate(bands):
        caption = (
            f"<b>Puzzle #{i+1}</b>  [{band['difficulty'].upper()}]\n"
            f"<i>{band['visual_concept']}</i>"
        )
        send_photo(chat_id, band["image_path"], caption)

    # Single approval message with all controls
    # Encode resume_url + action into callback_data (max 64 bytes — use a key)
    # Store resume_url in a sidecar file keyed by date; callback carries just the key
    sidecar = Path(content_file).parent / f"resume_{content['date']}.txt"
    sidecar.write_text(resume_url)

    date_key = content["date"]
    band_names_preview = "\n".join(
        f"  {i+1}. <tg-spoiler>{b['name']}</tg-spoiler>" for i, b in enumerate(bands)
    )

    approval_text = (
        f"<b>🎸 Daily Band Puzzles — {date_key}</b>\n\n"
        f"Review the 4 images above.\n"
        f"Answers (tap to reveal):\n{band_names_preview}\n\n"
        f"Approve to start rendering, or pick an image to regenerate."
    )

    markup = {
        "inline_keyboard": [
            [{"text": "✅ Approve & Render", "callback_data": f"approve|{date_key}"}],
            [
                {"text": "🔄 Regen #1", "callback_data": f"regen|{date_key}|0"},
                {"text": "🔄 Regen #2", "callback_data": f"regen|{date_key}|1"},
            ],
            [
                {"text": "🔄 Regen #3", "callback_data": f"regen|{date_key}|2"},
                {"text": "🔄 Regen #4", "callback_data": f"regen|{date_key}|3"},
            ],
        ]
    }
    send_message(chat_id, approval_text, reply_markup=markup)
    print(json.dumps({"status": "preview_sent", "date": date_key}))


def send_notification(chat_id: str, message: str, files: list[str] | None = None) -> None:
    send_message(chat_id, message)
    if files:
        for f in files:
            if Path(f).exists():
                platform = "TikTok/Reels" if "tiktok" in f else "YouTube Shorts"
                send_document(chat_id, f, caption=f"🎬 {platform}")
    print(json.dumps({"status": "notification_sent"}))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--action", choices=["send_preview", "send_notification"], required=True)
    p.add_argument("--content-file")
    p.add_argument("--resume-url")
    p.add_argument("--chat-id", required=True)
    p.add_argument("--message")
    p.add_argument("--files", nargs="*")
    args = p.parse_args()

    if args.action == "send_preview":
        if not args.content_file or not args.resume_url:
            print("--content-file and --resume-url required for send_preview", file=sys.stderr)
            sys.exit(1)
        send_preview(args.content_file, args.resume_url, args.chat_id)
    elif args.action == "send_notification":
        send_notification(args.chat_id, args.message or "Done!", args.files)


if __name__ == "__main__":
    main()
