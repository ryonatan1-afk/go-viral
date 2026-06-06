# Audio Assets

Place the following files here before running the pipeline:

| File | Purpose | Recommended specs |
|------|---------|-------------------|
| `tick.mp3` | Clock ticking layered over each puzzle slide | ~5s loop, 44.1kHz, mono |
| `outro.mp3` | Background beat for the 5-second CTA outro | ~10s loop, 44.1kHz, stereo |

## Free sources

- **Freesound.org** – search "clock ticking loop" (CC0 licence)
- **Pixabay** – royalty-free music for `outro.mp3`
- **Zapsplat** – registration required, CC licence

The pipeline degrades gracefully if these files are absent (video renders silently).
