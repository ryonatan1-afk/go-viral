# Go-Viral — Backlog & Open Items

_Last updated: 2026-06-09. Living doc — prune as items ship._

## 🔴 Open now (do next)
- [x] ~~Open the PR for `feat/six-bands-and-hook-playbook`~~ — merged to `main` (ff) 2026-06-09.
- [x] ~~Verify approval flow~~ — verified 2026-06-09: approve→render and regen→re-preview both work.

## 🔧 Reliability fixes (planned — not yet built)
_Two silent failures broke the 2026-06-09 morning run; both recovered manually. Plan below._

### #1 ngrok is a manual process outside the stack (ROOT CAUSE of dropped Telegram callbacks)
Every docker-compose service has `restart: unless-stopped`, but **ngrok runs as a separate manual
process** — when it died, all Telegram button taps hit a dead tunnel (404) and nothing restarted it.
- **Plan:** add an `ngrok` service to `docker-compose.yml` using the `ngrok/ngrok` image,
  `restart: unless-stopped`, command pointing at `n8n:5678`, with the **reserved domain** +
  `NGROK_AUTHTOKEN` from `.env`. Comes up with `make up`, self-heals on crash.
- **Payoff:** since the domain is reserved, the public URL never changes → Telegram webhook never
  needs re-registration again. Eliminates this failure mode entirely. (~10 lines.)

### #2 n8n catch-up cron did not re-arm after an ungraceful restart
The 15-min catch-up cron stopped firing after n8n restarted ~13h prior (same "ungraceful Docker kill"
footgun seen around exec #62). A manual `docker compose restart n8n` re-armed it.
- **Decision:** do NOT deep-debug n8n's trigger-registration internals (rabbit hole).
- **Plan:** add a **dead-man's-switch** — if `needs_run` is still `true` past ~14:00 local, send a
  Telegram "⚠️ daily video not generated yet" alert. Converts silent misses into a visible ping,
  catches #2 and any future silent failure cheaply. (Open question: where to host the check so it
  doesn't depend on the same n8n cron that may be dead — likely host-level Windows Task Scheduler.)

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
