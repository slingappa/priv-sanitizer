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

For a physical-camera or whiteboard recording, opt into the experimental
detector explicitly:

```bash
priv-sanitizer -w sanitize --video INPUT.mp4 --output OUTPUT_DIR
```

The executable `./priv-sanitizer` in this directory is a shorter wrapper for
the same CLI. It uses the native Python runtime by default and forwards all
arguments. Set `PRIV_SANITIZER_USE_DOCKER=1` to opt into Docker explicitly.

The executable `./priv-sanitizer-docker` is the direct Docker wrapper. After
`bash scripts/install.sh --docker-only`, call it from the recording folder to
run the configured image without writing a `docker run` command.

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
`outputs_sanitized/<video-stem>.mp4`. Batch removes generated `artifacts/`
contents by default after delivery verification; pass `-k`/`--keep-artifacts`
to retain manifests, reports, and audit videos for debugging. The legacy
`--keep-videos` spelling remains an alias. Same-stem DOCX/SRT/VTT files are
paired automatically; a transcript is never required for baseline processing
and is not rewritten or emitted.

For a new machine, run `bash scripts/install.sh` once. The installer provisions
the Python environment, FFmpeg/FFprobe, Tesseract OCR, and OpenCV vision
dependencies, and installs persistent user launchers in `~/.local/bin`; the
`priv-sanitizer` command then works from any recording directory without
activating a virtualenv. The installer persists that directory in
`~/.profile.local` for future terminals. `source scripts/activate.sh` remains
available for debugging or direct virtual-environment use.
If you use that script, source it rather than executing `./scripts/activate.sh`
so its environment changes persist in the current shell.
For a Docker-only setup that avoids host Python dependencies, run
`bash scripts/install.sh --docker-only`; use `priv-sanitizer-docker` from the
recording folder to select the configured image. The regular `priv-sanitizer`
launcher remains native-default and requires the host Python environment.

The workflow has two passes:

1. Baseline: video-only input. Preserve audio by default, blur participant visuals, skip the expensive audio scan unless explicitly enabled, and report that spoken-name privacy is unresolved without timed evidence. Do not block on a missing transcript.
2. Hardening: optionally provide the original SRT, WebVTT, or DOCX transcript. Use it as secondary evidence for names, speaker labels, and timing. Audio remains unchanged by default; `--experimental-audio` explicitly opts in to short matched-name mute windows. Do not rewrite or emit a sanitized transcript.

When a transcript is available, both analysis passes complete before rendering
the final transcript-aware manifest, so batch performs one full-length encode.
The baseline directory remains analysis-only in that successful path. If
hardening fails or its render fails verification, batch renders the baseline
manifest once as a fallback. A transcript is never used as a reason to encode
the full recording twice.

The detector stack records per-segment layout-model evidence, OCR evidence
(Tesseract when installed), and OpenCV Haar evidence when installed. Normal
meeting mode now uses a 2-fps frame-difference-triggered face scan by default,
multi-cascade temporal tracking, and bounded safety gaps for fullscreen
single/multi-face scenes and static avatars. Use `-w`/`--whiteboard` to opt into
the experimental physical-camera path; that mode uses a denser default scan
and localized feathered masks for whiteboards and arbitrary camera layouts.
`--balanced` and `--face-padding` tune that localized mode. The single
`-b`/`--blur-strength` control applies to both renderers and accepts `0`–`1` or
`0%`–`100%`: `0%` leaves visual regions unchanged and `100%` applies the
strongest opaque blur. Lower values are weaker review settings; the default is
`100%`. Older radius/opacity flags remain compatibility-only aliases.
Missing optional backends are reported as unresolved coverage; they are never
silently treated as successful detection.

Audio redaction is experimental and off by default. Enable it explicitly with
`--experimental-audio` for `batch` or `harden`; otherwise audio is preserved.
The full-length speech-activity scan is also off by default; enable it with
`PRIV_SANITIZER_AUDIO_ANALYSIS=1` when diagnostic VAD evidence is needed.
Audio-level verification follows the same opt-in policy while stream presence
and duration checks remain enabled.

Rendering defaults to 8 FFmpeg threads with the `ultrafast` x264 preset. Use
`-j N`/`--jobs N` to select the CPU/thread count for a run, or set
`PRIV_SANITIZER_FFMPEG_THREADS` and `PRIV_SANITIZER_X264_PRESET` to override
those defaults through an integration.

Diagnostics and periodic heartbeats are enabled by default and written to
`verbose.log` in the caller's working directory; detailed command lines and
heartbeats are log-only. `-v`/`--verbose` explicitly forces this mode, `PRIV_SANITIZER_VERBOSE=0` disables it, and
`PRIV_SANITIZER_HEARTBEAT_SECONDS` controls the heartbeat interval. Progress
ETAs are ballpark estimates refreshed when the percentage advances; spinner
heartbeats do not make a stable percentage's ETA drift.

The renderer preserves source edits and single-frame scene cuts. A bright
flash can remain visible in the delivery even when the entire frame is under
an active blur region; review the source alongside the output and use the
timestamped `visual_redactions` entries to distinguish an editorial flash from
a missed mask. Removing the flash itself is outside privacy masking.

For staged work, read [references/workflow.md](references/workflow.md). For privacy decisions, read [references/policy.md](references/policy.md).

Always review the rendered delivery before treating it as sanitized. When
`-k`/`--keep-artifacts` is used, also review `redaction-manifest.json` and
`report.txt`; report unresolved visual or audio coverage instead of claiming
complete privacy.
