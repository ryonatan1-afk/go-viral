# Go-Viral — Project Context

## What this is
Autonomous daily pipeline that generates "Guess the Band" visual puzzle videos for TikTok/Reels (~62s) and YouTube Shorts (59s). Each video has 6 slides showing a literal visual pun of a band name (e.g. a man with a radio for a head = Radiohead). The ~62s length intentionally clears TikTok's 60s monetization minimum.

## Stack
- **Python 3.14** (local), **Python 3.11** (Docker worker)
- **ffmpeg 8.1.1** — video engine (zoompan Ken Burns, drawtext overlays, H.264 encode). moviepy was REMOVED — too slow (48min renders). ffmpeg renders all 6 slides in parallel via subprocess, ~2.5min total.
- **fal.ai FLUX.1[dev]** — image generation (~$0.003/image)
- **Claude claude-sonnet-4-6** — band selection + image prompt generation
- **n8n** (self-hosted, Docker) — workflow orchestration, daily cron
- **Telegram bot** — human-in-the-loop approval before render
- **Celery + Redis** — async task queue for video rendering
- **ngrok** — exposes n8n webhooks publicly for Telegram callbacks

## Key files
| File | Purpose |
|------|---------|
| `scripts/generate_video.py` | Main render engine — Ken Burns zoom, text overlays, countdown timer, dual output |
| `scripts/generate_content.py` | Daily content brain — calls Claude for band ideas, fal.ai for images |
| `scripts/telegram_bot.py` | Sends Telegram previews + approval buttons, handles notifications |
| `scripts/celery_app.py` | Celery task wrapper for async renders |
| `scripts/health_check.py` | Validates ffmpeg, fonts, audio before run |
| `scripts/generate_audio_assets.py` | Synthesizes tick.wav + outro.wav (no ffmpeg needed) |
| `docker-compose.yml` | n8n + Redis + Celery worker + Flower |
| `n8n/workflow_daily_pipeline.json` | Full n8n workflow (import this) |
| `.env` | Secrets — never commit |

## Output
- `output/tiktok_reels.mp4` — ~**62s**: animated hook → 6×(puzzle 8s + reveal 2s) → outro, all crossfaded (xfade). Band count is dynamic (driven by `len(images)` in `generate_video.py`), not hardcoded.
- **YouTube Shorts is DISABLED** (commented out in `PLATFORM_CONFIGS` in `generate_video.py` + dropped from the upload list in `api.py`). Re-enable both spots when ready.
- `output/content/content_YYYY-MM-DD.json` — daily generated content cache
- `output/content/used_bands.csv` — persistent band-dedup log (date,band,difficulty)

## Video spec
- Canvas: **1080 × 1920** (9:16 strict), FPS 30
- Structure: hook (3s, blurred 2×2 collage + animated text) → per band: puzzle (Ken Burns 1.0→1.12, big title + caption on dark boxes, difficulty badge, progress bar, 3-2-1 countdown) → reveal (darkened image + band-name scale-pop). xfade `fade` 0.4s between all segments.
- Text: auto-fit font sizes (`fit_fontsize`), `sanitize_text` strips emoji/quotes/`%` (apostrophes & `%` break unquoted drawtext). Background boxes for legibility.
- Audio (all numpy-synthesized in `generate_audio_assets.py`): `beat.wav` music bed, `scratch.wav` on each new slide, `whoosh.wav`+`ding.wav` on reveals, `outro.wav` jingle. Mixed in one final ffmpeg pass.
- Font: Bebas Neue Bold (`templates/fonts/bold.ttf`)

## Daily pipeline flow
```
catch-up trigger (every 15m, 09:00-22:45) → GET worker:8080/needs-run
→ if today's content missing → Claude picks 6 bands (avoids used_bands.csv) → fal.ai generates 6 images
→ Telegram preview sent (Approve/Regen buttons) → pipeline PAUSES (n8n Wait node)
→ user taps ✅ Approve → resume → ffmpeg renders tiktok_reels.mp4 (~3.5min)
→ worker background thread sends video directly to Telegram
```
Note: exact 09:00 trigger is DISABLED; the 15-min catch-up covers 09:00 and recovers slept-through days on wake.

## Infrastructure
- n8n UI: http://localhost:5678
- Flower (Celery monitor): http://localhost:5555
- Redis: localhost:6379
- ngrok tunnel: https://cheatingly-aedilitian-chi.ngrok-free.dev (changes on restart)
- Telegram webhook: registered at `/webhook/telegram-callback` on n8n
- n8n MCP server registered at user scope: `http://localhost:5678/mcp-server/http`

## Known compatibility fixes applied
- `audioop-lts` required for pydub on Python 3.14
- `imageio-ffmpeg` must be >= 0.6.0 (dropped pkg_resources)
- `opencv-python-headless` must be >= 4.13.0 (NumPy 2.x ABI)
- Audio assets use `.wav` not `.mp3` (no local ffmpeg needed for synthesis)
- `structlog.stdlib.add_logger_name` removed (incompatible with PrintLoggerFactory)
- ffmpeg on Windows: PATH must be refreshed in each PowerShell session via `$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + ...`
- ffmpeg filter_complex on Windows: commas in drawtext `enable=` expressions MUST be escaped as `\,` (e.g. `gte(t\,13.0)*lte(t\,14.0)`). Single-quote protection does NOT work on this build. Comparison operators `>` `<` also do NOT work in enable expressions.
- ffmpeg drawtext: numeric option values (shadowx, shadowy, fontsize) are evaluated as arithmetic expressions and will greedily consume past `[pad]` labels. Do NOT use shadow options in filter_complex — they break the filtergraph. Text currently renders without drop shadow.

## Env vars required (.env)
```
ANTHROPIC_API_KEY=
FAL_KEY=
TELEGRAM_BOT_TOKEN=       # revoke old one if exposed — use /revoke in @BotFather
TELEGRAM_CHAT_ID=
N8N_ENCRYPTION_KEY=
N8N_JWT_SECRET=
```

## Common commands
```bash
make up                   # start all containers
make down                 # stop all containers
make logs                 # tail worker + n8n logs
make health               # validate assets inside worker
make test-render          # render with dummy test images
python scripts/generate_content.py --bands "Radiohead" "The Beatles" "Gorillaz" "Arctic Monkeys"
docker compose up -d --force-recreate n8n worker   # restart after .env changes
```

## Re-register Telegram webhook (after ngrok restart or token rotation)
```powershell
$token = "YOUR_BOT_TOKEN"
$ngrokUrl = "YOUR_NGROK_URL"
Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/setWebhook?url=$ngrokUrl/webhook/telegram-callback"
```

## Security reminders
- Never share bot token or ngrok authtoken in chat — regenerate immediately if exposed
- ngrok free URLs change on every restart — re-register Telegram webhook each time
- Consider a static ngrok domain (free tier allows 1) to avoid webhook re-registration
