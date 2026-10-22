# priv-sanitizer

Make a privacy-reviewed copy of a meeting recording. The tool blurs participant
video, avatars, and detected names while retaining shared content when the
layout makes that safe. Watch the delivered file before sharing it.

## Local quick start

Install the native runtime once:

```bash
git clone https://github.com/slingappa/priv-sanitizer.git
cd priv-sanitizer
bash scripts/install.sh
```

The installer adds `priv-sanitizer` to `~/.local/bin`. Start a new terminal if
needed, or use `./priv-sanitizer` from the checkout.

Run it from a folder containing recordings:

```bash
cd /path/to/recordings
priv-sanitizer
```

With no arguments, it batch-processes the current folder using the quick
default of 8 render threads and 30% blur strength. Delivery files go to
`outputs_sanitized/`:

```text
recordings/
├── meeting.mp4
├── meeting.docx                 # optional same-stem transcript
└── outputs_sanitized/
    └── meeting.mp4
```

For a privacy delivery, choose the full blur strength explicitly. Add `-k` to
keep audit material after a successful run:

```bash
priv-sanitizer -j8 -b 100% -k
```

By default, temporary audit files are removed after output verification. The
delivery copy, resumable batch state, and log remain in `outputs_sanitized/`.

## SharePoint batch (native only)

Use `--sharepoint-dir` when a local SharePoint mount is slow or unreliable.
This is a **native-runtime-only** workflow: it uses the host's Secrets Manager
helper to obtain a delegated Microsoft Graph token. Do not use
`priv-sanitizer-docker` or `PRIV_SANITIZER_USE_DOCKER=1` for this command.

Before running, configure a valid Microsoft Graph token in your organization's
Secrets Manager. The local `--input` directory must expose the source names
and extensions; it is used only to discover video names and pair optional
same-stem transcripts. Graph downloads the corresponding remote bytes into
local scratch, processes them there, then uploads completed MP4s to the remote
folder's `outputs_sanitized` child. A local copy is also retained in
`--output`.

Both of these SharePoint folder URL forms are supported:

```text
https://tenant.sharepoint.com/teams/site/Shared%20Documents/Recordings
https://tenant.sharepoint.com/:f:/r/teams/site/Shared%20Documents/Recordings?d=...&csf=1
```

Example:

```bash
priv-sanitizer batch \
  --input /path/to/mounted/recordings \
  --sharepoint-dir 'https://tenant.sharepoint.com/teams/site/Shared%20Documents/Recordings' \
  --output /path/to/local/outputs_sanitized \
  -j8 -b 100% -k
```

SharePoint mode always stages one item locally and does not fall back to reading
video bytes through the mount. Set `PRIV_SANITIZER_STAGE_DIR` if the default
scratch location lacks space.

## Common commands

Process a selected local folder:

```bash
priv-sanitizer batch \
  --input /path/to/recordings \
  --output /path/to/recordings/outputs_sanitized \
  --artifacts /path/to/recordings/artifacts \
  -j8 -b 100%
```

For other network filesystems, enable one-item-at-a-time local staging:

```bash
priv-sanitizer batch --input /path/to/recordings \
  --output ./outputs_sanitized --local-staging
```

Process one video into a review directory:

```bash
priv-sanitizer sanitize --video ./meeting.mp4 --output ./review/meeting
```

Supported video formats are MP4, MOV, AVI, MKV, WEBM, and M4V. Folder discovery
is non-recursive.

## Transcript and audio

A video works without a transcript. An optional DOCX, SRT, or VTT transcript is
secondary evidence: batch mode pairs an unambiguous same-stem transcript
automatically, but never changes or delivers it. A paired transcript is
analyzed before one transcript-aware render, so it normally does not cause a
second full encode.

Audio is copied unchanged by default. Experimental transcript-guided
spoken-name muting is opt-in:

```bash
priv-sanitizer batch --input . --experimental-audio -k
```

It makes short mute windows around matching transcript evidence. It does not
anonymize voices or guarantee removal of every spoken identifier.

For an explicit two-stage workflow:

```bash
priv-sanitizer inspect ./meeting.mp4 --job ./review/meeting/job
priv-sanitizer analyze ./review/meeting/job
priv-sanitizer harden ./review/meeting/job \
  --transcript ./meeting.docx --job ./review/meeting-hardened/job
priv-sanitizer render ./review/meeting-hardened/job \
  --output ./review/meeting-hardened
```

Use `--transcript-offset SECONDS` with `harden` for a clip cut from a longer
recording.

## Blur and whiteboard controls

`-b` / `--blur-strength` is the visual blur setting. It accepts `0`–`1` or
`0%`–`100%` and maps to an opaque blur radius up to 128 pixels.

- `100%` is the strongest setting and recommended for delivery.
- Lower values retain more detail and are suitable only for controlled review.
- `0%` disables visual blur for diagnostics.

For a physical presenter or whiteboard, enable the experimental localized-face
detector:

```bash
priv-sanitizer -w -j8 -b 100% -k --face-padding 20%
```

`--face-padding` expands a detected physical face equally in both directions.
It accepts `0`–`0.5` or `0%`–`50%` and defaults to `16%`. Review
whiteboard-mode output particularly carefully.

Older `--blur-radius` and `--blur-opacity` arguments remain compatibility
aliases; use `-b` for new commands.

## Docker (optional)

Native is the default. Docker is useful for ordinary local processing when an
isolated runtime is preferred:

```bash
cd /path/to/priv-sanitizer
bash scripts/install.sh --docker-only

cd /path/to/recordings
priv-sanitizer-docker -j8 -b 100%
```

Docker Engine/Desktop must be running. `bash scripts/install.sh --docker`
installs both runtimes. SharePoint mode remains native-only because the
container does not have the host Secrets Manager helper.

## Review, resume, and logs

Use `-k` to retain audit material under `artifacts/<video-stem>/`:

- `job/analysis.json` — detector evidence and warnings.
- `job/redaction-manifest.json` — applied regions and timestamps.
- `job/report.txt` — readable summary.
- `render.json` — settings and output verification.
- `artifacts/batch-report.json` — status for the batch.

The final delivery is always a flat file in the selected output directory
(normally `outputs_sanitized/`). A transcript-assisted render is stored under
`hardened/`; otherwise it is under `baseline/`.

`outputs_sanitized/.priv-sanitizer-batch.json` tracks completed items. A
matching rerun resumes items whose output is already valid and processes the
rest. Changed inputs, destinations, or processing settings start a new state.

`outputs_sanitized/verbose.log` records stages, command timing, and
heartbeats. The terminal shows a spinner, percentage, and approximate ETA.
Use `-v` for more diagnostics or `-q` to suppress status and the log.
FFmpeg can remain near 100% while it finalizes the MP4; inspect the log before
calling it stuck.

Output verification checks duration plus video/audio stream presence. It does
not prove that every identifier was found. Inspect full-screen speakers, grids,
layout transitions, and shared screens before distribution.

## CLI reference

```text
priv-sanitizer [options]                         batch current directory
priv-sanitizer batch --input DIR [--output DIR] [--artifacts DIR]
                    [--sharepoint-dir URL] [--local-staging]
priv-sanitizer sanitize --video FILE --output DIR
priv-sanitizer inspect FILE --job DIR
priv-sanitizer analyze JOB
priv-sanitizer harden JOB --transcript FILE --job DIR
priv-sanitizer render JOB --output DIR
priv-sanitizer report JOB
priv-sanitizer verify JOB --output DIR
```

Useful options: `-j N` (threads), `-b VALUE` (blur strength), `-k` (keep
artifacts), `-w` (experimental whiteboard mode), `-v` (verbose), `-q`
(quiet), `--experimental-audio`, and `--every SECONDS`.

Run `priv-sanitizer --help` or `priv-sanitizer <command> --help` for the
live parser reference.

## Setup problems

| Problem | Resolution |
| --- | --- |
| `ffprobe was not found` | Run `bash scripts/install.sh`, install FFmpeg, or set `PRIV_SANITIZER_FFMPEG_DIR`. |
| OCR or face backend unavailable | Re-run the installer without `--skip-vision`; retain artifacts and review output. |
| Graph authentication fails | Refresh the delegated Microsoft Graph token in the host Secrets Manager, then rerun the native command. |
| Docker image unavailable | Run `bash scripts/install.sh --docker-only`, or use the native launcher. |
| `priv-sanitizer: command not found` | Start a new terminal, add `~/.local/bin` to `PATH`, or run `./priv-sanitizer` from the checkout. |

Installer options include `--dry-run`, `--no-system-packages`,
`--skip-vision`, `--docker`, `--docker-only`, `--venv PATH`, and
`--bin-dir PATH`.
