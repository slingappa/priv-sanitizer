# priv-sanitizer

Local-first meeting-video privacy sanitizer with a video-only baseline pass and an optional transcript-assisted hardening pass.

## Quick start

Run these commands from the folder containing the videos. Set `VIDEO_DIR` if
the recordings are stored elsewhere.

```bash
export VIDEO_DIR="${VIDEO_DIR:-$PWD}"
export TOOL_DIR="${TOOL_DIR:-$HOME/tools/priv-sanitizer}"

git clone https://github.com/slingappa/priv-sanitizer.git "$TOOL_DIR"
cd "$TOOL_DIR"
bash scripts/install.sh
source scripts/activate.sh

cd "$VIDEO_DIR"
"$PRIV_SANITIZER_ROOT/priv-sanitizer"
```

The no-argument batch run discovers videos in `VIDEO_DIR`. Final sanitized
files are written to `VIDEO_DIR/outputs_sanitized/`; audit manifests, reports,
and intermediate pass outputs are written to `VIDEO_DIR/artifacts/`.

For usage examples after installation:

```bash
priv-sanitizer --help
priv-sanitizer batch --help
```

## Baseline

### Installation

On Debian/Ubuntu, install the complete runtime—including FFmpeg, FFprobe,
Tesseract OCR, OpenCV, and the Python environment—with:

```bash
TOOL_DIR="${PRIV_SANITIZER_ROOT:-$PWD}"
cd "$TOOL_DIR"
bash scripts/install.sh
source scripts/activate.sh
```

After installation, use the repository-local launcher to avoid repeating the
Python module invocation:

```bash
TOOL_DIR="${PRIV_SANITIZER_ROOT:-$PWD}"
VIDEO_DIR="${VIDEO_DIR:-$PWD}"
"$TOOL_DIR/priv-sanitizer" --help
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer"
```

It automatically uses the project virtual environment and local
FFmpeg/Tesseract configuration while preserving the caller's current directory
for batch input. With no arguments, it explicitly runs
`batch --input "$PWD" --output "$PWD/outputs_sanitized" --artifacts
"$PWD/artifacts"`. Run `priv-sanitizer --help` for copy-paste examples.

The installer creates `.venv/` inside the tool directory. Use
`bash scripts/install.sh --dry-run` to inspect actions, or
`--no-system-packages` when FFmpeg and Tesseract are already installed on
`PATH`. When passwordless `sudo` is unavailable, the installer can extract a
project-local Tesseract runtime under `.tools/tesseract`; the activation
script sets its binary, library, and language-data paths. It also honors
`PRIV_SANITIZER_FFMPEG_DIR` for a project-local FFmpeg installation.

The baseline does not require a transcript:

```bash
python3 -m priv_sanitizer sanitize \
  --video "${VIDEO_DIR:-$PWD}/input.mp4" \
  --output "${VIDEO_DIR:-$PWD}/artifacts/input-baseline"
```

The CLI reports progress for frame sampling, audio analysis, and video
rendering. Set `PRIV_SANITIZER_QUIET=1` to suppress progress messages.

## Batch mode

From a folder containing videos and optional same-stem transcripts, run the
launcher with no arguments:

```bash
TOOL_DIR="${PRIV_SANITIZER_ROOT:-$PWD}"
VIDEO_DIR="${VIDEO_DIR:-$PWD}"
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer"
```

This is equivalent to `python3 -m priv_sanitizer batch --input . --output
./outputs_sanitized --artifacts ./artifacts`. It scans only that folder for `.mp4`, `.mov`, `.avi`, `.mkv`,
`.webm`, and `.m4v` files. Each video receives a video-only baseline pass. If
there is an unambiguous same-stem `.docx`, `.srt`, or `.vtt` transcript, the
optional hardening pass runs afterward and becomes the selected final output.
Missing or ambiguous transcripts do not block baseline processing. Audio is
preserved by default, including when transcript hardening runs.

Final files are written directly as `outputs_sanitized/<video-stem>.mp4`.
Detailed artifacts are under `artifacts/<video-stem>/`, with `baseline/`,
optional `hardened/`, and `artifacts/batch-report.json`.

FFmpeg rendering defaults to 8 threads and the `ultrafast` x264 preset for a
fast run on an 8-core machine. Override them with
`PRIV_SANITIZER_FFMPEG_THREADS` and `PRIV_SANITIZER_X264_PRESET` when using a
different machine or sharing CPU resources. OCR and face analysis are also
dispatched concurrently. More threads are not always faster when another
FFmpeg job is running.

The render metadata records the selected thread count and x264 preset. CPU
usage above 100% in the shell's `time` output confirms parallel execution;
actual utilization may be limited by the container CPU quota, filters, or
storage bandwidth.

It detects a conservative participant-panel region and runs local layout/OCR/face detectors when available. Audio is preserved by default; speech activity is recorded as evidence only and is never used to mute an entire meeting.

## Optional hardening

```bash
VIDEO_DIR="${VIDEO_DIR:-$PWD}"
python3 -m priv_sanitizer harden "$VIDEO_DIR/artifacts/input-baseline/job" \
  --transcript "$VIDEO_DIR/input.docx" \
  --job "$VIDEO_DIR/artifacts/input-hardened/job"
python3 -m priv_sanitizer render "$VIDEO_DIR/artifacts/input-hardened/job" \
  --output "$VIDEO_DIR/artifacts/input-hardened"
```

The transcript is secondary evidence only. It is not rewritten or emitted. By
default, hardening uses transcript evidence for analysis and keeps audio
unchanged. To opt in to experimental short, review-required mute windows
around transcript name matches, add `--experimental-audio` to the batch
command or pass `--experimental-audio` to `harden`. It does not mute all speech
in a cue. For an extracted clip, pass the source timestamp offset with
`--transcript-offset`.

FFmpeg and FFprobe must be available on `PATH` or configured explicitly. Pillow is required. OCR uses
`pytesseract` plus the `tesseract` executable when installed; face detection
uses `opencv-python-headless` when installed. Both are optional, and their
availability and detections are written to `analysis.json`.

For a non-system FFmpeg installation, configure it once per shell (or add
these exports to your shell profile):

```bash
export PRIV_SANITIZER_FFMPEG_DIR="${PRIV_SANITIZER_FFMPEG_DIR:-$PWD/.tools/ffmpeg/bin}"
# Alternatively set PRIV_SANITIZER_FFMPEG and PRIV_SANITIZER_FFPROBE directly.
```

On Debian/Ubuntu, the usual installation is:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
```

For the full local vision stack, install the Python extras with
`pip install -e '.[vision]'` and install the system Tesseract language data
separately. The tool still runs without those extras using its auditable local
layout detector and participant-panel blur fallback.

Review `redaction-manifest.json`, `analysis.json`, and `report.txt` before distributing any output.
