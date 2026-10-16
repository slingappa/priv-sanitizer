---
name: priv-sanitizer
description: Sanitize meeting-recording video locally by masking participant visuals/names and protecting participant audio. A transcript is optional secondary evidence for a later hardening pass; video-only sanitization must work without it.
---

# priv-sanitizer

Use this skill when the user wants a privacy-reduced copy of a meeting recording while preserving shared-screen content where possible.

The implementation lives beside this file. Run it through the CLI:

```bash
python3 -m priv_sanitizer sanitize --video INPUT.mp4 --output OUTPUT_DIR
```

The executable `./priv-sanitizer` in this directory is a shorter wrapper for
the same CLI. It selects the project runtime automatically and forwards all
arguments.

When launched with no arguments from a folder containing recordings, it
discovers the supported videos in that folder and writes a two-pass batch under
`outputs_sanitized/`:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$PWD}"
video_dir="${VIDEO_DIR:-$PWD}"
cd "$video_dir"
"$tool_root/priv-sanitizer"
# equivalent: python3 -m priv_sanitizer batch --input . --output ./outputs_sanitized --artifacts ./artifacts
```

Each recording gets a flat final file at
`outputs_sanitized/<video-stem>.mp4`, detailed `artifacts/<video-stem>/baseline/`
and optional `hardened/` artifacts, and the overall `artifacts/batch-report.json`.
Same-stem DOCX/SRT/VTT files are paired automatically; a transcript is never
required for baseline processing and is not rewritten or emitted.

For a new machine, run `bash scripts/install.sh` and then
`source scripts/activate.sh` first. The installer provisions the Python
environment, FFmpeg/FFprobe, Tesseract OCR, and OpenCV vision dependencies.
For a Docker-only setup that avoids host Python dependencies, run
`bash scripts/install.sh --docker-only`; the repository launcher then uses the
configured image automatically.

The workflow has two passes:

1. Baseline: video-only input. Preserve audio by default, blur participant visuals, and report that spoken-name privacy is unresolved without timed evidence. Do not block on a missing transcript.
2. Hardening: optionally provide the original SRT, WebVTT, or DOCX transcript. Use it as secondary evidence for names, speaker labels, and timing. Audio remains unchanged by default; `--experimental-audio` explicitly opts in to short matched-name mute windows. Do not rewrite or emit a sanitized transcript.

The detector stack records local layout-model evidence, OCR evidence (Tesseract
when installed), and face-detection evidence (OpenCV Haar when installed).
Missing optional backends are reported as unresolved coverage; they are never
silently treated as successful detection.

Audio redaction is experimental and off by default. Enable it explicitly with
`--experimental-audio` for `batch` or `harden`; otherwise audio is preserved.

Rendering defaults to 8 FFmpeg threads with the `ultrafast` x264 preset. Set
`PRIV_SANITIZER_FFMPEG_THREADS` or `PRIV_SANITIZER_X264_PRESET` to override
those defaults.

For staged work, read [references/workflow.md](references/workflow.md). For privacy decisions, read [references/policy.md](references/policy.md).

Always review `redaction-manifest.json` and `report.txt` before treating the output as sanitized. Report unresolved visual or audio coverage instead of claiming complete privacy.
