# Setup Guide

## 1. Fill in your .env

```bash
cp .env.example .env
```

Edit `.env` and add:
| Key | Where to get it |
|-----|----------------|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `FAL_KEY` | fal.ai → Dashboard → API Keys |
| `TELEGRAM_BOT_TOKEN` | Telegram → message @BotFather → /newbot |
| `TELEGRAM_CHAT_ID` | Message @userinfobot in Telegram |
| `N8N_ENCRYPTION_KEY` | Any random 32-char string |
| `N8N_JWT_SECRET` | Any random string |

## 2. Start the stack

```bash
make up
```

Opens:
- n8n UI → http://localhost:5678
- Flower (Celery monitor) → http://localhost:5555

## 3. Register your Telegram webhook

Run this once (replace values from your .env):

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<YOUR_PUBLIC_N8N_URL>/webhook/telegram-callback"
```

> n8n must be publicly reachable for Telegram to call it.
> Use [ngrok](https://ngrok.com) locally: `ngrok http 5678`

## 4. Import the n8n workflow

1. Open http://localhost:5678
2. Go to **Workflows → Import from file**
3. Select `n8n/workflow_daily_pipeline.json`
4. Set your environment variables in n8n: Settings → Variables → add `TELEGRAM_CHAT_ID` and `TELEGRAM_BOT_TOKEN`
5. Activate the workflow

## 5. Test manually

```bash
# Generate content for today (no n8n needed)
python scripts/generate_content.py --output-dir ./output/content

# Or with specific bands
python scripts/generate_content.py --bands "Radiohead" "The Beatles" "Gorillaz" "Arctic Monkeys"

# Check health
python scripts/health_check.py
```

## Daily flow

```
09:00 UTC  → n8n cron fires
             → Claude picks 4 bands
             → fal.ai generates 4 images (~30s)
             → Telegram: 4 images sent to you with spoiler band names
             → Pipeline pauses ⏸

You review  → tap ✅ Approve & Render
             → Pipeline resumes ▶
             → moviepy renders tiktok_reels.mp4 (61s) + youtube_shorts.mp4 (59s)
             → Telegram sends you both video files
```

If you tap 🔄 Regen #N → that image is regenerated and preview re-sent.
