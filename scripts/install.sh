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
SETUP_DOCKER=0
DOCKER_ONLY=0
DOCKER_IMAGE="${PRIV_SANITIZER_DOCKER_IMAGE:-priv-sanitizer:local}"
USER_BIN_DIR="${PRIV_SANITIZER_BIN_DIR:-${HOME:-}/.local/bin}"

usage() {
  cat <<'EOF'
Usage: scripts/install.sh [options]

Options:
  --dry-run              Print actions without changing the system.
  --no-system-packages  Do not use apt or local system-tool extraction; require tools on PATH.
  --skip-vision         Install the CLI/Pillow stack without OCR/face extras.
  --docker              Build the optional Docker runtime (native remains the default launcher).
  --docker-only         Build Docker runtime without installing host Python packages; use priv-sanitizer-docker.
  --docker-image NAME   Docker image tag (default: priv-sanitizer:local).
  --venv PATH           Use PATH instead of .venv.
  --bin-dir PATH        Install persistent launchers in PATH (default: ~/.local/bin).
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

install_user_launchers() {
  [[ -n "$USER_BIN_DIR" && "$USER_BIN_DIR" != "/" ]] || die "a safe launcher directory is required; set PRIV_SANITIZER_BIN_DIR"
  if (( DRY_RUN )); then
    say "Would install persistent launchers in $USER_BIN_DIR"
    return 0
  fi

  mkdir -p "$USER_BIN_DIR"
  local native_target docker_target native_quoted docker_quoted
  native_target="$ROOT_DIR/priv-sanitizer"
  docker_target="$ROOT_DIR/priv-sanitizer-docker"
  printf -v native_quoted '%q' "$native_target"
  printf -v docker_quoted '%q' "$docker_target"

  # These small shims make the command independent of the caller's current
  # directory and of whether a virtualenv was activated in the parent shell.
  # The repository launcher selects .venv, Tesseract, and Docker as needed.
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail'
    printf 'exec %s "$@"\n' "$native_quoted"
  } > "$USER_BIN_DIR/priv-sanitizer"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -Eeuo pipefail'
    printf 'exec %s "$@"\n' "$docker_quoted"
  } > "$USER_BIN_DIR/priv-sanitizer-docker"
  chmod 0755 "$USER_BIN_DIR/priv-sanitizer" "$USER_BIN_DIR/priv-sanitizer-docker"

  local profile_file="${PRIV_SANITIZER_PROFILE_FILE:-${HOME:-}/.profile.local}"
  if [[ -z "${HOME:-}" && -z "${PRIV_SANITIZER_PROFILE_FILE:-}" ]]; then
    say "HOME is unset; launchers were installed but PATH was not persisted"
  elif [[ "$profile_file" == "/" ]]; then
    die "a safe profile file is required; set PRIV_SANITIZER_PROFILE_FILE"
  elif ! grep -Fq '# >>> priv-sanitizer user launcher path >>>' "$profile_file" 2>/dev/null; then
    local profile_dir_quoted
    mkdir -p "$(dirname "$profile_file")"
    profile_dir_quoted=${USER_BIN_DIR//\'/\'\\\'\'}
    {
      printf '\n%s\n' '# >>> priv-sanitizer user launcher path >>>'
      printf "PRIV_SANITIZER_BIN_DIR='%s'\n" "$profile_dir_quoted"
      printf '%s\n' 'case ":${PATH:-}:" in'
      printf '%s\n' '  *":${PRIV_SANITIZER_BIN_DIR}:"*) ;;'
      printf '%s\n' '  *) PATH="${PRIV_SANITIZER_BIN_DIR}${PATH:+:$PATH}" ;;'
      printf '%s\n' 'esac'
      printf '%s\n' 'export PATH'
      printf '%s\n' '# <<< priv-sanitizer user launcher path <<<'
    } >> "$profile_file"
    say "Persisted $USER_BIN_DIR in $profile_file for future terminals"
  fi

  say "Installed persistent launchers in $USER_BIN_DIR"
  case ":${PATH:-}:" in
    *":$USER_BIN_DIR:"*) ;;
    *) say "Add $USER_BIN_DIR to PATH once if it is not already present." ;;
  esac
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --no-system-packages) NO_SYSTEM_PACKAGES=1; shift ;;
    --skip-vision) SKIP_VISION=1; shift ;;
    --docker) SETUP_DOCKER=1; shift ;;
    --docker-only) SETUP_DOCKER=1; DOCKER_ONLY=1; shift ;;
    --docker-image)
      (($# >= 2)) || die "--docker-image requires an image tag"
      DOCKER_IMAGE="$2"; shift 2 ;;
    --venv)
      (($# >= 2)) || die "--venv requires a path"
      VENV_DIR="$2"; shift 2 ;;
    --bin-dir)
      (($# >= 2)) || die "--bin-dir requires a path"
      USER_BIN_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

if (( SETUP_DOCKER && SKIP_VISION && !DOCKER_ONLY )); then
  die "--docker requires the vision runtime; remove --skip-vision"
fi

if (( DOCKER_ONLY )); then
  has_command docker || die "Docker CLI was not found; install Docker or omit --docker-only"
  if (( DRY_RUN )); then
    printf '+ docker build -t %q %q\n' "$DOCKER_IMAGE" "$ROOT_DIR"
    say "Docker-only dry run complete. No files or packages were changed."
    exit 0
  fi
  say "Building Docker image: $DOCKER_IMAGE"
  docker build -t "$DOCKER_IMAGE" "$ROOT_DIR"
  printf '%s\n' "$DOCKER_IMAGE" > "$ROOT_DIR/.docker-runtime"
  install_user_launchers
  say "Docker runtime built. Use priv-sanitizer-docker (or PRIV_SANITIZER_USE_DOCKER=1) to select it."
  exit 0
fi

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
  if (( SETUP_DOCKER )); then
    printf '+ docker build -t %q %q\n' "$DOCKER_IMAGE" "$ROOT_DIR"
  fi
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

if (( SETUP_DOCKER )); then
  has_command docker || die "Docker CLI was not found; install Docker or omit --docker"
  if (( DRY_RUN )); then
    printf '+ docker build -t %q %q\n' "$DOCKER_IMAGE" "$ROOT_DIR"
  else
    say "Building Docker image: $DOCKER_IMAGE"
    docker build -t "$DOCKER_IMAGE" "$ROOT_DIR"
    printf '%s\n' "$DOCKER_IMAGE" > "$ROOT_DIR/.docker-runtime"
    say "Docker runtime built. Native Python remains the default; use priv-sanitizer-docker to select Docker."
  fi
fi

install_user_launchers

say "Installation complete. Run:"
say "  priv-sanitizer --help   # works from any directory; no activation required"
say "  $ROOT_DIR/priv-sanitizer --help   # equivalent direct launcher"
