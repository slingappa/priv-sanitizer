# priv-sanitizer

`priv-sanitizer` is a local-first tool for protecting participant privacy in
meeting recordings. It detects participant tiles, camera feeds, avatars, and
visible names, then blurs the video while preserving shared content and audio
by default.

> [!WARNING]
> This is an assistive redaction tool, not proof of complete
> de-identification. Review the rendered video before distributing it; use
> `-k`/`--keep-artifacts` when audit artifacts are needed for review or
> debugging.

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
- Produces a simple delivery copy, with optional inspectable analysis and
  render artifacts when `-k`/`--keep-artifacts` is requested.

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
└── verbose.log                  # operational diagnostics
```

Batch mode removes generated `artifacts/` contents by default after the
delivery copy is verified. Add `-k`/`--keep-artifacts` when you need the
analysis manifests, reports, and rendered audit copies for debugging:

```text
artifacts/
├── meeting/
└── batch-report.json
```

The Docker wrapper is also available directly:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer-docker"
```

It mounts the current folder as `/work`, preserves output ownership, and
avoids requiring users to write a `docker run` command.

The flat delivery video is always retained. Batch mode removes all generated
artifacts by default. Add `-k`/`--keep-artifacts` when you explicitly need
those files for debugging; `--keep-videos` remains a compatibility alias:

```bash
"$TOOL_DIR/priv-sanitizer" --keep-artifacts
```

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

Use `-k`/`--keep-artifacts` with batch mode to retain generated audit artifacts
under `artifacts/<stem>/`; `--keep-videos` is a compatibility alias. Without
`-k`, only the selected flat delivery copy under `outputs_sanitized/` remains.
When a transcript is paired, baseline and hardened analysis both finish before
one transcript-aware encode; the baseline artifact contains analysis only
unless it is needed as a fallback.
Explicit `sanitize` and `render` commands retain the video in the output
directory you choose because that directory is an intentional direct output.

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
renders the final hardened manifest once. If hardening fails or its output
fails verification, batch renders the baseline manifest once as a fallback.

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

Always inspect the output. If you used `-k`, also inspect these audit artifacts:

- `outputs_sanitized/<stem>.mp4` — the delivery copy.
- `artifacts/<stem>/baseline/job/analysis.json` — detector evidence and
  warnings.
- `artifacts/<stem>/baseline/job/redaction-manifest.json` — applied regions,
  modes, and timestamps.
- `artifacts/<stem>/baseline/report.txt` — readable job summary.
- `artifacts/<stem>/baseline/render.json` or
  `artifacts/<stem>/hardened/render.json` — render settings, verification, and
  whether the artifact video was retained. With a successful transcript pass,
  the hardened file is the rendered one and the baseline directory remains
  analysis-only.
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
- Live progress shows a rotating Braille spinner beside each percentage so a
  slow conversion visibly remains active. In a terminal it ticks about four
  times per second even when the percentage is unchanged; non-interactive logs
  use the slower heartbeat cadence.
- The Docker launchers preserve an interactive terminal for this animation and
  keep redirected or piped runs line-oriented.
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
- `-k`/`--keep-artifacts` retains generated audit artifacts for debugging;
  `--keep-videos` is a compatibility alias. Omit it to remove artifacts after
  delivery verification and minimize storage use.

A typical long-running batch prints concise stage updates like this. In an
interactive terminal, the progress line is refreshed in place; the Braille
character changes even when the encoded percentage is temporarily unchanged,
and completed stages use the six-dot `⠿` glyph.

```text
$ priv-sanitizer -j16
[priv-sanitizer] batch 1/1: baseline example-meeting.mp4
[priv-sanitizer] probing input
[priv-sanitizer] sampling frames: ⠋ 100%
[priv-sanitizer] detecting scene changes: ⠙ 100%
[priv-sanitizer] sampling complete: 1444 frames in 79.4s
[priv-sanitizer] analysing layout, OCR, and faces
[priv-sanitizer] visual analysis complete: 6 regions in 29.5s
[priv-sanitizer] audio analysis skipped (audio preserved; enable --experimental-audio or PRIV_SANITIZER_AUDIO_ANALYSIS=1 to enable)
[priv-sanitizer] writing analysis manifest
[priv-sanitizer] preparing render
[priv-sanitizer] render settings: ffmpeg_threads=16, x264_preset=ultrafast
[priv-sanitizer] rendering video: ⠴  78%
[priv-sanitizer] rendering video: ⠦  79%
[priv-sanitizer] rendering video: ⠿ 100%
```

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
