# Pre-change baseline

This document records the renderer state before adding normal-mode blur-opacity
control.

## Source revision

- Branch: `main`
- Revision: `5872b7b` (`Improve dense face coverage and progress reporting`)
- Baseline tag: `v0.1.0-baseline`

## Current rendering behavior

- No-argument execution runs native batch mode in the current directory.
- Normal meeting mode uses the legacy regional blur graph with
  `boxblur=luma_radius=18` and an opaque overlay (`blur_opacity: 1.0`).
- Normal mode intentionally ignores `--blur-radius`, `--face-padding`, and
  the configured blur-opacity value; those controls are currently effective
  only with experimental `-w`/`--whiteboard` mode.
- Audio is preserved by default.
- Transcript-assisted hardening is optional and secondary to video analysis.
- `-k` retains audit artifacts; without it, generated artifacts are removed
  after successful delivery verification.

## Validation

The baseline passed:

```text
48 passed
python -m compileall -q priv_sanitizer tests
bash -n priv-sanitizer scripts/install.sh scripts/activate.sh priv-sanitizer-docker
git diff --check
```

The baseline tag is a rollback point for the upcoming normal-mode opacity
change.
