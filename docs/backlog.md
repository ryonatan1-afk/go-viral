# Go-Viral — Backlog & Open Items

_Last updated: 2026-06-09. Living doc — prune as items ship._

## 🔴 Open now (do next)
- [ ] **Open the PR** for `feat/six-bands-and-hook-playbook` and decide merge to `main`.
- [ ] **Verify today's approval flow.** A 6-button preview was sent for 2026-06-08 against paused n8n
      execution **#62**, but #62's survival of an ungraceful Docker kill is unconfirmed. If tapping
      ✅ Approve doesn't render, re-trigger generation or POST worker `/render-videos` directly.

## 🟡 Hook improvements (see `docs/hook-improvements.md` for full detail + cost math)
Recommended rollout order — ship one at a time, measure **3-second view rate** ~5–7 days each:
- [ ] **#5 Kinetic "stakes UI" hook + 0.5s audio sting** — $0, fixes the static-text problem. Baseline.
- [ ] **#1 Smash-cut rapid-fire pre-roll** (1.2s) — $0 pattern break.
- [ ] **#2 Decoy / wrong-answer comment bait** — $0; add a `decoy_guess` field to the Claude content call.
- [ ] **#3 Recurring animated mascot host** — low cost; builds durable channel identity.
- [ ] **#4 HeyGen/D-ID AI avatar host** — premium (~$0.30/video D-ID). Do LAST; make it **fail-open**.
- [ ] **`HOOK_VARIANT` config** in `generate_video.py` to rotate/A-B hook variants and attribute retention.

## 🟢 Nice-to-have / tech debt
- [ ] Re-enable **YouTube Shorts** output (currently disabled in `PLATFORM_CONFIGS` + `api.py`).
- [ ] Fix stale `n8n/workflow_live.json` snapshot (still shows `!==4`; cosmetic, not the import file).
- [ ] Consider a **static ngrok domain** to stop re-registering the Telegram webhook on every restart.
- [ ] Drop the obsolete `version:` attribute from `docker-compose.yml` (harmless warning).

## ⚠️ Footguns to remember
- n8n MCP `update_workflow` edits the **draft** — must `publish_workflow` or the live cron ignores it.
- Stay off **Docker Desktop 4.62.0** (Model Runner startup crash). On 4.76 now.
- Catch-up cron fires only **09:00–22:45 local**; no auto-trigger outside that window.
