#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/liuxie066/options-monitor.git"
PREFIX="${HOME}/apps/options-monitor"
VERSION=""
PYTHON_BIN="${PYTHON:-python3}"
WITH_SERVER=0
WITH_DEV=0
FORCE=0

usage() {
  cat <<'EOF'
Usage:
  install.sh --version v1.2.90 [--prefix "$HOME/apps/options-monitor"]

Installs one pinned options-monitor release into:
  <prefix>/releases/<version>
  <prefix>/current -> <prefix>/releases/<version>

The installer only downloads code and installs Python dependencies. It does not
write runtime config, write env secrets, start services, create timers, connect
to OpenD, send Feishu messages, or touch SQLite state.

Options:
  --version VERSION     Required. Release tag to install, for example v1.2.90.
  --prefix PATH        Install root. Default: $HOME/apps/options-monitor.
  --repo-url URL       Git repository URL.
  --python PATH        Python executable for venv creation. Default: python3.
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

quote() {
  printf '%q' "$1"
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

[ -n "$VERSION" ] || die "--version is required; install a pinned release tag, for example --version v1.2.90"
case "$VERSION" in
  v*) TAG="$VERSION" ;;
  *) TAG="v${VERSION}" ;;
esac
case "$TAG" in
  *[!A-Za-z0-9._-]*|.*|*..*)
    die "unsupported version tag: $TAG"
    ;;
esac

command -v git >/dev/null 2>&1 || die "git is required"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python executable not found: $PYTHON_BIN"

PREFIX_PARENT="$(dirname "$PREFIX")"
mkdir -p "$PREFIX_PARENT"
PREFIX="$(cd "$PREFIX_PARENT" && pwd)/$(basename "$PREFIX")"
RELEASES_DIR="${PREFIX}/releases"
TARGET_DIR="${RELEASES_DIR}/${TAG}"
CURRENT_LINK="${PREFIX}/current"

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

printf '\n[install] installed options-monitor %s\n' "$TAG"
printf '[install] current -> %s\n\n' "$TARGET_DIR"
printf 'Next steps:\n'
printf '  cd %s\n' "$(quote "$CURRENT_LINK")"
printf '  ./om setup check\n'
printf '\nCreate runtime config and env-file only after reviewing setup output.\n'
