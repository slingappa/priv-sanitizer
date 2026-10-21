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
- Uses dense, frame-difference-triggered face sampling by default for ordinary
  meetings, including fullscreen single/multi-face views and static UI/avatar
  screens when layout evidence is available.
  The newer localized physical-camera and whiteboard detector is experimental
  and is enabled only with `-w`.
- Blurs participant images and visible names instead of replacing them with a
  solid fill.
- Preserves audio by default. Experimental transcript-guided name muting is
  opt-in.
- Produces a simple delivery copy, with optional inspectable analysis and
  render artifacts when `-k`/`--keep-artifacts` is requested.

## Quick start — native runtime

The native Python runtime is the default. Run this once, beginning in the
folder that contains the recordings. Set `VIDEO_DIR` first if they live
elsewhere.

```bash
export VIDEO_DIR="${VIDEO_DIR:-$PWD}"
export TOOL_DIR="${PRIV_SANITIZER_ROOT:-$HOME/tools/priv-sanitizer}"

mkdir -p "$(dirname "$TOOL_DIR")"
git clone https://github.com/slingappa/priv-sanitizer.git "$TOOL_DIR"
cd "$TOOL_DIR"
bash scripts/install.sh

cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer"
```

With no arguments, the launcher batch-sanitizes supported videos directly in
the current folder. Its delivery contract is deliberately simple:

```text
recordings/
├── meeting.mp4
├── meeting.docx                 # optional matching transcript
├── outputs_sanitized/
│   └── meeting.mp4              # final delivery copy
└── verbose.log                  # operational diagnostics
```

Batch mode removes generated audit artifacts after it verifies the delivery
copy. Retain them for a privacy review or diagnosis:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" -k
```

With `-k`/`--keep-artifacts`, audit files are retained under
`artifacts/<video-stem>/` and `artifacts/batch-report.json`. The flat delivery
file in `outputs_sanitized/` is always retained. `--keep-videos` is a
compatibility alias for `--keep-artifacts`.

## Quick start — Docker (optional)

Docker is an alternative, isolated runtime; it does not replace the native
default. Docker Engine or Docker Desktop must already be installed and running.

```bash
export VIDEO_DIR="${VIDEO_DIR:-$PWD}"
export TOOL_DIR="${PRIV_SANITIZER_ROOT:-$HOME/tools/priv-sanitizer}"

mkdir -p "$(dirname "$TOOL_DIR")"
git clone https://github.com/slingappa/priv-sanitizer.git "$TOOL_DIR"
cd "$TOOL_DIR"
bash scripts/install.sh --docker-only

cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer-docker"
```

The Docker launcher mounts the current recordings folder, preserves output
ownership, and has the same no-argument batch contract. It avoids making users
write a raw `docker run` command. When both runtimes are available,
`priv-sanitizer` stays native by default; use `priv-sanitizer-docker` or set
`PRIV_SANITIZER_USE_DOCKER=1` to select Docker explicitly.

The installer creates persistent `priv-sanitizer` and
`priv-sanitizer-docker` launchers in `~/.local/bin`. Start a new login terminal
or source `~/.profile.local` once to use those short command names. The
repository-local launchers shown above work immediately without changing
`PATH`.

For all available commands and copy-paste examples:

```bash
"$TOOL_DIR/priv-sanitizer" --help
"$TOOL_DIR/priv-sanitizer" batch --help
```

## Common workflows

### Batch a folder

From the recordings folder, the shortest command is:

```bash
"$TOOL_DIR/priv-sanitizer"
```

The equivalent explicit form is useful in scripts or when choosing locations:

```bash
"$TOOL_DIR/priv-sanitizer" batch \
  --input . \
  --output ./outputs_sanitized \
  --artifacts ./artifacts
```

Discovery is non-recursive. Video inputs may be MP4, MOV, AVI, MKV, WEBM, or
M4V; transcripts may be DOCX, SRT, or VTT. A same-stem transcript is paired
automatically. With exactly one video and one transcript, that pair is also
unambiguous. Missing or ambiguous transcripts never block video-only
sanitization.

When a transcript is paired, baseline and hardening analysis finish before one
transcript-aware render. If that render fails verification, batch mode falls
back to a baseline render. Direct `sanitize` and `render` commands retain the
output directory you explicitly choose.

### Sanitize one video

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" sanitize \
  --video meeting.mp4 \
  --output ./review/meeting
```

This creates `review/meeting/sanitized.mp4` and the job manifest in
`review/meeting/job/`.

### Use a transcript as secondary evidence

Transcripts are analysis input only; they are never rewritten or emitted. Batch
mode uses an unambiguous paired transcript automatically. To run the two passes
explicitly:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" harden \
  ./review/meeting/job \
  --transcript meeting.docx \
  --job ./review/meeting-hardened/job

"$TOOL_DIR/priv-sanitizer" render \
  ./review/meeting-hardened/job \
  --output ./review/meeting-hardened
```

For an extracted clip, pass `--transcript-offset` equal to the clip start time
in its source recording. This aligns source-relative transcript timestamps with
the clip.

### Experimental physical-camera and whiteboard support

The established meeting-layout detector is the normal path. Physical-camera
and whiteboard support is experimental and is enabled only with `-w` or
`--whiteboard`:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" -w -j 8 -k --blur-radius 96 --face-padding 20%
```

This opt-in adds localized face detection and masking for recordings where
participants appear in a physical camera view or next to a whiteboard. It is
independent of audio redaction and needs careful manual review.

`--blur-radius` accepts `1`–`128` (default `64` in `-w` mode); higher values
are stronger and slower. `--blur-opacity` accepts `0`–`1` or `0%`–`100%`
(default fully opaque), and `--face-padding` accepts `0`–`0.5` or
`0%`–`50%` (default `16%`). Lower blur opacity intentionally reveals more
source detail and is unsuitable for privacy delivery.

## Audio and transcript behavior

| Mode | Transcript required | Audio behavior |
| --- | --- | --- |
| Video-only baseline | No | Preserved |
| Transcript-assisted hardening | Optional | Preserved |
| `--experimental-audio` | Yes for useful matches | Short, review-required mute windows around matched spoken names |

Experimental audio processing is disabled by default. It does not anonymize a
voice, mute every mention, or redact arbitrary spoken identifiers. Enable it
only when its generated intervals will be reviewed:

```bash
cd "$VIDEO_DIR"
"$TOOL_DIR/priv-sanitizer" batch \
  --input . \
  --output ./outputs_sanitized \
  --artifacts ./artifacts \
  --experimental-audio \
  -k
```

## How detection works

1. A coarse sampler always includes the first frame and periodic frames.
2. A lightweight FFmpeg scene-change pass adds frames around brief layout
   transitions.
3. Available OCR, face, and temporal meeting-layout evidence is combined into
   time-bounded blur regions.
4. Dense normal-mode face observations are temporally tracked and grouped so
   missed transition frames, fullscreen multi-face scenes, and static avatars
   remain covered. With `-w`, the same evidence feeds localized feathered
   physical-face masks for physical-camera/whiteboard scenes.
5. FFmpeg renders regional blur while preserving shared content where the
   layout can be separated safely.
6. Verification checks output duration and video/audio stream presence.

If a layout cannot be separated confidently, the tool records unresolved
coverage for manual review. Fullscreen participant-only scenes may be blurred
as a complete view; participant rails remain localized so shared content is
preserved. Only `-w` enables the experimental localized whiteboard/camera
path. Detector availability, warnings, evidence, effective mode, and
review-required regions are recorded in `analysis.json` and
`redaction-manifest.json`.

## Review before sharing

Always inspect the output. If you used `-k`, also inspect these audit artifacts:

- `outputs_sanitized/<stem>.mp4` — the delivery copy.
- `artifacts/<stem>/baseline/job/analysis.json` — detector evidence and
  warnings.
- `artifacts/<stem>/baseline/job/redaction-manifest.json` — applied regions,
  modes, and timestamps.
- `artifacts/<stem>/baseline/job/report.txt` — readable job summary.
- `artifacts/<stem>/baseline/render.json` or
  `artifacts/<stem>/hardened/render.json` — render settings, verification, and
  whether the artifact video was retained. With a successful transcript pass,
  the hardened file is the rendered one and the baseline directory remains
  analysis-only.
- `artifacts/batch-report.json` — per-file status and verification results.
- `verbose.log` — stage progress, command timing, and long-run heartbeats.

Duration and stream checks confirm a usable output; they do not prove that
every visual or spoken identifier was found.

### Brief flashes and scene cuts

The sanitizer masks source frames; it does not remove edits, camera cuts, or
single-frame flashes that already exist in the recording. A bright frame can
therefore remain visibly bright while its participant faces and names are
blurred. Compare the source and delivery at the reported timestamp and check
the corresponding `visual_redactions` interval in `redaction-manifest.json`.
Removing the flash itself is a separate editorial operation, such as
replacing or freezing the frame, and is outside privacy masking.

## Installation options

`scripts/install.sh` supports Debian/Ubuntu and can be previewed safely:

```bash
bash scripts/install.sh --dry-run
```

Useful options:

- `--no-system-packages` — require FFmpeg, FFprobe, and Tesseract on `PATH`.
- `--skip-vision` — install without the OCR/face Python extras.
- `--docker` — install the native runtime and build the optional Docker image;
  native Python remains the default launcher.
- `--docker-only` — build Docker without installing host Python packages; use
  `priv-sanitizer-docker` (or `PRIV_SANITIZER_USE_DOCKER=1`) to run it.
- `--docker-image NAME` — choose a Docker image tag.
- `--venv PATH` — choose the virtual-environment location.
- `--bin-dir PATH` — choose where persistent user launchers are installed
  (default: `~/.local/bin`).

The Docker image packages Python, FFmpeg/FFprobe, Tesseract English data,
OpenCV, Pillow, and the sanitizer runtime. Docker setup needs access to the
package registries during installation; normal processing is local.

If you already manage FFmpeg separately, set:

```bash
export PRIV_SANITIZER_FFMPEG_DIR="$PWD/.tools/ffmpeg/bin"
```

The activation script is optional. It exposes the project virtual environment
and local tool paths for debugging or direct Python use:

```bash
source "$TOOL_DIR/scripts/activate.sh"
```

Use `source` (or `. `) rather than executing `./scripts/activate.sh`; an
executed script runs in a child shell and cannot change the calling terminal's
`PATH`. After a Docker-only install, use `priv-sanitizer-docker` for the
container. Rebuild the image when the checkout gains new CLI options; the
regular launcher continues to use native Python by default.

## Logging and troubleshooting

- `verbose.log` is created by default in the recordings folder.
- Live progress shows a rotating Braille spinner beside each percentage so a
  slow conversion visibly remains active. In a terminal it ticks about four
  times per second even when the percentage is unchanged; non-interactive logs
  use the slower heartbeat cadence. The optional ETA is a ballpark estimate and
  is refreshed when the reported percentage advances.
- The Docker launchers preserve an interactive terminal for this animation and
  keep redirected or piped runs line-oriented.
- `-v`/`--verbose` forces detailed diagnostics into that log.
- `-q`/`--quiet` suppresses status output and does not create the log for that
  run.
- `PRIV_SANITIZER_HEARTBEAT_SECONDS=5` makes long-render heartbeats more
  frequent.
- The regular launcher uses native Python by default. Set
  `PRIV_SANITIZER_USE_DOCKER=1` to require the configured Docker image, or call
  `priv-sanitizer-docker` directly.
- `PRIV_SANITIZER_SCENE_DETECTION=0` disables adaptive scene triggers for a
  diagnostic comparison. Threshold and clustering are controlled by
  `PRIV_SANITIZER_SCENE_THRESHOLD` and
  `PRIV_SANITIZER_SCENE_MERGE_SECONDS`.
- Normal mode enables a low-resolution face scan at 2 fps by default, with
  frame-difference triggers, multi-cascade tracking, and bounded temporal
  safety gaps. `PRIV_SANITIZER_DENSE_FACE_FPS`,
  `PRIV_SANITIZER_FACE_DIFF_THRESHOLD`, and
  `PRIV_SANITIZER_FACE_FALLBACK_SECONDS` tune that scan. `-w` additionally
  enables localized feathered physical-face masks and uses a 4-fps default.
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
[priv-sanitizer] sampling frames: ⠋ 100.00%
[priv-sanitizer] detecting scene changes: ⠙ 100.00%
[priv-sanitizer] sampling complete: 1444 frames in 79.4s
[priv-sanitizer] analysing layout, OCR, and faces: ⠹   0.00%
[priv-sanitizer] analysing layout, OCR, and faces: ⠸  20.00%
[priv-sanitizer] analysing layout, OCR, and faces: ⠼  60.00% ETA 18s
[priv-sanitizer] analysing layout, OCR, and faces: ⠴  90.00% ETA 5s
[priv-sanitizer] analysing layout, OCR, and faces: ⠿ 100.00%
[priv-sanitizer] visual analysis complete: 6 regions in 29.5s
[priv-sanitizer] audio analysis skipped (audio preserved; enable --experimental-audio or PRIV_SANITIZER_AUDIO_ANALYSIS=1 to enable)
[priv-sanitizer] writing analysis manifest
[priv-sanitizer] preparing render
[priv-sanitizer] render settings: ffmpeg_threads=16, x264_preset=ultrafast
[priv-sanitizer] rendering video: ⠴  78.00% ETA 2m 10s
[priv-sanitizer] rendering video: ⠦  79.00% ETA 2m 05s
[priv-sanitizer] rendering video: ⠿ 100.00%
```

Common setup errors:

| Message | Resolution |
| --- | --- |
| `ffprobe was not found` | Run `scripts/install.sh`, install FFmpeg, or set `PRIV_SANITIZER_FFMPEG_DIR`. |
| Docker image unavailable | Run `bash scripts/install.sh --docker-only` before using Docker, or use the native-default `priv-sanitizer` launcher. |
| OCR/face backend unavailable | Install the vision extras and Tesseract, or review the localized-detection warnings before sharing. |

## Supported formats and limitations

Supported videos are MP4, MOV, AVI, MKV, WEBM, and M4V. Optional transcripts
are DOCX, SRT, and VTT.

Detector coverage varies with recording quality, compression, UI theme, and
meeting layout. Shared-screen identifiers, names missed by OCR, and spoken
identifiers outside experimental transcript matches are not guaranteed to be
redacted. Review every output before sharing it.
