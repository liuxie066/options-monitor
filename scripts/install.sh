#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/liuxie066/options-monitor.git"
PREFIX="${HOME}/apps/options-monitor"
VERSION=""
PYTHON_BIN="${PYTHON:-python3}"
WITH_SERVER=0
WITH_DEV=0
FORCE=0
INSTALL_CLI=1
BIN_DIR="${HOME}/.local/bin"
FORCE_CLI_WRAPPER=0
OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"

usage() {
  cat <<'EOF'
Usage:
  install.sh --version v1.2.107 [--prefix "$HOME/apps/options-monitor"]

Installs one pinned options-monitor release into:
  <prefix>/releases/<version>
  <prefix>/current -> <prefix>/releases/<version>

The installer downloads code, installs Python dependencies, updates current,
and by default creates user-level CLI wrappers. It does not write runtime config,
write env secrets, start services, create timers, connect to OpenD, send Feishu
messages, or touch SQLite state.

Options:
  --version VERSION     Required. Release tag to install, for example v1.2.107.
  --prefix PATH        Install root. Default: $HOME/apps/options-monitor.
  --repo-url URL       Git repository URL.
  --python PATH        Python executable for venv creation. Default: python3.
  --install-cli        Create user-level om and om-agent wrappers. Default.
  --no-install-cli     Do not create user-level CLI wrappers.
  --bin-dir PATH       Wrapper directory. Default: $HOME/.local/bin.
  --force-cli-wrapper  Overwrite existing non-options-monitor wrapper files.
  --with-server        Also install requirements/server.txt.
  --with-dev           Also install requirements/dev.txt.
  --force              Recreate the target release directory if it exists.
  -h, --help           Show this help.
EOF
}

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

missing_git_message() {
  case "$OS_NAME" in
    Darwin)
      printf 'git is required. On macOS run: xcode-select --install, or install Homebrew git with: brew install git'
      ;;
    Linux)
      printf 'git is required. Install it with your package manager, for example: sudo apt-get install git'
      ;;
    *)
      printf 'git is required'
      ;;
  esac
}

missing_python_message() {
  case "$OS_NAME" in
    Darwin)
      printf 'python executable not found: %s. On macOS install Python with: brew install python' "$PYTHON_BIN"
      ;;
    Linux)
      printf 'python executable not found: %s. Install python3 and venv support, for example: sudo apt-get install python3 python3-venv' "$PYTHON_BIN"
      ;;
    *)
      printf 'python executable not found: %s' "$PYTHON_BIN"
      ;;
  esac
}

check_python_runtime() {
  "$PYTHON_BIN" - <<'PY'
import importlib.util
import sys

if sys.version_info < (3, 10):
    raise SystemExit("python >= 3.10 is required")
if importlib.util.find_spec("venv") is None:
    raise SystemExit("python venv module is required")
PY
}

quote() {
  printf '%q' "$1"
}

normalize_dir_path() {
  raw="$1"
  parent="$(dirname "$raw")"
  mkdir -p "$parent"
  printf '%s/%s' "$(cd "$parent" && pwd)" "$(basename "$raw")"
}

write_cli_wrapper() {
  name="$1"
  target="$2"
  wrapper_path="${BIN_DIR}/${name}"
  tmp_wrapper="${wrapper_path}.tmp.$$"

  if [ ! -x "$target" ]; then
    die "cannot install CLI wrapper; target is not executable: $target"
  fi

  mkdir -p "$BIN_DIR"
  check_cli_wrapper_path "$name"

  cat > "$tmp_wrapper" <<EOF
#!/usr/bin/env bash
# options-monitor managed wrapper
# target-prefix: $PREFIX
exec "$target" "\$@"
EOF
  chmod +x "$tmp_wrapper"
  mv "$tmp_wrapper" "$wrapper_path"
  printf '[install] cli wrapper: %s -> %s\n' "$wrapper_path" "$target"
}

check_cli_wrapper_path() {
  name="$1"
  wrapper_path="${BIN_DIR}/${name}"
  if [ -e "$wrapper_path" ] || [ -L "$wrapper_path" ]; then
    if [ -d "$wrapper_path" ]; then
      die "cannot install CLI wrapper; path is a directory: $wrapper_path"
    fi
    if [ "$FORCE_CLI_WRAPPER" -ne 1 ] && ! grep -F "options-monitor managed wrapper" "$wrapper_path" >/dev/null 2>&1; then
      die "refusing to overwrite existing non-options-monitor command: $wrapper_path (pass --force-cli-wrapper)"
    fi
  fi
}

preflight_cli_wrappers() {
  mkdir -p "$BIN_DIR"
  check_cli_wrapper_path "om"
  check_cli_wrapper_path "om-agent"
}

bin_dir_in_path() {
  case ":${PATH:-}:" in
    *":${BIN_DIR}:"*) return 0 ;;
    *) return 1 ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --version)
      [ "$#" -ge 2 ] || die "--version requires a value"
      VERSION="$2"
      shift 2
      ;;
    --prefix)
      [ "$#" -ge 2 ] || die "--prefix requires a value"
      PREFIX="$2"
      shift 2
      ;;
    --repo-url)
      [ "$#" -ge 2 ] || die "--repo-url requires a value"
      REPO_URL="$2"
      shift 2
      ;;
    --python)
      [ "$#" -ge 2 ] || die "--python requires a value"
      PYTHON_BIN="$2"
      shift 2
      ;;
    --install-cli)
      INSTALL_CLI=1
      shift
      ;;
    --no-install-cli)
      INSTALL_CLI=0
      shift
      ;;
    --bin-dir)
      [ "$#" -ge 2 ] || die "--bin-dir requires a value"
      BIN_DIR="$2"
      shift 2
      ;;
    --force-cli-wrapper)
      FORCE_CLI_WRAPPER=1
      shift
      ;;
    --with-server)
      WITH_SERVER=1
      shift
      ;;
    --with-dev)
      WITH_DEV=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[ -n "$VERSION" ] || die "--version is required; install a pinned release tag, for example --version v1.2.107"
case "$VERSION" in
  v*) TAG="$VERSION" ;;
  *) TAG="v${VERSION}" ;;
esac
case "$TAG" in
  *[!A-Za-z0-9._-]*|.*|*..*)
    die "unsupported version tag: $TAG"
    ;;
esac

command -v git >/dev/null 2>&1 || die "$(missing_git_message)"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$(missing_python_message)"
check_python_runtime || die "python runtime check failed. Linux may need python3-venv; macOS may need a Homebrew Python."

PREFIX_PARENT="$(dirname "$PREFIX")"
mkdir -p "$PREFIX_PARENT"
PREFIX="$(cd "$PREFIX_PARENT" && pwd)/$(basename "$PREFIX")"
RELEASES_DIR="${PREFIX}/releases"
TARGET_DIR="${RELEASES_DIR}/${TAG}"
CURRENT_LINK="${PREFIX}/current"
if [ "$INSTALL_CLI" -eq 1 ]; then
  BIN_DIR="$(normalize_dir_path "$BIN_DIR")"
  preflight_cli_wrappers
fi

mkdir -p "$RELEASES_DIR"
if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
  die "current path exists and is not a symlink: $CURRENT_LINK"
fi

if [ -e "$TARGET_DIR" ]; then
  if [ "$FORCE" -ne 1 ]; then
    die "target release already exists: $TARGET_DIR (pass --force to recreate it)"
  fi
  rm -rf "$TARGET_DIR"
fi

tmp_dir="${RELEASES_DIR}/.${TAG}.tmp.$$"
rm -rf "$tmp_dir"
trap 'rm -rf "$tmp_dir"' EXIT

printf '[install] cloning %s at %s\n' "$REPO_URL" "$TAG"
git clone --depth 1 --branch "$TAG" "$REPO_URL" "$tmp_dir"

printf '[install] creating virtualenv\n'
"$PYTHON_BIN" -m venv "$tmp_dir/.venv"
"$tmp_dir/.venv/bin/pip" install -U pip
"$tmp_dir/.venv/bin/pip" install -r "$tmp_dir/requirements.txt" -c "$tmp_dir/constraints.txt"

if [ "$WITH_SERVER" -eq 1 ]; then
  "$tmp_dir/.venv/bin/pip" install -r "$tmp_dir/requirements/server.txt" -c "$tmp_dir/constraints/server.txt"
fi
if [ "$WITH_DEV" -eq 1 ]; then
  "$tmp_dir/.venv/bin/pip" install -r "$tmp_dir/requirements/dev.txt" -c "$tmp_dir/constraints/dev.txt"
fi

mv "$tmp_dir" "$TARGET_DIR"
ln -sfn "$TARGET_DIR" "$CURRENT_LINK"
trap - EXIT

if [ "$INSTALL_CLI" -eq 1 ]; then
  write_cli_wrapper "om" "${CURRENT_LINK}/om"
  write_cli_wrapper "om-agent" "${CURRENT_LINK}/om-agent"
fi

printf '\n[install] installed options-monitor %s\n' "$TAG"
printf '[install] current -> %s\n\n' "$TARGET_DIR"
if [ "$INSTALL_CLI" -eq 1 ]; then
  printf '[install] CLI wrappers installed in %s\n\n' "$(quote "$BIN_DIR")"
fi
printf 'Next steps:\n'
if [ "$INSTALL_CLI" -eq 1 ]; then
  if bin_dir_in_path; then
    printf '  om setup check\n'
  else
    printf '  export PATH=%s:"$PATH"\n' "$(quote "$BIN_DIR")"
    printf '  om setup check\n'
  fi
else
  printf '  cd %s\n' "$(quote "$CURRENT_LINK")"
  printf '  ./om setup check\n'
fi
case "$OS_NAME" in
  Darwin)
    printf '\nmacOS service env-file, if you later render launchd services:\n'
    printf '  mkdir -p "$HOME/Library/Application Support/options-monitor"\n'
    printf '  cp -n configs/examples/options-monitor.env.example "$HOME/Library/Application Support/options-monitor/options-monitor.env"\n'
    if [ "$INSTALL_CLI" -eq 1 ]; then
      printf '  om settings doctor --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env"\n'
    else
      printf '  ./om settings doctor --env-file "$HOME/Library/Application Support/options-monitor/options-monitor.env"\n'
    fi
    ;;
  Linux)
    printf '\nLinux production env-file, if you later render systemd services:\n'
    printf '  sudo install -d -m 700 /etc/options-monitor\n'
    printf '  sudo test -f /etc/options-monitor/options-monitor.env || sudo install -m 600 configs/examples/options-monitor.env.example /etc/options-monitor/options-monitor.env\n'
    if [ "$INSTALL_CLI" -eq 1 ]; then
      printf '  om settings doctor --env-file /etc/options-monitor/options-monitor.env\n'
    else
      printf '  ./om settings doctor --env-file /etc/options-monitor/options-monitor.env\n'
    fi
    ;;
esac
if [ "$INSTALL_CLI" -eq 1 ] && ! bin_dir_in_path; then
  printf '\nWarning: %s is not in PATH for this shell. Add it to your shell profile to keep using om directly.\n' "$(quote "$BIN_DIR")"
fi
printf '\nCreate runtime config and env-file only after reviewing setup output.\n'
