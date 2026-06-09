"""
generate_video.py
=================
Renders "Guess the Band" vertical videos using pure ffmpeg filter chains.
All compositing (Ken Burns, text overlays, countdown, transitions, audio)
happens inside ffmpeg in C — no Python frame loops, no numpy pixel work.

Structure (per video):
  HOOK (blurred 2x2 collage + big hook text)
  → for each of 4 bands:
        PUZZLE  (Ken Burns image + progress bar + difficulty badge
                 + kinetic title + caption + 3-2-1 countdown)
        REVEAL  (darkened image + band name slam)
  → OUTRO (CTA card)
All segments are crossfaded together (xfade) and a music bed + SFX
(whoosh on reveals, ding on answers, outro pad) is mixed over the top.

Output: tiktok_reels.mp4 (~54s)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import structlog
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Constants ─────────────────────────────────────────────────────────────────

CANVAS_W = 1080
CANVAS_H = 1920
FPS = 30

PLATFORM_CONFIGS = {
    "tiktok_reels":    {"filename": "tiktok_reels.mp4"},
    # YouTube Shorts render disabled for now — re-enable once the product is finalized.
    # "youtube_shorts":  {"filename": "youtube_shorts.mp4"},
}

# ── Timeline (seconds) ──
HOOK_DUR    = 3.5          # kinetic stakes-UI hook needs room for the game UI to land
PUZZLE_DUR  = 8.0          # snappier than the old 10s; 6 bands keeps tempo high
REVEAL_DUR  = 2.0
OUTRO_DUR   = 4.5
# 6 bands → 3.5 + 6*(8+2) + 4.5 - 0.4*13 xfade ≈ 62.8s (clears TikTok's 60s
# monetization minimum). Band count is driven dynamically by len(images).
XFADE       = 0.4          # crossfade duration between segments
TRANSITION  = "fade"       # xfade type (reliable soft cut; not a hard cut)

# Hook variant: "kinetic" (default, game-UI + motion) | "classic" (legacy static text).
# A/B by setting HOOK_VARIANT in the environment; measure 3-second view rate.
HOOK_VARIANT = os.environ.get("HOOK_VARIANT", "kinetic").lower()

COUNTDOWN_N = 3            # show 3-2-1 in the last N seconds of each puzzle
ZOOM_FACTOR = 1.12         # Ken Burns end scale

# Difficulty badge colours
DIFF_COLORS = {"easy": "0x2ECC71", "medium": "0xF39C12", "hard": "0xE74C3C"}

# ── Logging ───────────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(sys.stderr),
)
log = structlog.get_logger()


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class VideoPayload:
    images:       list[str]
    titles:       list[str]
    captions:     list[str]
    band_names:   list[str] = field(default_factory=list)
    difficulties: list[str] = field(default_factory=list)
    hook:         str = "GUESS THE BAND"
    cta:          str = "FOLLOW FOR DAILY PUZZLES"
    output_dir:   str = "./output"
    beat_sound:   str = "./templates/audio/beat.wav"
    ding_sound:   str = "./templates/audio/ding.wav"
    whoosh_sound: str = "./templates/audio/whoosh.wav"
    scratch_sound: str = "./templates/audio/scratch.wav"
    sting_sound:  str = "./templates/audio/sting.wav"
    outro_sound:  str = "./templates/audio/outro.wav"
    hook_variant: str = HOOK_VARIANT
    tick_sound:   str = "./templates/audio/tick.wav"   # legacy, unused
    font_path:    str = "./templates/fonts/bold.ttf"
    webhook_url:  Optional[str] = None
    platforms:    list[str] = field(default_factory=lambda: list(PLATFORM_CONFIGS.keys()))

    def __post_init__(self) -> None:
        # Normalise optional list lengths to the image count so they never
        # crash the render. Band count is driven entirely by len(images).
        n = len(self.images)
        if not self.band_names:
            self.band_names = ["" for _ in range(n)]
        if not self.difficulties:
            self.difficulties = ["" for _ in range(n)]
        self.band_names = (self.band_names + [""] * n)[:n]
        self.difficulties = (self.difficulties + [""] * n)[:n]

    def validate(self) -> None:
        n = len(self.images)
        assert n >= 1,                  "Need at least 1 image"
        assert len(self.titles) == n,   f"Need {n} titles to match images"
        assert len(self.captions) == n, f"Need {n} captions to match images"
        for p in self.images:
            if not Path(p).exists():
                raise FileNotFoundError(f"Image not found: {p}")
        for p in self.platforms:
            if p not in PLATFORM_CONFIGS:
                raise ValueError(f"Unknown platform: {p}")
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)


# ── Text helpers ──────────────────────────────────────────────────────────────

_SMART = {
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "—": "-", "–": "-", "…": "...", "·": "-",
}


def sanitize_text(s: str) -> str:
    """Strip emoji / non-ASCII and any chars that break ffmpeg filtergraphs."""
    if not s:
        return ""
    for k, v in _SMART.items():
        s = s.replace(k, v)
    s = "".join(c for c in s if 32 <= ord(c) < 127)        # drop emoji/non-ascii
    # Drop chars that break unquoted ffmpeg drawtext values: structural symbols,
    # quotes/apostrophes (flip filtergraph quote state) and % (drawtext expansion).
    s = re.sub(r"[\[\]{}<>;=@`\\|'\"%]", "", s)
    return " ".join(s.split()).strip()


def esc(t: str) -> str:
    """Escape a (sanitised) string for an unquoted ffmpeg drawtext value."""
    return (t.replace("'", "\\'").replace(":", "\\:")
             .replace(",", "\\,").replace("%", "\\%"))


def fit_fontsize(text: str, max_size: int, min_size: int,
                 max_width: int = CANVAS_W - 100, ratio: float = 0.40) -> int:
    """Auto-fit a font size so condensed text stays on one line and stays big."""
    n = max(len(text), 1)
    size = int(max_width / (ratio * n))
    return max(min_size, min(max_size, size))


def _font_esc(font_path: str) -> str:
    return font_path.replace("\\", "/").replace(":", "\\\\:")


def _fade_in(dur: float = 0.40, delay: float = 0.0) -> str:
    """drawtext alpha expression: fade 0->1 over `dur`s, after `delay`s."""
    if delay > 0:
        return f"max(0\\,min(1\\,(t-{delay:.2f})/{dur:.2f}))"
    return f"min(1\\,t/{dur:.2f})"


def _drawtext(prev: str, nxt: str, fe: str, text: str, size, color: str,
              x: str, y: str, *, alpha: str | None = None,
              box: str | None = "black@0.45", boxborderw: int = 24,
              enable: str | None = None) -> str:
    """Build one drawtext filter step with an optional background box for legibility."""
    parts = [
        f"fontfile={fe}", f"text={esc(text)}", f"fontsize={size}",
        f"fontcolor={color}", f"x={x}", f"y={y}",
    ]
    if box:
        parts += [f"box=1", f"boxcolor={box}", f"boxborderw={boxborderw}"]
    if alpha is not None:
        parts.append(f"alpha={alpha}")
    if enable is not None:
        parts.append(f"enable={enable}")
    return f"[{prev}]drawtext={':'.join(parts)}[{nxt}]"


# ── FFmpeg helpers ────────────────────────────────────────────────────────────

def _run_ffmpeg(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed:\n{result.stderr[-2000:]}")


# ── Segment builders (each returns (out_path, cmd, duration)) ──────────────────

def _hook_bg(p: VideoPayload) -> tuple[list[str], str]:
    """Shared blurred N-image collage background for every hook variant.
    4 images → 2x2, 6 images → 2x3. Cells tile the full canvas. Returns
    (filter steps, last label = "bg")."""
    n = len(p.images)
    cols = 2
    rows = math.ceil(n / cols)
    cell_w = CANVAS_W // cols
    cell_h = CANVAS_H // rows
    steps = []
    for i in range(n):
        steps.append(
            f"[{i}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=increase,"
            f"crop={cell_w}:{cell_h},setsar=1[q{i}]"
        )
    row_labels = []
    for r in range(rows):
        cells = [f"[q{r * cols + c}]" for c in range(cols) if r * cols + c < n]
        steps.append(f"{''.join(cells)}hstack=inputs={len(cells)}[row{r}]")
        row_labels.append(f"[row{r}]")
    steps.append(f"{''.join(row_labels)}vstack=inputs={rows}[grid]")
    steps.append(
        f"[grid]boxblur=24:2,eq=brightness=-0.28:saturation=1.05,"
        f"scale={CANVAS_W}:{CANVAS_H},setsar=1[bg]"
    )
    return steps, "bg"


def _finish_hook(steps: list[str], prev: str, p: VideoPayload, out: str) -> list[str]:
    """Shared ffmpeg command tail: N looped image inputs → filtergraph → silent mp4."""
    fc = ";".join(steps)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
    for i in range(len(p.images)):
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", str(HOOK_DUR), "-i", p.images[i]]
    cmd += ["-filter_complex", fc, "-map", f"[{prev}]", "-t", str(HOOK_DUR),
            "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-an", out]
    return cmd


def build_hook_classic(p: VideoPayload, tmp: Path) -> tuple[str, list[str], float]:
    """Legacy hook: three static text reveals over the blurred collage. A/B control."""
    out = str(tmp / "00_hook.mp4")
    fe = _font_esc(p.font_path)
    steps, prev = _hook_bg(p)

    brand = "GUESS THE BAND"
    hook = sanitize_text(p.hook) or brand
    sub = f"CAN YOU NAME ALL {len(p.images)}?"

    # Line 1 (brand): drops in from above + fades in.
    bsize = fit_fontsize(brand, 80, 56)
    steps.append(_drawtext(
        prev, "hk0", fe, brand, bsize, "0x64DCFF",
        x="(w-text_w)/2", y="560-90*exp(-t*9)", alpha=_fade_in(0.35),
        box="black@0.40",
    ))
    prev = "hk0"

    # Line 2 (hook): scale-pops in from zero with a gentle ongoing pulse.
    hsize = fit_fontsize(hook, 128, 64)
    hsize_expr = f"{hsize}*(1-exp(-t*16))*(1+0.03*sin(2*PI*t*1.4))"
    steps.append(_drawtext(
        prev, "hk1", fe, hook, hsize_expr, "white",
        x="(w-text_w)/2", y="760", alpha=_fade_in(0.25),
        box="black@0.50", boxborderw=30,
    ))
    prev = "hk1"

    # Animated cyan underline bar that sweeps open beneath the hook.
    steps.append(
        f"[{prev}]drawbox=x='540-430*(1-exp(-t*9))':y=890:"
        f"w='860*(1-exp(-t*9))':h=10:color=0x64DCFF:t=fill[hb]"
    )
    prev = "hb"

    # Line 3 (sub): rises in from below + fades in (slightly delayed).
    ssize = fit_fontsize(sub, 86, 52)
    steps.append(_drawtext(
        prev, "hk2", fe, sub, ssize, "yellow",
        x="(w-text_w)/2", y="1040+90*exp(-t*9)", alpha=_fade_in(0.35, delay=0.25),
        box="black@0.45",
    ))
    prev = "hk2"

    return out, _finish_hook(steps, prev, p, out), HOOK_DUR


def build_hook_kinetic(p: VideoPayload, tmp: Path) -> tuple[str, list[str], float]:
    """Idea #5 — kinetic stakes-UI hook: word-by-word pops + live game UI (score
    counter, difficulty meter, draining timer) so the open loop is *visible* in 1s."""
    out = str(tmp / "00_hook.mp4")
    fe = _font_esc(p.font_path)
    steps, prev = _hook_bg(p)
    n = len(p.images)

    # ── Top game UI ──
    # Score counter chip (top-left): "0/N" — pops in immediately. Static text (no %).
    score = f"0/{n}"
    steps.append(f"[{prev}]drawbox=x=40:y=70:w=190:h=84:color=black@0.55:t=fill[sc0]")
    steps.append(_drawtext(
        "sc0", "sc1", fe, score, 60, "0x64DCFF",
        x="135-text_w/2", y="86", alpha=_fade_in(0.25), box=None,
    ))
    prev = "sc1"

    # Difficulty meter (top, segmented bar): one cell per band, colored by difficulty,
    # revealing left→right. Reuses the puzzle progress-bar drawbox idiom.
    seg_total = CANVAS_W - 320
    seg_gap = 12
    seg_w = (seg_total - seg_gap * (n - 1)) // n
    seg_x0 = 280
    for i in range(n):
        diff = sanitize_text(p.difficulties[i]).lower()
        col = DIFF_COLORS.get(diff, "0x888888")
        sx = seg_x0 + i * (seg_w + seg_gap)
        reveal = 0.15 + i * 0.10   # staggered left→right reveal
        steps.append(
            f"[{prev}]drawbox=x={sx}:y=96:w={seg_w}:h=30:color={col}:t=fill:"
            f"enable='gte(t\\,{reveal:.2f})'[dm{i}]"
        )
        prev = f"dm{i}"

    # ── Kinetic hook line (word-by-word vertical pops, centered) ──
    hook = sanitize_text(p.hook) or "GUESS THE BAND"
    words = hook.split()
    if 1 <= len(words) <= 7:
        line_h = 150
        block_h = line_h * len(words)
        y0 = (CANVAS_H - block_h) // 2 - 60
        for i, w in enumerate(words):
            wsize = fit_fontsize(w, 150, 80)
            delay = i * 0.12                       # ~beat-synced stagger (100bpm)
            scale = f"{wsize}*(1-exp(-max(0\\,t-{delay:.2f})*18))"
            steps.append(_drawtext(
                prev, f"kw{i}", fe, w, scale, "white",
                x="(w-text_w)/2", y=str(y0 + i * line_h),
                alpha=_fade_in(0.18, delay=delay), box="black@0.45", boxborderw=26,
            ))
            prev = f"kw{i}"
    else:
        # Long/empty hook → single whole-line scale-pop fallback.
        hsize = fit_fontsize(hook, 120, 60)
        steps.append(_drawtext(
            prev, "kwl", fe, hook, f"{hsize}*(1-exp(-t*16))", "white",
            x="(w-text_w)/2", y="820", alpha=_fade_in(0.22),
            box="black@0.50", boxborderw=30,
        ))
        prev = "kwl"

    # ── Draining timer bar (the visible "open loop": time is running) ──
    # Bar, not a counting number — live numeric text needs %{eif} expansion and
    # `%` is stripped/escaped per the drawtext footgun. Geometry-only is reliable.
    barw = CANVAS_W - 160
    steps.append(f"[{prev}]drawbox=x=80:y=1560:w={barw}:h=22:color=white@0.20:t=fill[tb0]")
    steps.append(
        f"[tb0]drawbox=x=80:y=1560:w='{barw}*max(0\\,1-t/{HOOK_DUR})':h=22:"
        f"color=0xE74C3C:t=fill[tb1]"
    )
    prev = "tb1"

    # Sub line below the timer — reinforces the challenge.
    sub = f"CAN YOU NAME ALL {n}?"
    ssize = fit_fontsize(sub, 80, 50)
    steps.append(_drawtext(
        prev, "ksub", fe, sub, ssize, "yellow",
        x="(w-text_w)/2", y="1620", alpha=_fade_in(0.35, delay=0.30),
        box="black@0.45",
    ))
    prev = "ksub"

    return out, _finish_hook(steps, prev, p, out), HOOK_DUR


def build_hook_cmd(p: VideoPayload, tmp: Path) -> tuple[str, list[str], float]:
    """Dispatch to the payload's hook_variant (defaults to kinetic)."""
    builder = {"classic": build_hook_classic,
               "kinetic": build_hook_kinetic}.get(p.hook_variant, build_hook_kinetic)
    return builder(p, tmp)


def build_puzzle_cmd(p: VideoPayload, i: int, tmp: Path) -> tuple[str, list[str], float]:
    out = str(tmp / f"{i*2+1:02d}_puzzle{i}.mp4")
    fe = _font_esc(p.font_path)
    n_frames = int(PUZZLE_DUR * FPS)

    zoom = f"1+({ZOOM_FACTOR - 1.0})*on/{n_frames}"
    steps = [
        f"[0:v]scale=8000:-1,"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={n_frames}:s={CANVAS_W}x{CANVAS_H}:fps={FPS},setsar=1[kb]"
    ]
    prev = "kb"

    # Progress bar (top): background + filled portion for slide i (1-indexed)
    barw = CANVAS_W - 80
    fillw = int(barw * (i + 1) / 4)
    steps.append(f"[{prev}]drawbox=x=40:y=44:w={barw}:h=14:color=white@0.25:t=fill[pbg]")
    steps.append(f"[pbg]drawbox=x=40:y=44:w={fillw}:h=14:color=0x64DCFF:t=fill[pf]")
    prev = "pf"

    # Difficulty badge (top-right)
    diff = sanitize_text(p.difficulties[i]).lower()
    if diff:
        dtext = diff.upper()
        col = DIFF_COLORS.get(diff, "0x888888")
        fs = 44
        bw = int(len(dtext) * fs * 0.60) + 50
        bx = CANVAS_W - 40 - bw
        steps.append(f"[{prev}]drawbox=x={bx}:y=74:w={bw}:h=70:color={col}@0.92:t=fill[db]")
        steps.append(
            f"[db]drawtext=fontfile={fe}:text={esc(dtext)}:fontsize={fs}:"
            f"fontcolor=black:x={bx + 25}:y=86[dt]"
        )
        prev = "dt"

    # Kinetic title: fade in + settle upward, on a dark legibility box.
    # Conservative width budget so even long legacy titles stay within frame.
    title = sanitize_text(p.titles[i]) or "WHAT BAND IS THIS?"
    tsize = fit_fontsize(title, 118, 46, max_width=CANVAS_W - 150, ratio=0.42)
    steps.append(_drawtext(
        prev, "ti", fe, title, tsize, "white",
        x="(w-text_w)/2", y="210+50*(1-min(1\\,t/0.40))",
        alpha=_fade_in(), box="black@0.45", boxborderw=28,
    ))
    prev = "ti"

    # Caption near the bottom — on a strong dark box so yellow is readable.
    # Conservative width budget + ratio so even long captions stay within frame.
    caption = sanitize_text(p.captions[i]) or "COMMENT YOUR GUESS"
    csize = fit_fontsize(caption, 80, 44, max_width=CANVAS_W - 180, ratio=0.46)
    steps.append(_drawtext(
        prev, "ca", fe, caption, csize, "yellow",
        x="(w-text_w)/2", y=str(CANVAS_H - 380),
        alpha=_fade_in(), box="black@0.60", boxborderw=20,
    ))
    prev = "ca"

    # Countdown 3-2-1 in the last COUNTDOWN_N seconds
    for n in range(1, COUNTDOWN_N + 1):
        t_s = max(0.0, PUZZLE_DUR - n)
        t_e = float(PUZZLE_DUR - n + 1)
        nxt = f"cd{n}"
        enable = f"gte(t\\,{t_s:.3f})*lte(t\\,{t_e:.3f})"
        steps.append(
            f"[{prev}]drawtext=fontfile={fe}:text={n}:fontsize=280:"
            f"fontcolor=yellow@0.92:x=(w-text_w)/2:y=(h-text_h)/2:"
            f"enable={enable}[{nxt}]"
        )
        prev = nxt

    fc = ";".join(steps)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
           "-loop", "1", "-framerate", str(FPS), "-i", p.images[i],
           "-filter_complex", fc, "-map", f"[{prev}]", "-t", str(PUZZLE_DUR),
           "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-an", out]
    return out, cmd, PUZZLE_DUR


def build_reveal_cmd(p: VideoPayload, i: int, tmp: Path) -> tuple[str, list[str], float]:
    out = str(tmp / f"{i*2+2:02d}_reveal{i}.mp4")
    fe = _font_esc(p.font_path)
    n_frames = int(REVEAL_DUR * FPS)

    zoom = f"1+0.12*on/{n_frames}"
    steps = [
        f"[0:v]scale=8000:-1,"
        f"zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={n_frames}:s={CANVAS_W}x{CANVAS_H}:fps={FPS},setsar=1[kb]"
    ]
    # Darken so the answer pops
    steps.append(f"[kb]drawbox=x=0:y=0:w={CANVAS_W}:h={CANVAS_H}:color=black@0.5:t=fill[dk]")
    prev = "dk"

    name = sanitize_text(p.band_names[i]) or "???"
    nsize = fit_fontsize(name, 160, 80)

    steps.append(_drawtext(
        prev, "rv0", fe, "THE ANSWER", fit_fontsize("THE ANSWER", 66, 50),
        "0x64DCFF", x="(w-text_w)/2", y="640", alpha=_fade_in(0.3), box="black@0.35",
    ))
    # Band name slams in with a scale-pop
    steps.append(_drawtext(
        "rv0", "rv1", fe, name, f"{nsize}*(1-exp(-t*18))", "white",
        x="(w-text_w)/2", y="880", alpha=_fade_in(0.25),
        box="black@0.45", boxborderw=30,
    ))
    steps.append(_drawtext(
        "rv1", "rv2", fe, "DID YOU GET IT?", fit_fontsize("DID YOU GET IT?", 62, 46),
        "yellow", x="(w-text_w)/2", y="1180", alpha=_fade_in(0.5, delay=0.3),
        box="black@0.45",
    ))
    prev = "rv2"

    fc = ";".join(steps)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
           "-loop", "1", "-framerate", str(FPS), "-i", p.images[i],
           "-filter_complex", fc, "-map", f"[{prev}]", "-t", str(REVEAL_DUR),
           "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-an", out]
    return out, cmd, REVEAL_DUR


def build_outro_cmd(p: VideoPayload, tmp: Path) -> tuple[str, list[str], float]:
    out = str(tmp / "99_outro.mp4")
    fe = _font_esc(p.font_path)
    steps = [f"color=c=0x0A0A0A:s={CANVAS_W}x{CANVAS_H}:r={FPS}[bg]"]
    cy = CANVAS_H // 2
    overlays = [
        ("FOLLOW FOR MORE",          fit_fontsize("FOLLOW FOR MORE", 112, 70), "yellow",   cy - 280, 0.00),
        ("COMMENT YOUR FAVORITE BAND", fit_fontsize("COMMENT YOUR FAVORITE BAND", 84, 52), "white", cy - 40, 0.20),
        ("LIKE IF YOU GOT THEM",     fit_fontsize("LIKE IF YOU GOT THEM", 72, 46), "0x64DCFF", cy + 200, 0.40),
    ]
    prev = "bg"
    for j, (txt, sz, col, y, delay) in enumerate(overlays):
        nxt = f"o{j}"
        # Staggered rise-in: each line slides up into place as it fades in.
        y_expr = f"{y}+70*exp(-max(0\\,t-{delay:.2f})*8)"
        steps.append(_drawtext(
            prev, nxt, fe, txt, sz, col, x="(w-text_w)/2", y=y_expr,
            alpha=_fade_in(0.35, delay=delay), box="black@0.0", boxborderw=0,
        ))
        prev = nxt

    fc = ";".join(steps)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
           "-f", "lavfi", "-i", f"color=c=black:s={CANVAS_W}x{CANVAS_H}:r={FPS}:d={OUTRO_DUR}",
           "-filter_complex", fc, "-map", f"[{prev}]", "-t", str(OUTRO_DUR),
           "-r", str(FPS), "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-an", out]
    return out, cmd, OUTRO_DUR


# ── Assembly: xfade chain + audio mux ─────────────────────────────────────────

def assemble_xfade(paths: list[str], durs: list[float], out_path: str) -> None:
    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", p]

    steps = []
    prev = "0:v"
    cum = durs[0]
    for k in range(1, len(paths)):
        off = cum - XFADE
        lbl = f"vx{k}"
        steps.append(
            f"[{prev}][{k}:v]xfade=transition={TRANSITION}:"
            f"duration={XFADE}:offset={off:.3f}[{lbl}]"
        )
        prev = lbl
        cum = cum + durs[k] - XFADE

    fc = ";".join(steps)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", *inputs,
           "-filter_complex", fc, "-map", f"[{prev}]", "-r", str(FPS),
           "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-pix_fmt", "yuv420p", out_path]
    log.info("assemble", segments=len(paths), out=Path(out_path).name)
    _run_ffmpeg(cmd, "xfade assembly")


def _segment_starts(durs: list[float]) -> tuple[list[float], float]:
    """Final-timeline start time of each segment's crossfade-in, plus total."""
    starts = [0.0]
    L = durs[0]
    for k in range(1, len(durs)):
        starts.append(L - XFADE)
        L = L + durs[k] - XFADE
    return starts, L


def mux_audio(video_in: str, out_path: str, p: VideoPayload, durs: list[float]) -> None:
    """Mix a looping music bed + reveal whooshes/dings + outro pad over the video."""
    if not Path(p.beat_sound).exists():
        log.warning("no_beat_sound", path=p.beat_sound)
        shutil.copy(video_in, out_path)
        return

    starts, total = _segment_starts(durs)
    # Layout: [hook] + N*(puzzle, reveal) + [outro]. Derive event slots from N.
    n_bands = (len(durs) - 2) // 2
    puzzle_idxs = [1 + 2 * i for i in range(n_bands)]   # slide entrances
    reveal_idxs = [2 + 2 * i for i in range(n_bands)]   # reveal segments
    outro_idx = len(durs) - 1

    events: list[tuple[str, float, float]] = []
    # First-frame attention sting under the hook — sound is half the hook on TikTok.
    if Path(p.sting_sound).exists():
        events.append((p.sting_sound, 0.0, 0.9))
    # Record scratch when each new slide comes in
    for idx in puzzle_idxs:
        if idx < len(durs) and Path(p.scratch_sound).exists():
            events.append((p.scratch_sound, max(0.0, starts[idx] - 0.08), 0.6))
    for idx in reveal_idxs:
        if idx >= len(durs):
            continue
        if Path(p.whoosh_sound).exists():
            events.append((p.whoosh_sound, max(0.0, starts[idx] - 0.05), 0.5))
        if Path(p.ding_sound).exists():
            events.append((p.ding_sound, starts[idx] + XFADE + 0.05, 0.6))
    if Path(p.outro_sound).exists():
        events.append((p.outro_sound, max(0.0, starts[outro_idx]), 0.7))

    inputs = ["-i", video_in, "-stream_loop", "-1", "-i", p.beat_sound]
    parts = [f"[1:a]volume=0.32,atrim=0:{total:.3f}[bed]"]
    labels = ["[bed]"]
    idx = 2
    for (path, at, vol) in events:
        inputs += ["-i", path]
        ms = int(max(0.0, at) * 1000)
        parts.append(f"[{idx}:a]adelay={ms}|{ms},volume={vol}[e{idx}]")
        labels.append(f"[e{idx}]")
        idx += 1
    parts.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,"
        f"alimiter=limit=0.95[aout]"
    )
    fc = ";".join(parts)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", *inputs,
           "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", out_path]
    log.info("mux_audio", events=len(events), total=f"{total:.1f}s")
    _run_ffmpeg(cmd, "audio mux")


# ── Master render ─────────────────────────────────────────────────────────────

def render_platform(payload: VideoPayload, platform: str) -> str:
    cfg = PLATFORM_CONFIGS[platform]
    out_path = str(Path(payload.output_dir) / cfg["filename"])
    log.info("render_start", platform=platform, hook_variant=payload.hook_variant)
    t0 = time.time()

    tmp_dir = Path(payload.output_dir) / f"_tmp_{platform}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Build all segment commands in timeline order.
        builders: list[tuple[str, list[str], float]] = [build_hook_cmd(payload, tmp_dir)]
        for i in range(len(payload.images)):
            builders.append(build_puzzle_cmd(payload, i, tmp_dir))
            builders.append(build_reveal_cmd(payload, i, tmp_dir))
        builders.append(build_outro_cmd(payload, tmp_dir))

        # Render every segment in parallel.
        procs = []
        for path, cmd, dur in builders:
            procs.append((path, dur, subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                      stderr=subprocess.PIPE)))
        seg_paths, seg_durs = [], []
        for path, dur, proc in procs:
            _, stderr = proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Segment {Path(path).name} failed:\n{stderr.decode(errors='replace')[-2000:]}"
                )
            seg_paths.append(path)
            seg_durs.append(dur)
        log.info("segments_done", count=len(seg_paths))

        # Crossfade assemble → silent master → mux audio.
        silent = str(tmp_dir / "master_silent.mp4")
        assemble_xfade(seg_paths, seg_durs, silent)
        mux_audio(silent, out_path, payload, seg_durs)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    elapsed = time.time() - t0
    size_mb = Path(out_path).stat().st_size / 1_048_576
    log.info("render_complete", platform=platform, path=out_path,
             elapsed=f"{elapsed:.1f}s", size=f"{size_mb:.1f}MB")
    return out_path


# ── Webhook ───────────────────────────────────────────────────────────────────

def post_webhook(url: str, data: dict) -> None:
    try:
        requests.post(url, json=data, timeout=15).raise_for_status()
        log.info("webhook_sent")
    except Exception as e:
        log.error("webhook_failed", error=str(e))


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--json-stdin", action="store_true")
    p.add_argument("--json-file")
    p.add_argument("--images",       nargs="+")
    p.add_argument("--titles",       nargs="+")
    p.add_argument("--captions",     nargs="+")
    p.add_argument("--band-names",   nargs="+", dest="band_names")
    p.add_argument("--difficulties", nargs="+")
    p.add_argument("--hook",         default="GUESS THE BAND")
    p.add_argument("--cta",          default="FOLLOW FOR DAILY PUZZLES")
    p.add_argument("--output-dir",   default="./output")
    p.add_argument("--beat-sound",   default="./templates/audio/beat.wav",   dest="beat_sound")
    p.add_argument("--ding-sound",   default="./templates/audio/ding.wav",   dest="ding_sound")
    p.add_argument("--whoosh-sound", default="./templates/audio/whoosh.wav", dest="whoosh_sound")
    p.add_argument("--scratch-sound", default="./templates/audio/scratch.wav", dest="scratch_sound")
    p.add_argument("--sting-sound",  default="./templates/audio/sting.wav",  dest="sting_sound")
    p.add_argument("--outro-sound",  default="./templates/audio/outro.wav",  dest="outro_sound")
    p.add_argument("--hook-variant", default=HOOK_VARIANT, dest="hook_variant")
    p.add_argument("--font",         default="./templates/fonts/bold.ttf",   dest="font_path")
    p.add_argument("--webhook-url",  default=None)
    p.add_argument("--platforms",    nargs="+", default=list(PLATFORM_CONFIGS.keys()),
                   choices=list(PLATFORM_CONFIGS.keys()))
    return p.parse_args()


def build_payload(args: argparse.Namespace) -> VideoPayload:
    if args.json_stdin:
        raw = json.load(sys.stdin)
    elif args.json_file:
        with open(args.json_file) as f:
            raw = json.load(f)
    else:
        if not (args.images and args.titles and args.captions):
            raise ValueError("Provide --images, --titles, --captions")
        raw = dict(
            images=args.images, titles=args.titles, captions=args.captions,
            band_names=args.band_names or [], difficulties=args.difficulties or [],
            hook=args.hook, cta=args.cta, output_dir=args.output_dir,
            beat_sound=args.beat_sound, ding_sound=args.ding_sound,
            whoosh_sound=args.whoosh_sound, scratch_sound=args.scratch_sound,
            sting_sound=args.sting_sound, outro_sound=args.outro_sound,
            hook_variant=args.hook_variant,
            font_path=args.font_path, webhook_url=args.webhook_url,
            platforms=args.platforms,
        )
    # Drop unknown keys so a rich content JSON doesn't crash the dataclass.
    allowed = VideoPayload.__dataclass_fields__.keys()
    raw = {k: v for k, v in raw.items() if k in allowed}
    return VideoPayload(**raw)


def main() -> None:
    args = parse_args()
    try:
        payload = build_payload(args)
        payload.validate()
    except (ValueError, FileNotFoundError, AssertionError) as e:
        log.error("invalid", error=str(e)); sys.exit(1)

    results, errors = {}, {}
    for platform in payload.platforms:
        try:
            results[platform] = render_platform(payload, platform)
        except Exception as e:
            log.exception("failed", platform=platform, error=str(e))
            errors[platform] = str(e)

    summary = {
        "status": "error" if errors and not results else ("partial" if errors else "ok"),
        "outputs": results, "errors": errors,
    }
    if payload.webhook_url:
        post_webhook(payload.webhook_url, summary)

    print(json.dumps(summary, indent=2))
    sys.exit(1 if errors and not results else 0)


if __name__ == "__main__":
    main()
