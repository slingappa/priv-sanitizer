# priv-sanitizer

A local tool for making privacy-reviewed copies of meeting recordings. It blurs
participant video, avatars, and detected names while retaining shared content
where the meeting layout makes that safe. Always watch the delivered file before
sharing it.

## Quick start

Install the native runtime once:

```bash
git clone https://github.com/slingappa/priv-sanitizer.git
cd priv-sanitizer
bash scripts/install.sh
```

The installer creates a `priv-sanitizer` launcher in `~/.local/bin`. Open a
new terminal if needed, or use `./priv-sanitizer` from the checkout.

From a folder containing recordings:

```bash
cd /path/to/recordings
priv-sanitizer
```

With no command or arguments, it batch-processes supported videos in the
current directory with `-j8 -b 30%` (8 FFmpeg/filter threads and a moderate
blur-strength quick default). Final files are placed here:

```text
recordings/
├── meeting.mp4
├── meeting.docx                 # optional same-stem transcript
└── outputs_sanitized/
    └── meeting.mp4
```

The default removes generated audit data after the final copy is verified. Keep
it for review or debugging:

```bash
priv-sanitizer -j8 -k
```

That retains `artifacts/`; the delivery copy in `outputs_sanitized/` is always
kept. `-j N` selects FFmpeg/filter threads (default: 8).

Each batch also keeps a small `.priv-sanitizer-batch.json` state file inside
`outputs_sanitized/`. If the process is interrupted, killed, or leaves a
missing/invalid delivery file, the next matching batch invocation resumes the
unfinished items and reuses completed deliveries. Changing the input folder,
output folder, transcript/settings, or sampling options starts a fresh batch
state instead of mixing jobs.

Batch mode reads sources directly by default. If the input or output is on a
network mount, opt in to one-video-at-a-time local scratch staging:

```bash
priv-sanitizer --local-staging --input /path/to/recordings \
  --output /path/to/outputs_sanitized
```

Analysis and rendering then reread the local copy instead of continuously
reading a network mount; only the source staging copy and final delivery cross
the network. Copy-in, retained-artifact, and final-delivery transfers show
percentage and ETA; use `-q` to suppress them. `PRIV_SANITIZER_LOCAL_STAGING=1`
is the equivalent environment setting. Set `PRIV_SANITIZER_STAGE_DIR` when
`/tmp` is not large enough.

For a SharePoint-mounted recordings folder, use Graph for the file bytes
instead of copying through the FUSE mount:

```bash
priv-sanitizer batch \
  --input /path/to/mounted/recordings \
  --sharepoint-dir 'https://tenant.sharepoint.com/teams/site/Shared%20Documents/Recordings' \
  --output /path/to/local/outputs_sanitized
```

`--sharepoint-dir` accepts either a canonical folder URL or a SharePoint
sharing-folder link (`/:f:/r/...?...`). It implies local staging. The input directory is used only to
discover video/transcript names; each source is downloaded directly through
Microsoft Graph, and completed deliveries are uploaded to the remote
`outputs_sanitized/` child folder. The local delivery copy is retained too.
Configure a delegated token for `https://graph.microsoft.com/v1.0` through
your organization's Secrets Manager before using this option. Use the native
launcher for this workflow; the optional Docker image does not include the
host's `secrets` helper.

## Common use

Process a selected folder:

```bash
priv-sanitizer batch \
  --input /path/to/recordings \
  --output /path/to/recordings/outputs_sanitized \
  --artifacts /path/to/recordings/artifacts
```

Process one video into a review directory:

```bash
priv-sanitizer sanitize \
  --video ./meeting.mp4 \
  --output ./review/meeting
```

Supported videos: MP4, MOV, AVI, MKV, WEBM, and M4V. Discovery is
non-recursive.

## Transcript and audio

A video works on its own. An optional same-stem DOCX, SRT, or VTT transcript is
secondary evidence: it helps hardening but is never modified or included in the
output. A batch run pairs it automatically when the match is unambiguous.
Transcript analysis completes before rendering, so a matched transcript normally
does not cause a second full encode.

Audio is copied unchanged by default. Transcript-guided spoken-name muting is
experimental and opt-in:

```bash
priv-sanitizer batch --input . --experimental-audio -k
```

It mutes short windows around matching transcript evidence. It does not
anonymize voices or guarantee removal of all spoken identifiers, so review the
result.

For an explicit hardening workflow:

```bash
priv-sanitizer inspect ./meeting.mp4 --job ./review/meeting/job
priv-sanitizer analyze ./review/meeting/job
priv-sanitizer harden ./review/meeting/job \
  --transcript ./meeting.docx \
  --job ./review/meeting-hardened/job
priv-sanitizer render ./review/meeting-hardened/job \
  --output ./review/meeting-hardened
```

Use `--transcript-offset SECONDS` with `harden` for a clip taken from a longer
recording.

## Visual controls

`-b` / `--blur-strength` is the single blur control. It accepts a fraction
or percentage from `0` to `1` / `0%` to `100%`:

```bash
# Privacy delivery: strongest opaque blur for an explicit command.
priv-sanitizer -j8 -b 100%

# Reduced blur for a controlled review copy.
priv-sanitizer -j8 -b 60%
```

Strength maps to an opaque FFmpeg blur radius up to 128 pixels. `100%` is
recommended for delivery; lower values retain more detail. `0%` disables
visual blur and is useful only for a diagnostic comparison.

For recordings of a physical presenter, camera, or whiteboard, opt into the
experimental localized-face detector:

```bash
priv-sanitizer -w -j8 -k -b 100% --face-padding 20%
```

`--face-padding` expands a detected face equally in both directions. It accepts
`0`–`0.5` or `0%`–`50%` and defaults to `16%`. Whiteboard mode needs
especially careful review.

Older `--blur-radius` and `--blur-opacity` options remain accepted for
existing scripts, but new commands should use `-b` only.

## Docker (optional)

The normal `priv-sanitizer` launcher uses native Python. To use an isolated
Docker runtime instead:

```bash
cd /path/to/priv-sanitizer
bash scripts/install.sh --docker-only

cd /path/to/recordings
priv-sanitizer-docker -j8
```

Docker Engine or Docker Desktop must be installed and running. To install both
runtimes, use `bash scripts/install.sh --docker`; the normal launcher remains
native unless you call `priv-sanitizer-docker` or set
`PRIV_SANITIZER_USE_DOCKER=1`.

## Review, artifacts, and logs

With `-k`, each input has audit material under `artifacts/<video-stem>/`.
The most useful files are:

- `job/analysis.json` — detector evidence and warnings.
- `job/redaction-manifest.json` — applied regions and timestamps.
- `job/report.txt` — readable summary.
- `render.json` — render settings and output verification.
- `artifacts/batch-report.json` — status of every batch item.
- `outputs_sanitized/.priv-sanitizer-batch.json` — resumable batch state.
- `outputs_sanitized/verbose.log` — status/heartbeat and diagnostic log.

A transcript-hardened render uses a `hardened/` directory; otherwise it uses
`baseline/`. In either case, deliver only the flat file in
`outputs_sanitized/`.

Verification checks duration and the presence of video/audio streams. It is not
proof of complete anonymization. Inspect full-screen participants, grid views,
layout transitions, and any shared screen that might contain identifying data.

Batch runs write `verbose.log` beside the deliveries in `outputs_sanitized/`.
Status lines show an animated spinner, percentage, and approximate ETA. Use
`-v` for extra diagnostics; use `-q` to suppress terminal status and the log.
Near 100%, FFmpeg may spend time flushing and finalizing the MP4; check
`outputs_sanitized/verbose.log` before treating that as a hang.

## Compact CLI reference

```text
priv-sanitizer [options]                         batch current directory
priv-sanitizer batch --input DIR [--output DIR] [--artifacts DIR]
                    [--sharepoint-dir URL]
priv-sanitizer sanitize --video FILE --output DIR
priv-sanitizer inspect FILE --job DIR
priv-sanitizer analyze JOB
priv-sanitizer harden JOB --transcript FILE --job DIR
priv-sanitizer render JOB --output DIR
priv-sanitizer report JOB
priv-sanitizer verify JOB --output DIR
```

Useful options: `-j N` (threads), `-b VALUE` (blur strength), `-k` (keep
artifacts), `-w` (experimental whiteboard mode), `-v` (verbose), and `-q`
(quiet). Batch also accepts `--experimental-audio`, `--every SECONDS`,
`--local-staging`, and `--sharepoint-dir URL`.

Run `priv-sanitizer --help` or `priv-sanitizer <command> --help` for the full
current parser help.

## Setup problems

| Problem | Resolution |
| --- | --- |
| `ffprobe was not found` | Run `bash scripts/install.sh`, install FFmpeg, or set `PRIV_SANITIZER_FFMPEG_DIR`. |
| OCR or face backend unavailable | Re-run the installer without `--skip-vision`; ensure Tesseract is available; retain artifacts and review output. |
| Docker image unavailable | Run `bash scripts/install.sh --docker-only`, or use the native launcher. |
| `priv-sanitizer: command not found` | Start a new terminal, add `~/.local/bin` to `PATH`, or run `./priv-sanitizer` from the checkout. |

Installer options include `--dry-run`, `--no-system-packages`,
`--skip-vision`, `--docker`, `--docker-only`, `--venv PATH`, and
`--bin-dir PATH`.
