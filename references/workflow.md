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
transcript triggers the optional hardening pass. The final selected video is
`outputs_sanitized/<video-stem>.mp4`. Generated `artifacts/` contents are
removed by default; pass `-k`/`--keep-artifacts` to retain audit data for
debugging (`--keep-videos` is a compatibility alias). When a transcript is
paired, baseline and hardening analysis finish before one final
transcript-aware render; the baseline render is only a fallback if hardening
fails.

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
