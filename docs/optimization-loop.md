# Self-Tuning Hook Loop (v1)

A minimal closed loop to learn which hook variant performs best — no platform APIs.

## The loop
```
ASSIGN   experiment.assign_variant() picks today's hook (balanced rotation over
         VARIANTS) → stamps hook_variant into content_<date>.json + experiments.csv
RENDER   /render-videos reads hook_variant from the content JSON → generate_video.py
         --hook-variant → kinetic | classic hook
POST     you download the video from Telegram and post it manually
INGEST   you open IG Insights, screenshot it, send the image to the Telegram bot
         → Claude vision reads {views,likes,comments,shares} → results.csv
SUMMARY  /results (or send "/results" to the bot) → per-variant leaderboard
DECIDE   you read the leaderboard; promote the winner by hand when confident
```

## Operator runbook
1. **Daily** — the pipeline assigns a variant automatically. Approve/post as usual.
2. **~2–3 days after posting** (let engagement mature), open Instagram **Insights** for that post,
   **screenshot** it, and **send the screenshot to the bot**. Put the post's date in the **caption**
   as `YYYY-MM-DD` (e.g. `2026-06-09`). No caption → the bot assumes your most-recent un-scored post.
   The bot replies with what it recorded.
3. **Anytime** — send **`/results`** to the bot to see the leaderboard:
   `kinetic 0.42 (8 posts) vs classic 0.31 (7) — kinetic leading`.

## Metric
`engagement_score = (likes + 2*comments + 3*shares) / max(views, 1)` — a reach-normalized
engagement basket. Weights live in `scripts/experiment.py` (`engagement_score`), tweak freely.

## Promoting a winner (manual by design)
When one variant clearly leads with enough samples (rule of thumb: **≥10–14 posts per arm** — 1 post/day
on one account means this is a *sequential* test, so give it 2–4 weeks), promote it by either:
- editing the live default (`HOOK_VARIANT` env / the dispatcher default in `generate_video.py`), or
- dropping the loser from `VARIANTS` in `scripts/experiment.py` so the loop stops testing it.

## State files (in `output/content/`, same pattern as `used_bands.csv`)
- `experiments.csv` — `date, variant` (one row/day, idempotent).
- `results.csv` — `date, variant, views, likes, comments, shares, captured_at, source` (latest wins).

## Adding a new variant to test
1. Add a `build_hook_<name>` builder + register it in the `build_hook_cmd` dispatcher
   (`scripts/generate_video.py`).
2. Add `"<name>"` to `VARIANTS` in `scripts/experiment.py`. The rotation picks it up automatically.

## Deferred (build when the simple loop proves useful)
Auto-upload to platforms; analytics-API / OCR-less ingestion; multi-armed-bandit weighting; an
approve-to-promote button; agent-generated new variants; 3-second view rate / retention curves.

## Notes / footguns
- Photo + `/results` updates reach the worker through the **existing** Telegram→ngrok→n8n→
  `/handle-callback` plumbing — no n8n change needed. **ngrok must be running.**
- Vision extraction uses `claude-sonnet-4-6`; a real IG/TikTok analytics screenshot is the live test.
- Render verification runs in the **Docker worker**, not local Windows ffmpeg.
