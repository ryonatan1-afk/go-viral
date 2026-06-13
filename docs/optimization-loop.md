# Self-Tuning Hook Loop (v1)

A minimal closed loop to learn which hook variant performs best — no platform APIs.

## The loop
```
ASSIGN   experiment.assign_variant() picks today's hook (balanced rotation over
         VARIANTS) → stamps hook_variant into content_<date>.json + experiments.csv
RENDER   /render-videos reads hook_variant from the content JSON → generate_video.py
         --hook-variant → kinetic | classic | glitch | zoom hook
POST     you download the video from Telegram and post it manually
INGEST   you screenshot the IG Reel-insights "What affects your views" panel and send
         it to the bot → Claude vision reads the rates (skip/like/comment/share/save)
         → results.csv
SUMMARY  /results (or send "/results" to the bot) → per-variant leaderboard by hold rate
DECIDE   you read the leaderboard; promote the winner by hand when confident
```

## Operator runbook
1. **Daily** — the pipeline assigns a variant automatically. Approve/post as usual.
2. **~48h after posting** (let it mature; keep the timing consistent across posts), open the Reel →
   **View Insights** → the **"What affects your views"** panel (shows *Skip rate, Like rate, Share rate,
   Save rate, Comment rate* as %). **Screenshot it and send to the bot** with the video's date in the
   **caption** as `YYYY-MM-DD` (e.g. `2026-06-13`). No caption → bot assumes most-recent un-scored post.
   The bot replies with what it read (skip/hold + engagement rates).
3. **Anytime** — send **`/results`** for the leaderboard, ranked by **hold rate** (higher = better):
   `glitch: hold 45% (skip 55%), eng 2.8% — 6 posts` …  `🏆 Best hook: glitch`.

## Metric
**Primary = skip rate** (from the panel) → the hook's whole job is to lower it. The leaderboard ranks by
**hold rate = 100 − skip rate** (higher is better). Secondary = `interaction_rate` = like+comment+share+
save rates summed. Both live in `scripts/experiment.py` (`summary`, `interaction_rate`).

## Promoting a winner (manual by design)
When one variant clearly leads with enough samples (rule of thumb: **≥10–14 posts per arm** — 1 post/day
on one account means this is a *sequential* test, so give it 2–4 weeks), promote it by either:
- editing the live default (`HOOK_VARIANT` env / the dispatcher default in `generate_video.py`), or
- dropping the loser from `VARIANTS` in `scripts/experiment.py` so the loop stops testing it.

## State files (in `output/content/`, same pattern as `used_bands.csv`)
- `experiments.csv` — `date, variant` (one row/day, idempotent).
- `results.csv` — `date, variant, skip_rate, like_rate, comment_rate, share_rate, save_rate,
  captured_at, source` (rates as %, latest wins).

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
