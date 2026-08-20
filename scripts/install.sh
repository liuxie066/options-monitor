#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/liuxie066/options-monitor.git"
PREFIX="${HOME}/apps/options-monitor"
VERSION=""
PYTHON_BIN="${PYTHON:-}"
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
  install.sh [--version latest] [--prefix "$HOME/apps/options-monitor"]

Installs the latest GitHub release, or one pinned options-monitor release, into:
  <prefix>/releases/<version>
  <prefix>/current -> <prefix>/releases/<version>

The installer requires Node >= 22.19.0 and npm. It downloads code, installs
locked Python and Pi dependencies, verifies both runtimes, updates current, and
by default creates user-level CLI wrappers. It does not write runtime config,
write env secrets, start services, create timers, connect to OpenD, send Feishu
messages, or touch SQLite state.

Options:
  --version VERSION     Release tag to install, for example v1.2.118.
                        Default: latest published GitHub release, never main.
  --prefix PATH        Install root. Default: $HOME/apps/options-monitor.
  --repo-url URL       Git repository URL.
  --python PATH        Python executable for venv creation. Default: python3.12, then compatible python3.
  --install-cli        Create user-level om and om-agent wrappers. Default.
  --no-install-cli     Do not create user-level CLI wrappers.
  --bin-dir PATH       Wrapper directory. Default: $HOME/.local/bin.
  --force-cli-wrapper  Overwrite existing non-options-monitor wrapper files.
  --with-server        Also install requirements/server.txt.
  --with-dev           Also install requirements/dev.txt.
  --force              Recreate an inactive target release directory if it exists.
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
      printf 'python executable not found: %s. On macOS install Python 3.12 with: brew install python@3.12' "$PYTHON_BIN"
      ;;
    Linux)
      printf 'python executable not found: %s. Install Python 3.12 and venv support, for example: sudo apt-get install python3.12 python3.12-venv' "$PYTHON_BIN"
      ;;
    *)
      printf 'python executable not found: %s' "$PYTHON_BIN"
      ;;
  esac
}

missing_curl_message() {
  case "$OS_NAME" in
    Darwin)
      printf 'curl is required to resolve the latest release. On macOS install it with system tools or Homebrew, or pass --version vX.Y.Z.'
      ;;
    Linux)
      printf 'curl is required to resolve the latest release. Install it with your package manager, or pass --version vX.Y.Z.'
      ;;
    *)
      printf 'curl is required to resolve the latest release, or pass --version vX.Y.Z.'
      ;;
  esac
}

check_python_runtime() {
  runtime_version=""
  if runtime_version="$("$PYTHON_BIN" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3])); raise SystemExit(0 if sys.version_info >= (3, 12) else 42)' 2>&1)"; then
    :
  else
    status=$?
    if [ "$status" -eq 42 ]; then
      die "Python >= 3.12 is required; executable=$PYTHON_BIN; observed=${runtime_version:-unknown}. Install Python 3.12 or pass --python PATH."
    fi
    die "Python >= 3.12 runtime check failed; executable=$PYTHON_BIN; observed=${runtime_version:-unusable}. Install Python 3.12 or pass --python PATH."
  fi

  "$PYTHON_BIN" -c 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("venv") is not None else 43)' \
    || die "Python venv module is required; executable=$PYTHON_BIN; observed=$runtime_version. Install the Python 3.12 venv package."
}

check_node_runtime() {
  NODE_BIN="$(command -v node 2>/dev/null || true)"
  [ -n "$NODE_BIN" ] || die "Node >= 22.19.0 is required; node was not found on PATH. Install Node 22.19.0 or newer."
  NPM_BIN="$(command -v npm 2>/dev/null || true)"
  [ -n "$NPM_BIN" ] || die "npm is required. Install npm for Node 22.19.0 or newer."
  node_version="$("$NODE_BIN" --version 2>/dev/null || true)"
  if [[ ! "$node_version" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    die "Node >= 22.19.0 is required; observed=${node_version:-unusable}. Install Node 22.19.0 or newer."
  fi
  node_major="${BASH_REMATCH[1]}"
  node_minor="${BASH_REMATCH[2]}"
  if (( node_major < 22 || (node_major == 22 && node_minor < 19) )); then
    die "Node >= 22.19.0 is required; observed=$node_version. Install Node 22.19.0 or newer."
  fi
}

run_pi_smoke() {
  release_dir="$1"
  smoke_script="$release_dir/scripts/pi_runtime_smoke.sh"
  [ -x "$smoke_script" ] || die "Pi runtime smoke script is missing or not executable: $smoke_script"
  "$smoke_script" --root "$release_dir" --python "$release_dir/.venv/bin/python"
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

github_repo_slug_from_url() {
  raw="$1"
  case "$raw" in
    https://github.com/*)
      slug="${raw#https://github.com/}"
      ;;
    http://github.com/*)
      slug="${raw#http://github.com/}"
      ;;
    git@github.com:*)
      slug="${raw#git@github.com:}"
      ;;
    ssh://git@github.com/*)
      slug="${raw#ssh://git@github.com/}"
      ;;
    *)
      return 1
      ;;
  esac
  slug="${slug%%\?*}"
  slug="${slug%%#*}"
  slug="${slug%/}"
  owner="${slug%%/*}"
  rest="${slug#*/}"
  repo="${rest%%/*}"
  repo="${repo%.git}"
  if [ "$owner" = "$slug" ] || [ -z "$owner" ] || [ -z "$repo" ]; then
    return 1
  fi
  case "${owner}/${repo}" in
    *[!A-Za-z0-9._/-]*|*//*|/*|*/)
      return 1
      ;;
  esac
  printf '%s/%s\n' "$owner" "$repo"
}

latest_release_api_url() {
  slug="$(github_repo_slug_from_url "$REPO_URL")" || return 1
  printf 'https://api.github.com/repos/%s/releases/latest\n' "$slug"
}

normalize_tag() {
  raw="$1"
  case "$raw" in
    v*) printf '%s\n' "$raw" ;;
    *) printf 'v%s\n' "$raw" ;;
  esac
}

validate_tag() {
  tag="$1"
  case "$tag" in
    *[!A-Za-z0-9._-]*|.*|*..*)
      die "unsupported version tag: $tag"
      ;;
  esac
}

resolve_latest_release_tag() {
  api_url="$(latest_release_api_url)" || die "cannot resolve latest GitHub release from repo URL: $REPO_URL; pass --version vX.Y.Z"
  command -v curl >/dev/null 2>&1 || die "$(missing_curl_message)"
  latest_json="$(curl -fsSL "$api_url")" || die "failed to resolve latest GitHub release from $api_url; pass --version vX.Y.Z"
  latest_tag="$(printf '%s\n' "$latest_json" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  [ -n "$latest_tag" ] || die "latest GitHub release response did not include tag_name; pass --version vX.Y.Z"
  normalize_tag "$latest_tag"
}

install_optional_requirements() {
  release_dir="$1"
  if [ "$WITH_SERVER" -ne 1 ] && [ "$WITH_DEV" -ne 1 ]; then
    return 0
  fi
  pip_bin="${release_dir}/.venv/bin/pip"
  [ -x "$pip_bin" ] || die "cannot install optional requirements; pip is missing: $pip_bin"
  if [ "$WITH_SERVER" -eq 1 ]; then
    "$pip_bin" install -r "$release_dir/requirements/server.txt" -c "$release_dir/constraints/server.txt"
  fi
  if [ "$WITH_DEV" -eq 1 ]; then
    "$pip_bin" install -r "$release_dir/requirements/dev.txt" -c "$release_dir/constraints/dev.txt"
  fi
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

stage_cli_wrapper() {
  name="$1"
  target="$2"
  validation_target="$3"
  staged_path="$4"

  [ -x "$validation_target" ] \
    || die "cannot install CLI wrapper; target is not executable: $validation_target"
  check_cli_wrapper_path "$name"
  cat > "$staged_path" <<EOF
#!/usr/bin/env bash
# options-monitor managed wrapper
# target-prefix: $PREFIX
exec "$target" "\$@"
EOF
  chmod +x "$staged_path"
}

backup_cli_wrapper() {
  wrapper_path="$1"
  backup_path="$2"
  if [ -e "$wrapper_path" ] || [ -L "$wrapper_path" ]; then
    cp -pP "$wrapper_path" "$backup_path" \
      || die "failed to back up CLI wrapper: $wrapper_path"
    return 0
  fi
  return 1
}

restore_cli_wrapper() {
  wrapper_path="$1"
  backup_path="$2"
  existed="$3"
  if [ "$existed" -eq 1 ]; then
    mv -f "$backup_path" "$wrapper_path"
  else
    rm -f "$wrapper_path"
  fi
}

replace_current_link() {
  staged_link="${CURRENT_LINK}.tmp.$$"
  rm -f "$staged_link"
  ln -s "$TARGET_DIR" "$staged_link"
  "$PYTHON_BIN" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$staged_link" "$CURRENT_LINK"
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

print_next_steps() {
  printf '\n[install] installed options-monitor %s\n' "$TAG"
  printf '[install] current -> %s\n\n' "$TARGET_DIR"
  printf '[install] Pi runtime verified with Node %s\n\n' "$node_version"
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

if [ -z "$VERSION" ] || [ "$VERSION" = "latest" ]; then
  TAG="$(resolve_latest_release_tag)"
  printf '[install] resolved latest release: %s\n' "$TAG"
else
  TAG="$(normalize_tag "$VERSION")"
fi
validate_tag "$TAG"

if [ -z "$PYTHON_BIN" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
  else
    PYTHON_BIN="python3"
  fi
fi

command -v git >/dev/null 2>&1 || die "$(missing_git_message)"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$(missing_python_message)"
check_python_runtime

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
if { [ -e "$CURRENT_LINK" ] || [ -L "$CURRENT_LINK" ]; } && [ ! -L "$CURRENT_LINK" ]; then
  die "current path exists and is not a symlink: $CURRENT_LINK"
fi

ALREADY_INSTALLED=0
REPLACE_INACTIVE_TARGET=0
TARGET_EXISTS=0
if [ -e "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; then
  TARGET_EXISTS=1
fi
if [ "$TARGET_EXISTS" -eq 1 ]; then
  TARGET_IS_ACTIVE=0
  if [ -L "$CURRENT_LINK" ]; then
    current_target="$(readlink "$CURRENT_LINK")"
    case "$current_target" in
      /*) ;;
      *) current_target="$(dirname "$CURRENT_LINK")/$current_target" ;;
    esac
    current_resolved="$(cd "$current_target" 2>/dev/null && pwd -P)" \
      || die "current symlink target is missing or unreadable: $CURRENT_LINK"
    target_resolved="$(cd "$TARGET_DIR" 2>/dev/null && pwd -P)" \
      || die "target release is unreadable: $TARGET_DIR"
    if [ "$current_resolved" = "$target_resolved" ]; then
      TARGET_IS_ACTIVE=1
    fi
  fi
  if [ "$TARGET_IS_ACTIVE" -eq 1 ]; then
    if [ "$FORCE" -eq 1 ]; then
      die "refusing --force for the active current release: $TARGET_DIR"
    fi
    ALREADY_INSTALLED=1
  elif [ "$FORCE" -eq 1 ]; then
    REPLACE_INACTIVE_TARGET=1
  else
    die "target release already exists but is not the active current release: $TARGET_DIR (pass --force to recreate it)"
  fi
fi

check_node_runtime

if [ "$ALREADY_INSTALLED" -eq 1 ]; then
  printf '[install] options-monitor %s is already installed\n' "$TAG"
  installed_version="$(sed -n '1p' "$TARGET_DIR/VERSION" 2>/dev/null || true)"
  [ "$installed_version" = "${TAG#v}" ] \
    || die "active release VERSION mismatch; expected=${TAG#v}; observed=${installed_version:-missing}"
  run_pi_smoke "$TARGET_DIR"
else
  tmp_dir="${RELEASES_DIR}/.${TAG}.tmp.$$"
  staged_om_wrapper="${BIN_DIR}/.om.tmp.$$"
  staged_agent_wrapper="${BIN_DIR}/.om-agent.tmp.$$"
  backup_om_wrapper="${BIN_DIR}/.om.backup.$$"
  backup_agent_wrapper="${BIN_DIR}/.om-agent.backup.$$"
  rm -rf "$tmp_dir"
  trap 'rm -rf "$tmp_dir"; if [ "$INSTALL_CLI" -eq 1 ]; then rm -f "$staged_om_wrapper" "$staged_agent_wrapper" "$backup_om_wrapper" "$backup_agent_wrapper"; fi; rm -f "${CURRENT_LINK}.tmp.$$"' EXIT

  printf '[install] cloning %s at %s\n' "$REPO_URL" "$TAG"
  git clone --depth 1 --branch "$TAG" "$REPO_URL" "$tmp_dir"

  printf '[install] creating virtualenv\n'
  "$PYTHON_BIN" -m venv "$tmp_dir/.venv"
  "$tmp_dir/.venv/bin/pip" install -U pip
  "$tmp_dir/.venv/bin/pip" install -r "$tmp_dir/requirements.txt" -c "$tmp_dir/constraints.txt"

  install_optional_requirements "$tmp_dir"

  printf '[install] installing locked Pi runtime dependencies\n'
  (
    cd "$tmp_dir"
    "$NPM_BIN" ci --omit=dev --ignore-scripts --prefix agent-runtime
  )
  run_pi_smoke "$tmp_dir"

  if [ "$INSTALL_CLI" -eq 1 ]; then
    stage_cli_wrapper "om" "${CURRENT_LINK}/om" "$tmp_dir/om" "$staged_om_wrapper"
    stage_cli_wrapper "om-agent" "${CURRENT_LINK}/om-agent" "$tmp_dir/om-agent" "$staged_agent_wrapper"
    om_wrapper_existed=0
    agent_wrapper_existed=0
    if backup_cli_wrapper "${BIN_DIR}/om" "$backup_om_wrapper"; then
      om_wrapper_existed=1
    fi
    if backup_cli_wrapper "${BIN_DIR}/om-agent" "$backup_agent_wrapper"; then
      agent_wrapper_existed=1
    fi
  fi

  if [ "$REPLACE_INACTIVE_TARGET" -eq 1 ]; then
    rm -rf "$TARGET_DIR"
  fi
  mv "$tmp_dir" "$TARGET_DIR"

  if [ "$INSTALL_CLI" -eq 1 ]; then
    if ! mv -f "$staged_om_wrapper" "${BIN_DIR}/om"; then
      restore_cli_wrapper "${BIN_DIR}/om" "$backup_om_wrapper" "$om_wrapper_existed"
      restore_cli_wrapper "${BIN_DIR}/om-agent" "$backup_agent_wrapper" "$agent_wrapper_existed"
      die "failed to publish CLI wrapper: ${BIN_DIR}/om"
    fi
    if ! mv -f "$staged_agent_wrapper" "${BIN_DIR}/om-agent"; then
      restore_cli_wrapper "${BIN_DIR}/om" "$backup_om_wrapper" "$om_wrapper_existed"
      restore_cli_wrapper "${BIN_DIR}/om-agent" "$backup_agent_wrapper" "$agent_wrapper_existed"
      die "failed to publish CLI wrapper: ${BIN_DIR}/om-agent"
    fi
  fi

  if ! replace_current_link; then
    if [ "$INSTALL_CLI" -eq 1 ]; then
      restore_cli_wrapper "${BIN_DIR}/om" "$backup_om_wrapper" "$om_wrapper_existed"
      restore_cli_wrapper "${BIN_DIR}/om-agent" "$backup_agent_wrapper" "$agent_wrapper_existed"
    fi
    die "failed to atomically switch current release: $CURRENT_LINK"
  fi
  if [ "$INSTALL_CLI" -eq 1 ]; then
    rm -f "$backup_om_wrapper" "$backup_agent_wrapper"
  fi
  trap - EXIT
fi

if [ "$INSTALL_CLI" -eq 1 ] && [ "$ALREADY_INSTALLED" -eq 1 ]; then
  write_cli_wrapper "om" "${CURRENT_LINK}/om"
  write_cli_wrapper "om-agent" "${CURRENT_LINK}/om-agent"
fi

print_next_steps
