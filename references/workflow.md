# Workflow

## Folder batch

For a Docker-only installation, run `bash scripts/install.sh --docker-only`
once. The repository launcher automatically uses the resulting image for later
runs. The explicit `priv-sanitizer-docker` wrapper is also available. Set
`PRIV_SANITIZER_USE_DOCKER=0` to use local Python temporarily through the
regular launcher.

From a directory containing recordings and optional same-stem transcripts:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$PWD}"
video_dir="${VIDEO_DIR:-$PWD}"
cd "$video_dir"
"$tool_root/priv-sanitizer"
```

With no command arguments, the launcher scans only the current directory and
is equivalent to:

```bash
priv-sanitizer batch --input . --output ./outputs_sanitized --artifacts ./artifacts
```

Every video gets a baseline pass. A same-stem `.docx`, `.srt`, or `.vtt`
transcript triggers the optional hardening pass. The final selected video and
small per-pass audit artifacts are stored under `artifacts/<video-stem>/`, the
flat final video is `outputs_sanitized/<video-stem>.mp4`, and the batch summary
is `artifacts/batch-report.json`. Intermediate per-pass MP4s are removed from
`artifacts/` by default; pass `-k`/`--keep-videos` to retain them.

Audio is preserved by default. To opt in to experimental transcript-based name
muting, add `--experimental-audio` to the batch command.

## Baseline

```bash
priv-sanitizer sanitize --video input.mp4 --output output/baseline
```

## Optional hardening

```bash
priv-sanitizer harden output/baseline/job \
  --transcript transcript.docx \
  --job output/hardened/job
priv-sanitizer render output/hardened/job --output output/hardened
```

For experimental audio muting, add `--experimental-audio` to the `harden`
command before rendering.

Review `redaction-manifest.json`, `report.txt`, and `analysis.json` before distributing the output.
