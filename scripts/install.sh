#!/usr/bin/env bash
set -Eeuo pipefail

# Install the complete local runtime for priv-sanitizer.
# The installer is intentionally explicit about system packages because
# pytesseract is only a Python wrapper; OCR also needs the tesseract binary.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PRIV_SANITIZER_VENV:-${ROOT_DIR}/.venv}"
DRY_RUN=0
NO_SYSTEM_PACKAGES=0
SKIP_VISION=0

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Options:
  --dry-run              Print actions without changing the system.
  --no-system-packages  Do not use apt or local system-tool extraction; require tools on PATH.
  --skip-vision         Install the CLI/Pillow stack without OCR/face extras.
  --venv PATH           Use PATH instead of .venv.
  -h, --help            Show this help.
EOF
}

say() { printf '[priv-sanitizer] %s\n' "$*"; }
die() { printf '[priv-sanitizer] ERROR: %s\n' "$*" >&2; exit 1; }

run() {
  if (( DRY_RUN )); then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

has_command() { command -v "$1" >/dev/null 2>&1; }

tool_available() {
  local name="$1" override="" candidate=""
  if [[ "$name" == "ffmpeg" ]]; then
    override="${PRIV_SANITIZER_FFMPEG:-}"
  elif [[ "$name" == "ffprobe" ]]; then
    override="${PRIV_SANITIZER_FFPROBE:-}"
  fi
  if [[ -n "$override" && -x "$override" ]]; then
    return 0
  fi
  if [[ "$name" == "ffmpeg" || "$name" == "ffprobe" ]] && [[ -n "${PRIV_SANITIZER_FFMPEG_DIR:-}" ]]; then
    candidate="${PRIV_SANITIZER_FFMPEG_DIR}/${name}"
    [[ -x "$candidate" ]] && return 0
  fi
  has_command "$name"
}

apt_runner() {
  if [[ "$(id -u)" == "0" ]]; then
    printf 'apt-get'
  elif has_command sudo && sudo -n true >/dev/null 2>&1; then
    printf 'sudo apt-get'
  else
    return 1
  fi
}

configure_local_tesseract() {
  local root="${ROOT_DIR}/.tools/tesseract"
  if [[ ! -x "$root/usr/bin/tesseract" ]]; then
    return 1
  fi
  export PATH="$root/usr/bin:${PATH}"
  export LD_LIBRARY_PATH="$root/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  export TESSDATA_PREFIX="$root/usr/share/tesseract-ocr/5/tessdata"
  return 0
}

install_local_tesseract() {
  configure_local_tesseract && return 0
  has_command apt-get || return 1
  has_command dpkg-deb || return 1
  if (( DRY_RUN )); then
    say "Would extract local Tesseract runtime under ${ROOT_DIR}/.tools/tesseract"
    return 0
  fi
  local temp_dir root
  temp_dir="$(mktemp -d /tmp/priv-sanitizer-tesseract.XXXXXX)"
  root="${ROOT_DIR}/.tools/tesseract"
  say "No passwordless sudo; downloading a project-local Tesseract runtime."
  (
    cd "$temp_dir"
    apt-get download tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd libtesseract5 liblept5
  )
  mkdir -p "$root"
  for package in "$temp_dir"/*.deb; do
    dpkg-deb -x "$package" "$root"
  done
  rm -rf "$temp_dir"
  configure_local_tesseract
}

install_system_packages() {
  (( NO_SYSTEM_PACKAGES )) && return 0
  local apt_cmd
  apt_cmd="$(apt_runner 2>/dev/null || true)"
  if [[ -z "$apt_cmd" ]]; then
    if ! tool_available ffmpeg || ! tool_available ffprobe; then
      die "apt/sudo unavailable and FFmpeg is missing; use --no-system-packages with explicit tool paths"
    fi
    if ! (( SKIP_VISION )) && ! has_command tesseract; then
      install_local_tesseract || die "apt/sudo unavailable and Tesseract could not be installed locally"
    fi
    say "No apt runner available; using existing system binaries."
    return 0
  fi

  local packages=(python3-venv python3-pip ffmpeg)
  if ! (( SKIP_VISION )); then
    packages+=(tesseract-ocr tesseract-ocr-eng)
  fi
  say "Installing system packages: ${packages[*]}"
  # shellcheck disable=SC2086
  run $apt_cmd update
  # shellcheck disable=SC2086
  run $apt_cmd install -y "${packages[@]}"
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-system-packages) NO_SYSTEM_PACKAGES=1; shift ;;
    --skip-vision) SKIP_VISION=1; shift ;;
    --venv)
      (($# >= 2)) || die "--venv requires a path"
      VENV_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

install_system_packages

say "Creating/updating Python environment: $VENV_DIR"
if (( DRY_RUN )); then
  printf '+ python3 -m venv %q\n' "$VENV_DIR"
else
  python3 -m venv "$VENV_DIR"
fi

if (( ! DRY_RUN )); then
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
  if (( SKIP_VISION )); then
    "$VENV_DIR/bin/python" -m pip install -e "$ROOT_DIR"
  else
    "$VENV_DIR/bin/python" -m pip install -e "${ROOT_DIR}[vision]"
    # OpenCV 5 removed the Haar CascadeClassifier API used by the local
    # detector. Keep the vision runtime on the supported OpenCV 4 line.
    "$VENV_DIR/bin/python" -m pip install --upgrade "opencv-python-headless==4.10.0.84"
  fi
fi

if (( DRY_RUN )); then
  say "Dry run complete. No files or packages were changed."
  exit 0
fi

[[ -x "$VENV_DIR/bin/priv-sanitizer" ]] || die "CLI was not installed into $VENV_DIR"
tool_available ffmpeg || die "ffmpeg is still unavailable after installation"
tool_available ffprobe || die "ffprobe is still unavailable after installation"
if ! (( SKIP_VISION )); then
  has_command tesseract || configure_local_tesseract || die "tesseract is still unavailable after installation"
  "$VENV_DIR/bin/python" -c 'import cv2, pytesseract; assert hasattr(cv2, "CascadeClassifier"), "OpenCV build lacks CascadeClassifier"; print("vision backends:", cv2.__version__, pytesseract.get_tesseract_version())'
fi

say "Installation complete. Run:"
say "  source $ROOT_DIR/scripts/activate.sh"
say "  priv-sanitizer --help"
