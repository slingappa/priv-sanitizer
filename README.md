# priv-sanitizer

Local-first meeting-video privacy sanitizer with a video-only baseline pass and an optional transcript-assisted hardening pass.

## Quick start

Run these commands from the folder containing the videos. Set `VIDEO_DIR` if
the recordings are stored elsewhere.

```bash
export VIDEO_DIR="${VIDEO_DIR:-$PWD}"

git clone https://github.com/slingappa/priv-sanitizer.git "$HOME/tools/priv-sanitizer"
cd "$HOME/tools/priv-sanitizer"
bash scripts/install.sh
source scripts/activate.sh

cd "$VIDEO_DIR"
"$HOME/tools/priv-sanitizer/priv-sanitizer"
```

The no-argument batch run discovers videos in `VIDEO_DIR`. Final sanitized
files are written to `VIDEO_DIR/outputs_sanitized/`; audit manifests, reports,
and intermediate pass outputs are written to `VIDEO_DIR/artifacts/`.

For usage examples after installation:

```bash
priv-sanitizer --help
priv-sanitizer batch --help
```

For a Docker-only installation, skip host Python setup and let the installer
build the image and configure the launcher to use it:

```bash
cd "$HOME/tools/priv-sanitizer"
bash scripts/install.sh --docker-only
cd "$VIDEO_DIR"
"$HOME/tools/priv-sanitizer/priv-sanitizer-docker"
```

Use `PRIV_SANITIZER_USE_DOCKER=0` before a launcher invocation to temporarily
force the local Python runtime, or `PRIV_SANITIZER_USE_DOCKER=1` to require the
configured Docker image.

## Docker quick start

Docker packages Python, FFmpeg, Tesseract, OpenCV, and the sanitizer runtime
into one image. The recommended setup is `bash scripts/install.sh
--docker-only`; to build manually instead:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$HOME/tools/priv-sanitizer}"
video_dir="${VIDEO_DIR:-$PWD}"
cd "$tool_root"
docker build -t priv-sanitizer:local .
```

Run it from the folder containing the videos:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$HOME/tools/priv-sanitizer}"
video_dir="${VIDEO_DIR:-$PWD}"
cd "$video_dir"
"$tool_root/priv-sanitizer-docker"
```

The Docker launcher uses the same no-argument batch behavior: final videos
are written to `VIDEO_DIR/outputs_sanitized/` and audit artifacts to
`VIDEO_DIR/artifacts/`. Audio remains preserved by default. Add
`--experimental-audio` to the launcher command to opt in to experimental
transcript-based audio muting.

For long conversions, add `-v` (or `--verbose`). This writes detailed command,
stage, percentage, and heartbeat diagnostics to `VIDEO_DIR/verbose.log` while
also showing them on stderr. Heartbeats default to every 15 seconds; override
that with `PRIV_SANITIZER_HEARTBEAT_SECONDS`.

```bash
PRIV_SANITIZER_HEARTBEAT_SECONDS=5 \
  "$tool_root/priv-sanitizer-docker" -v
```

The raw `docker run` form remains available for custom container workflows,
but is not required for normal use.

## Baseline

### Installation

On Debian/Ubuntu, install the complete runtime—including FFmpeg, FFprobe,
Tesseract OCR, OpenCV, and the Python environment—with:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$PWD}"
cd "$tool_root"
bash scripts/install.sh
source scripts/activate.sh
```

After installation, use the repository-local launcher to avoid repeating the
Python module invocation:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$PWD}"
video_dir="${VIDEO_DIR:-$PWD}"
"$tool_root/priv-sanitizer" --help
cd "$video_dir"
"$tool_root/priv-sanitizer"
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

The CLI reports progress for frame sampling, optional audio analysis, and video
rendering. Audio analysis is skipped by default when audio is preserved, which
avoids an unnecessary full-length decode on long recordings. Add
`-v`/`--verbose` to write detailed diagnostics to
`verbose.log`; set `PRIV_SANITIZER_QUIET=1` to suppress progress messages.
The default verification still checks audio-stream presence and duration; full
audio-level verification is included only when audio analysis is enabled.

## Batch mode

From a folder containing videos and optional same-stem transcripts, run the
launcher with no arguments:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$PWD}"
video_dir="${VIDEO_DIR:-$PWD}"
cd "$video_dir"
"$tool_root/priv-sanitizer"
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

It detects a conservative participant-panel region and runs local layout/OCR/face detectors when available. Audio is preserved by default and audio
analysis is skipped unless `--experimental-audio`, a non-preserve audio mode,
or `PRIV_SANITIZER_AUDIO_ANALYSIS=1` is requested. When enabled, speech
activity is recorded as evidence only and is never used to mute an entire
meeting.

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
