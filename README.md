# priv-sanitizer

`priv-sanitizer` is a local-first tool for protecting participant privacy in
meeting recordings. It detects participant tiles, camera feeds, avatars, and
visible names, then blurs the video while preserving shared content and audio
by default.

> [!WARNING]
> This is an assistive redaction tool, not proof of complete
> de-identification. Review the rendered video and audit artifacts before
> distributing it.

## What it does

- Works with a video alone; a transcript is optional secondary evidence.
- Detects changing meeting layouts, including participant rails, grids, and
  full-screen speakers or avatars.
- Uses OCR and face detection when available, with conservative layout
  fallbacks when they are not.
- Blurs participant images and visible names instead of replacing them with a
  solid fill.
- Preserves audio by default. Experimental transcript-guided name muting is
  opt-in.
- Produces a simple delivery copy plus inspectable analysis and render
  artifacts.

## Quick start — Docker (recommended)

Run these commands from the folder that contains the recordings. Set
`VIDEO_DIR` first if the recordings are somewhere else.

```bash
export VIDEO_DIR="${VIDEO_DIR:-$PWD}"
export TOOL_DIR="${PRIV_SANITIZER_ROOT:-$HOME/tools/priv-sanitizer}"

git clone https://github.com/slingappa/priv-sanitizer.git "$TOOL_DIR"
cd "$TOOL_DIR"
bash scripts/install.sh --docker-only

cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer"
```

The no-argument launcher batches every supported video in the current folder.
It automatically uses the configured Docker image after `--docker-only`:

```text
recordings/
├── meeting.mp4
├── meeting.docx                 # optional matching transcript
├── outputs_sanitized/
│   └── meeting.mp4              # delivery copy
├── artifacts/
│   ├── meeting/                  # analysis and render audit data
│   └── batch-report.json
└── verbose.log                  # operational diagnostics
```

The Docker wrapper is also available directly:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer-docker"
```

It mounts the current folder as `/work`, preserves output ownership, and
avoids requiring users to write a `docker run` command.

## Quick start — native Python

Use the native runtime instead of Docker when you prefer a local virtual
environment:

```bash
export VIDEO_DIR="${VIDEO_DIR:-$PWD}"
export TOOL_DIR="${PRIV_SANITIZER_ROOT:-$HOME/tools/priv-sanitizer}"

git clone https://github.com/slingappa/priv-sanitizer.git "$TOOL_DIR"
cd "$TOOL_DIR"
bash scripts/install.sh
source scripts/activate.sh

cd "$VIDEO_DIR"
PRIV_SANITIZER_USE_DOCKER=0 "$TOOL_DIR/priv-sanitizer"
```

After either installation, inspect copy-paste examples with:

```bash
"$TOOL_DIR/priv-sanitizer" --help
"$TOOL_DIR/priv-sanitizer" batch --help
```

## Everyday use

### Batch a folder

From the recordings folder, the shortest command is:

```bash
"$TOOL_DIR/priv-sanitizer"
```

The equivalent explicit form is:

```bash
"$TOOL_DIR/priv-sanitizer" batch \
  --input . \
  --output ./outputs_sanitized \
  --artifacts ./artifacts
```

Discovery is non-recursive. Videos may be MP4, MOV, AVI, MKV, WEBM, or M4V;
transcripts may be DOCX, SRT, or VTT. A same-stem transcript is paired
automatically. Missing or ambiguous transcripts never block the video-only
pass.

### Sanitize one video

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" sanitize \
  --video meeting.mp4 \
  --output artifacts/meeting-baseline
```

This creates `artifacts/meeting-baseline/sanitized.mp4` and its job manifest.
The command works with either launcher when paths are relative to the current
recordings folder.

### Use a transcript for hardening

Transcripts are analysis input only; they are never rewritten or emitted:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" harden \
  artifacts/meeting-baseline/job \
  --transcript meeting.docx \
  --job artifacts/meeting-hardened/job

"$TOOL_DIR/priv-sanitizer" render \
  artifacts/meeting-hardened/job \
  --output artifacts/meeting-hardened
```

For an extracted clip, subtract the clip's source start time from transcript
timestamps with `--transcript-offset`. Batch mode performs this hardening
analysis automatically when an unambiguous matching transcript is present and
reuses the baseline encode when the redaction mask did not change.

### Tune CPU use

Rendering defaults to eight FFmpeg/filter/x264 threads:

```bash
"$TOOL_DIR/priv-sanitizer" -j 4
```

Use `-j N`/`--jobs N` for a one-off override. The equivalent environment
variables are `PRIV_SANITIZER_FFMPEG_THREADS` and
`PRIV_SANITIZER_X264_PRESET`. More threads are not always faster for long
blur/overlay graphs or when storage is busy.

## Audio and transcripts

| Mode | Transcript required | Audio behavior |
| --- | --- | --- |
| Video-only baseline | No | Preserved |
| Transcript-assisted hardening | Optional | Preserved |
| `--experimental-audio` | Yes for useful matches | Short, review-required mute windows around matched spoken names |

Experimental audio processing is disabled by default. It does not anonymize a
voice, mute every mention, or redact arbitrary spoken identifiers. Use it only
when its generated intervals have been reviewed:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" batch \
  --input . \
  --output ./outputs_sanitized \
  --artifacts ./artifacts \
  --experimental-audio
```

## How detection works

1. A coarse sampler always includes the first frame and periodic frames.
2. A lightweight FFmpeg scene-change pass adds frames around brief layout
   transitions.
3. OCR, face, and temporal meeting-layout evidence are combined into
   time-bounded blur regions.
4. FFmpeg renders regional blur while preserving shared content where the
   layout can be separated safely.
5. Verification checks output duration and video/audio stream presence.

If a layout cannot be separated confidently, the tool may use a conservative
full-frame participant fallback. Detector availability, warnings, evidence,
and review-required regions are recorded in `analysis.json` and
`redaction-manifest.json`.

## Review before sharing

Always inspect the output and these artifacts:

- `outputs_sanitized/<stem>.mp4` — the delivery copy.
- `artifacts/<stem>/baseline/job/analysis.json` — detector evidence and
  warnings.
- `artifacts/<stem>/baseline/job/redaction-manifest.json` — applied regions,
  modes, and timestamps.
- `artifacts/<stem>/baseline/report.txt` — readable job summary.
- `artifacts/batch-report.json` — per-file status and verification results.
- `verbose.log` — stage progress, command timing, and long-run heartbeats.

Duration and stream checks confirm a usable output; they do not prove that
every visual or spoken identifier was found.

## Installation options

`scripts/install.sh` supports Debian/Ubuntu and can be previewed safely:

```bash
bash scripts/install.sh --dry-run
```

Useful options:

- `--no-system-packages` — require FFmpeg, FFprobe, and Tesseract on `PATH`.
- `--skip-vision` — install without the OCR/face Python extras.
- `--docker` — install the native runtime and build Docker, then select Docker
  for the launcher.
- `--docker-only` — build Docker without installing host Python packages.
- `--docker-image NAME` — choose a Docker image tag.
- `--venv PATH` — choose the virtual-environment location.

The Docker image packages Python, FFmpeg/FFprobe, Tesseract English data,
OpenCV, Pillow, and the sanitizer runtime. Docker setup needs access to the
package registries during installation; normal processing is local.

If you already manage FFmpeg separately, set:

```bash
export PRIV_SANITIZER_FFMPEG_DIR="$PWD/.tools/ffmpeg/bin"
```

The activation script also exposes the project virtual environment and local
tool paths:

```bash
source "$TOOL_DIR/scripts/activate.sh"
```

## Logging and troubleshooting

- `verbose.log` is created by default in the recordings folder.
- `-v`/`--verbose` forces detailed diagnostics into that log.
- `-q`/`--quiet` suppresses status output and does not create the log for that
  run.
- `PRIV_SANITIZER_HEARTBEAT_SECONDS=5` makes long-render heartbeats more
  frequent.
- `PRIV_SANITIZER_USE_DOCKER=0` forces the native launcher; `=1` requires the
  configured Docker image.
- `PRIV_SANITIZER_SCENE_DETECTION=0` disables adaptive scene triggers for a
  diagnostic comparison. Threshold and clustering are controlled by
  `PRIV_SANITIZER_SCENE_THRESHOLD` and
  `PRIV_SANITIZER_SCENE_MERGE_SECONDS`.

Common setup errors:

| Message | Resolution |
| --- | --- |
| `ffprobe was not found` | Run `scripts/install.sh`, install FFmpeg, or set `PRIV_SANITIZER_FFMPEG_DIR`. |
| Docker image unavailable | Run `bash scripts/install.sh --docker-only` or set `PRIV_SANITIZER_USE_DOCKER=0`. |
| OCR/face backend unavailable | Install the vision extras and Tesseract, or review the conservative fallback warnings. |

## Supported formats and limitations

Supported videos are MP4, MOV, AVI, MKV, WEBM, and M4V. Optional transcripts
are DOCX, SRT, and VTT.

Detector coverage varies with recording quality, compression, UI theme, and
meeting layout. Shared-screen identifiers, names missed by OCR, and spoken
identifiers outside experimental transcript matches are not guaranteed to be
redacted. Review every output before sharing it.
