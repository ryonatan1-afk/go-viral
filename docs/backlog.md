# Go-Viral — Backlog & Open Items

_Last updated: 2026-06-09. Living doc — prune as items ship._

## 🔴 Open now (do next)
- [x] ~~Open the PR for `feat/six-bands-and-hook-playbook`~~ — merged to `main` (ff) 2026-06-09.
- [x] ~~Verify approval flow~~ — verified 2026-06-09: approve→render and regen→re-preview both work.

## 🔧 Reliability fixes
_Silent failures broke the 06-09 AND 06-10 morning runs. The cron one is now fixed; ngrok still open._

### #2 n8n schedule trigger stalls — FIXED 2026-06-10
The n8n internal schedule trigger silently stopped firing (recurred two days running; a manual
`docker compose restart n8n` re-armed it each time). Everything else in n8n (webhooks, HTTP, Wait)
is reliable — only the *scheduler* is flaky.
- [x] **Fix shipped:** moved scheduling into the always-on **worker**. A daemon thread in
      `scripts/api.py` (`_scheduler_loop`) checks `/needs-run` every 15 min during the UTC window and
      POSTs a new n8n **webhook** (`/webhook/daily-trigger`) that runs the existing
      generate→preview→approve→render flow. The flaky `Run Check (15m, 9-22)` schedule trigger is
      **disabled**; the webhook trigger feeds `Needs Run?` (workflow published). Includes a
      **dead-man's-switch**: Telegram alert if a day is still un-generated after `DEADMAN_UTC` (14:00
      UTC default) following a poke. Env knobs: `TRIGGER_START_UTC/END_UTC`, `TRIGGER_INTERVAL_SEC`,
      `DEADMAN_UTC`, `N8N_DAILY_WEBHOOK`.
- [ ] **Follow-up:** `n8n/workflow_daily_pipeline.json` snapshot is now stale (missing the
      `Daily Trigger Webhook` node + disabled schedule). Re-export before any re-import, or the fix
      is lost on import.

### #1 ngrok is a manual process outside the stack (STILL OPEN — next reliability item)
Every docker-compose service has `restart: unless-stopped`, but **ngrok runs as a separate manual
process** — when it died (06-09), all Telegram button taps hit a dead tunnel (404) and nothing
restarted it. The dead-man's-switch above now *alerts* on a stuck pipeline, but doesn't fix ngrok.
- **Plan:** add an `ngrok` service to `docker-compose.yml` using the `ngrok/ngrok` image,
  `restart: unless-stopped`, command pointing at `n8n:5678`, with the **reserved domain** +
  `NGROK_AUTHTOKEN` from `.env`. Comes up with `make up`, self-heals on crash. Reserved domain →
  webhook never needs re-registration. (~10 lines.)

## 🟡 Hook improvements (see `docs/hook-improvements.md` for full detail + cost math)
Recommended rollout order — ship one at a time, measure **3-second view rate** ~5–7 days each:
- [x] ~~**#5 Kinetic "stakes UI" hook + 0.5s audio sting**~~ — SHIPPED 2026-06-09 (kinetic is the live
      default). Word-by-word pops + `0/N` counter + difficulty meter + draining timer; first-frame
      `sting.wav`. Verified in worker @ 62.8s. **Now running ~5–7 days; read 3-sec view rate before #1.**
- [ ] **#1 Smash-cut rapid-fire pre-roll** (1.2s) — $0 pattern break. (Next; reuses `build_hook_*` scaffold.)
- [ ] **#2 Decoy / wrong-answer comment bait** — $0; add a `decoy_guess` field to the Claude content call.
- [ ] **#3 Recurring animated mascot host** — low cost; builds durable channel identity.
- [ ] **#4 HeyGen/D-ID AI avatar host** — premium (~$0.30/video D-ID). Do LAST; make it **fail-open**.
- [x] ~~**`HOOK_VARIANT` config**~~ — SHIPPED. `HOOK_VARIANT=kinetic|classic` env var dispatches in
      `generate_video.py`; `classic` preserved as the A/B control. Future variants plug into the same switch.

## 🟢 Nice-to-have / tech debt
- [ ] Re-enable **YouTube Shorts** output (currently disabled in `PLATFORM_CONFIGS` + `api.py`).
- [ ] Fix stale `n8n/workflow_live.json` snapshot (still shows `!==4`; cosmetic, not the import file).
- [x] ~~Static ngrok domain~~ — already have a reserved domain (`cheatingly-aedilitian-chi.ngrok-free.dev`); URL is stable across restarts. Remaining work is dockerizing the process (see Reliability #1).
- [ ] Drop the obsolete `version:` attribute from `docker-compose.yml` (harmless warning).

## ⚠️ Footguns to remember
- n8n MCP `update_workflow` edits the **draft** — must `publish_workflow` or the live cron ignores it.
- Stay off **Docker Desktop 4.62.0** (Model Runner startup crash). On 4.76 now.
- Catch-up cron fires only **09:00–22:45 local**; no auto-trigger outside that window.
