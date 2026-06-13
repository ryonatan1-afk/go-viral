"""
experiment.py
=============
Self-tuning hook loop (v1): assign a hook variant per day, record engagement
metrics pulled from Instagram-Insights screenshots, and summarize which variant
wins. State is two CSVs in the content dir, mirroring the used_bands.csv pattern
in generate_content.py.

  experiments.csv  date, variant                       (one row per day, idempotent)
  results.csv      date, variant, views, likes,         (one row per day; latest wins)
                   comments, shares, captured_at, source

engagement_score = (likes + 2*comments + 3*shares) / max(views, 1)
"""

from __future__ import annotations

import csv
import random
from datetime import datetime
from pathlib import Path

# Hook variants under test. Must match the dispatch arms in generate_video.py.
VARIANTS = ["kinetic", "classic", "glitch", "zoom"]

EXPERIMENTS_CSV = "experiments.csv"
RESULTS_CSV = "results.csv"

EXP_FIELDS = ["date", "variant"]
# Metrics come from IG Reel insights "What affects your views" panel (rates, %).
# skip_rate is the primary hook signal — the hook's job is to lower it.
RES_FIELDS = ["date", "variant", "skip_rate", "like_rate", "comment_rate",
              "share_rate", "save_rate", "captured_at", "source"]
RATE_FIELDS = ["skip_rate", "like_rate", "comment_rate", "share_rate", "save_rate"]


# ── low-level CSV helpers ───────────────────────────────────────────────────────

def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


# ── assignment ──────────────────────────────────────────────────────────────────

def variant_for_date(output_dir: Path, date_str: str) -> str | None:
    """Return the variant already assigned to date_str, or None."""
    for row in _read_rows(output_dir / EXPERIMENTS_CSV):
        if row.get("date") == date_str:
            return row.get("variant")
    return None


def assign_variant(output_dir: Path, date_str: str) -> str:
    """
    Pick today's hook variant and log it. Idempotent: if date_str is already
    assigned, return that variant unchanged. Otherwise pick the least-used
    variant so far (ties broken randomly) to keep the split balanced over time.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = variant_for_date(output_dir, date_str)
    if existing:
        return existing

    rows = _read_rows(output_dir / EXPERIMENTS_CSV)
    counts = {v: 0 for v in VARIANTS}
    for r in rows:
        if r.get("variant") in counts:
            counts[r["variant"]] += 1
    fewest = min(counts.values())
    candidates = [v for v, c in counts.items() if c == fewest]
    variant = random.choice(candidates)

    path = output_dir / EXPERIMENTS_CSV
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EXP_FIELDS)
        if is_new:
            w.writeheader()
        w.writerow({"date": date_str, "variant": variant})
    return variant


# ── metrics ingestion ─────────────────────────────────────────────────────────

def record_metrics(output_dir: Path, date_str: str, skip_rate: float,
                   like_rate: float, comment_rate: float, share_rate: float,
                   save_rate: float, source: str = "manual") -> dict:
    """Upsert a rates row for date_str (latest screenshot wins). All values are
    percentages from the IG 'What affects your views' panel."""
    output_dir.mkdir(parents=True, exist_ok=True)
    variant = variant_for_date(output_dir, date_str) or "unknown"
    row = {
        "date": date_str, "variant": variant,
        "skip_rate": round(float(skip_rate), 2),
        "like_rate": round(float(like_rate), 2),
        "comment_rate": round(float(comment_rate), 2),
        "share_rate": round(float(share_rate), 2),
        "save_rate": round(float(save_rate), 2),
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
    }
    path = output_dir / RESULTS_CSV
    rows = [r for r in _read_rows(path) if r.get("date") != date_str]
    rows.append(row)
    rows.sort(key=lambda r: r.get("date", ""))
    _write_rows(path, RES_FIELDS, rows)
    return row


def most_recent_unscored(output_dir: Path) -> str | None:
    """Latest assigned date that has no metrics row yet (default screenshot target)."""
    scored = {r["date"] for r in _read_rows(output_dir / RESULTS_CSV)}
    dates = sorted(
        (r["date"] for r in _read_rows(output_dir / EXPERIMENTS_CSV)
         if r.get("date") and r["date"] not in scored),
        reverse=True,
    )
    return dates[0] if dates else None


# ── analysis ────────────────────────────────────────────────────────────────────

def interaction_rate(like_rate: float, comment_rate: float,
                     share_rate: float, save_rate: float) -> float:
    """Secondary signal: total positive engagement per view (sum of %)."""
    return like_rate + comment_rate + share_rate + save_rate


def summary(output_dir: Path) -> dict:
    """Per-variant mean skip rate (primary — lower is better) + mean engagement,
    plus the current leader (the variant that holds viewers best = lowest skip)."""
    skip: dict[str, list[float]] = {}
    eng: dict[str, list[float]] = {}
    for r in _read_rows(output_dir / RESULTS_CSV):
        try:
            v = r.get("variant", "unknown")
            skip.setdefault(v, []).append(float(r["skip_rate"]))
            eng.setdefault(v, []).append(interaction_rate(
                float(r["like_rate"]), float(r["comment_rate"]),
                float(r["share_rate"]), float(r["save_rate"])))
        except (ValueError, KeyError):
            continue

    variants = {}
    for v, skips in skip.items():
        if not skips:
            continue
        mean_skip = sum(skips) / len(skips)
        engs = eng.get(v, [])
        variants[v] = {
            "n": len(skips),
            "mean_skip": round(mean_skip, 2),
            "mean_hold": round(100.0 - mean_skip, 2),
            "mean_engagement": round(sum(engs) / len(engs), 2) if engs else 0.0,
        }
    # Best hook = lowest skip rate (highest hold).
    leader = min(variants, key=lambda v: variants[v]["mean_skip"]) if variants else None
    return {"variants": variants, "leader": leader}
