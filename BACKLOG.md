# Backlog

## Must do next session

- [ ] **Render youtube_shorts.mp4** — the file in `output/` is stale (old moviepy render). Run: `python scripts/generate_video.py --platforms youtube_shorts` (refresh PATH first)
- [ ] **Review tiktok_reels.mp4 quality** — open `output/tiktok_reels.mp4` and check Ken Burns zoom, text readability, countdown timer visibility
- [ ] **Activate n8n workflow** — open http://localhost:5678, toggle the Go-Viral pipeline to Active, verify `executeCommand` node paths match container paths (`/app/scripts/`, `/output/`, `/templates/`)
- [ ] **Test Telegram approval loop end-to-end** — trigger the workflow manually in n8n and confirm the Telegram preview arrives, approve button resumes execution, and videos render
- [ ] **Decide on text drop shadows** — currently omitted because ffmpeg `shadowx/shadowy` options break `filter_complex` on Windows. Fix requires a two-pass render (encode slide first, then add drawtext in a second `-vf` pass on the encoded file)
- [ ] **Decide where n8n runs scripts** — workflow `executeCommand` nodes run inside the n8n container. Python scripts + API keys need to be accessible there. Either fix volume mounts or switch to calling the Celery worker via HTTP instead

## High priority

- [ ] **Revoke exposed credentials**
  - Telegram bot token exposed in chat → `/revoke` in @BotFather, update `.env`, restart containers, re-register webhook
  - ngrok authtoken exposed in chat → regenerate at ngrok.com/dashboard, run `ngrok config add-authtoken NEW_TOKEN`

- [ ] **Claim a static ngrok domain** (free)
  - ngrok dashboard → Domains → claim 1 free static domain
  - Update tunnel command: `C:\ngrok\ngrok.exe http --domain=your-domain.ngrok-free.app 5678`
  - Re-register Telegram webhook with the static URL once — never need to do it again

- [ ] **Test the full end-to-end pipeline**
  - Activate the n8n workflow (toggle top-right in workflow editor)
  - Click "Execute Workflow" manually to trigger a test run
  - Verify: Claude picks bands → fal.ai generates images → Telegram preview arrives → approve → videos render → Telegram sends files

## Medium priority

- [ ] **Wire up the n8n MCP server**
  - Restart Claude Code and complete the n8n OAuth flow when prompted
  - Lets you trigger/inspect workflows directly from Claude without touching the n8n UI

- [ ] **Add real puzzle images**
  - Replace test_assets with actual band visual content
  - Or let the pipeline generate them fully via fal.ai (already wired up)

- [ ] **Schedule the daily cron in n8n**
  - Currently set to 09:00 — adjust timezone in `.env` (`TZ=`) and in the cron node to match your local time

- [ ] **Set up a static output folder / file naming**
  - Currently overwrites `tiktok_reels.mp4` and `youtube_shorts.mp4` each run
  - Consider date-stamped filenames: `tiktok_reels_2026-06-04.mp4`

## Nice to have

- [ ] **Runway Gen-3 animated slides**
  - Use Runway image-to-video API to animate each band image into a 14s cinematic clip before compositing
  - Pipeline: fal.ai image → Runway Gen-3 (AI motion) → ffmpeg compositor (text + countdown overlays on top)
  - Cost: ~$0.05/s × 56s of slides = ~$2.80/render. Worth it if engagement lifts significantly
  - Runway Python SDK: `pip install runwayml`
  - Would replace Ken Burns zoom with actual AI-generated motion (camera moves, subtle animation, lighting changes)
  - Keep ffmpeg compositor as-is — just swap static image input for Runway video clip input


- [ ] **Auto-upload to TikTok/YouTube after approval**
  - TikTok: Content Posting API (requires app registration)
  - YouTube: YouTube Data API v3 (OAuth2)
  - Could be an additional n8n node after render completes

- [ ] **Add answer reveal as a follow-up video**
  - Second shorter video (15s) revealing the band name
  - Post as a reply/stitch to the original

- [ ] **Track used bands in a proper database**
  - Currently scans JSON files in output/content/
  - SQLite or Supabase would be cleaner for deduplication at scale

- [ ] **Improve Ken Burns variety**
  - Currently always zooms in — add zoom-out and pan-left/right variants
  - Randomise per slide to add more visual variety

- [ ] **Add difficulty badges as visual overlays**
  - Easy / Medium / Hard shown as a styled badge on the image
  - Currently only in the Telegram preview caption

- [ ] **Dockerise the full workflow runner**
  - `generate_content.py` and `telegram_bot.py` currently run on host Python
  - Move execution into the worker container so everything is self-contained
