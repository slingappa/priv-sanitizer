#!/usr/bin/env bash

# Source this file after install.sh to select the project virtualenv and any
# project-local FFmpeg runtime.

PRIV_SANITIZER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRIV_SANITIZER_VENV="${PRIV_SANITIZER_VENV:-${PRIV_SANITIZER_ROOT}/.venv}"

if [[ -f "${PRIV_SANITIZER_VENV}/bin/activate" ]]; then
  # shellcheck disable=SC1090
  source "${PRIV_SANITIZER_VENV}/bin/activate"
fi

# Keep the checked-out implementation ahead of any older copy that may still
# be installed in site-packages.  This matters when the console entry point is
# reused after a source update, and makes activation deterministic from any
# recordings directory.
export PYTHONPATH="${PRIV_SANITIZER_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# Bash caches command locations.  Clear that cache after changing PATH so a
# previously resolved global launcher cannot shadow this environment.
hash -r 2>/dev/null || true

if [[ -z "${PRIV_SANITIZER_FFMPEG_DIR:-}" && -x "${PRIV_SANITIZER_ROOT}/.tools/ffmpeg/bin/ffmpeg" ]]; then
  export PRIV_SANITIZER_FFMPEG_DIR="${PRIV_SANITIZER_ROOT}/.tools/ffmpeg/bin"
fi

if [[ -n "${PRIV_SANITIZER_FFMPEG_DIR:-}" ]]; then
  export PATH="${PRIV_SANITIZER_FFMPEG_DIR}:${PATH}"
fi

PRIV_SANITIZER_TESS_ROOT="${PRIV_SANITIZER_TESS_ROOT:-${PRIV_SANITIZER_ROOT}/.tools/tesseract}"
if [[ -x "${PRIV_SANITIZER_TESS_ROOT}/usr/bin/tesseract" ]]; then
  export PATH="${PRIV_SANITIZER_TESS_ROOT}/usr/bin:${PATH}"
  export LD_LIBRARY_PATH="${PRIV_SANITIZER_TESS_ROOT}/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  export TESSDATA_PREFIX="${PRIV_SANITIZER_TESS_ROOT}/usr/share/tesseract-ocr/5/tessdata"
fi

export PRIV_SANITIZER_ROOT
export PRIV_SANITIZER_VENV
export PRIV_SANITIZER_TESS_ROOT

echo "priv-sanitizer environment active: ${PRIV_SANITIZER_ROOT}"
command -v priv-sanitizer || true
command -v ffmpeg || true
command -v ffprobe || true
command -v tesseract || true

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  printf '%s\n' "[priv-sanitizer] NOTE: this script ran in a child shell; use 'source ${BASH_SOURCE[0]}' to keep activation in the current shell." >&2
fi
