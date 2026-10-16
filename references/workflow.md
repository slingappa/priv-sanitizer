# Workflow

## Folder batch

From a directory containing recordings and optional same-stem transcripts:

```bash
TOOL_DIR="${PRIV_SANITIZER_ROOT:-$PWD}"
VIDEO_DIR="${VIDEO_DIR:-$PWD}"
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer"
```

With no command arguments, the launcher scans only the current directory and
is equivalent to:

```bash
priv-sanitizer batch --input . --output ./outputs_sanitized --artifacts ./artifacts
```

Every video gets a baseline pass. A same-stem `.docx`, `.srt`, or `.vtt`
transcript triggers the optional hardening pass. The final selected video and
per-pass audit artifacts are stored under `artifacts/<video-stem>/`, the flat
final video is `outputs_sanitized/<video-stem>.mp4`, and the batch summary is
`artifacts/batch-report.json`.

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

Review `redaction-manifest.json`, `report.txt`, and `analysis.json` before distributing the output.
