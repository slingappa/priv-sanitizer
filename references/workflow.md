# Workflow

## Folder batch

For a Docker-only installation, run `bash scripts/install.sh --docker-only`
once. The regular `priv-sanitizer` launcher remains native-default; use the
explicit `priv-sanitizer-docker` wrapper (or set
`PRIV_SANITIZER_USE_DOCKER=1`) to use the resulting image.

From a directory containing recordings and optional same-stem transcripts:

```bash
tool_root="${PRIV_SANITIZER_ROOT:-$PWD}"
video_dir="${VIDEO_DIR:-$PWD}"
cd "$video_dir"
"$tool_root/priv-sanitizer"
```

After `bash scripts/install.sh`, the persistent `priv-sanitizer` launcher in
`~/.local/bin` can be called directly from any directory; no activation step is
required. Use the repository path above when `~/.local/bin` is not on `PATH`.
If you activate manually, use `source scripts/activate.sh`; executing the file
cannot modify the parent shell's environment.

With no command arguments, the launcher scans only the current directory and
is equivalent to:

```bash
priv-sanitizer batch -j 8 -b 30% --input . --output ./outputs_sanitized --artifacts ./artifacts
```

Every video gets a baseline pass. A same-stem `.docx`, `.srt`, or `.vtt`
transcript triggers the optional hardening pass. The final selected video is
`outputs_sanitized/<video-stem>.mp4`. Generated `artifacts/` contents are
removed by default; pass `-k`/`--keep-artifacts` to retain audit data for
debugging (`--keep-videos` is a compatibility alias). When a transcript is
paired, baseline and hardening analysis finish before one final
transcript-aware render; the baseline render is only a fallback if hardening
fails.

Batch progress is checkpointed in
`outputs_sanitized/.priv-sanitizer-batch.json`. If the process is killed or a
delivery is partial, rerunning the same batch resumes completed and unfinished
items instead of starting every video over. A changed input, transcript, or
render setting starts a new state rather than mixing incompatible outputs.

Batch mode reads sources directly by default. For network-mounted recordings,
add `--local-staging` (or set `PRIV_SANITIZER_LOCAL_STAGING=1`) to stage each
source video in local scratch, normally `/tmp`, before analysis/rendering; only
the final delivery is copied back. Copy-in, retained-artifact, and final-
delivery transfers report percentage and ETA unless `-q` is used. Set
`PRIV_SANITIZER_STAGE_DIR=/path/to/local/scratch` to choose the scratch volume.

For a SharePoint-mounted input, avoid FUSE video reads by adding a canonical
folder URL or `/:f:/r/...` sharing link with
`--sharepoint-dir 'https://tenant.sharepoint.com/teams/site/Shared%20Documents/Recordings'`.
The local `--input` directory supplies names; Graph downloads each source into
scratch and uploads completed files to the remote `outputs_sanitized/` child.
The local `--output` copy is retained as well. This requires a delegated
Microsoft Graph token configured through the organization's Secrets Manager.

The baseline uses the stable coarse layout/OCR/Haar path plus the established
scene-change triggers. The experimental physical-camera/whiteboard path is
opt-in only: add `-w`/`--whiteboard` to enable frame-difference face sampling,
multi-cascade temporal tracking, denser short-clip sampling, and localized
feathered masks. `--blur-radius`, `--blur-opacity`, `--face-padding`, and the
`PRIV_SANITIZER_DENSE_FACE_*` environment controls apply to that `-w` mode;
without it the stable renderer remains unchanged.

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
