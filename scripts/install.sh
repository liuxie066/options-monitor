#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/liuxie066/options-monitor.git"
PREFIX="${HOME}/apps/options-monitor"
VERSION=""
PYTHON_BIN="${PYTHON:-}"
SOURCE_INSTALL=0
WITH_SERVER=0
WITH_DEV=0
WITH_POSITION_ADVICE=0
WITH_FEISHU=0
WITH_WECHAT=0
WITH_BROKER_FUTU=0
WITH_BROKER_LONGBRIDGE=0
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

The default installer downloads a complete, checksum-verified runtime bundle.
It requires curl, tar and sha256sum or shasum, but no Git, Python or pip.
It verifies the runtime, updates current, and
by default creates user-level CLI wrappers. It does not write runtime config,
write env secrets, start services, create timers, connect to OpenD, send Feishu
messages, or touch SQLite state.

Options:
  --version VERSION     Release tag to install, for example v1.2.118.
                        Default: latest published GitHub release, never main.
  --prefix PATH        Install root. Default: $HOME/apps/options-monitor.
  --repo-url URL       Git repository URL.
  --source             Explicit legacy source installation; requires Git and Python >=3.12.
  --python PATH        Python executable for venv creation. Default: python3.12, then compatible python3.
  --install-cli        Create user-level om and om-agent wrappers. Default.
  --no-install-cli     Do not create user-level CLI wrappers.
  --bin-dir PATH       Wrapper directory. Default: $HOME/.local/bin.
  --force-cli-wrapper  Overwrite existing non-options-monitor wrapper files.
  --with-server        Also install requirements/server.txt.
  --with-dev           Also install requirements/dev.txt (source mode only).
  --with-position-advice Install the optional Position Advice package (source mode only; does not enable it).
  --with-feishu        Install the optional Feishu message package (source mode only; does not enable it).
  --with-wechat        Install the optional WeChat message package (source mode only; does not enable it).
  --with-broker-futu   Install the optional Futu broker package (source mode only; does not enable account access).
  --with-broker-longbridge Install the pinned Longbridge SDK and broker package (source mode only).
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

run_bot_smoke() {
  release_dir="$1"
  if [ -e "$release_dir/om.runtime.json" ] || [ -e "$release_dir/runtime" ]; then
    PYTHON_BIN="$release_dir/runtime/python/bin/python3"
    [ -x "$PYTHON_BIN" ] || die "declared bundled Python is missing: $PYTHON_BIN"
    (cd "$release_dir" && PYTHONDONTWRITEBYTECODE=1 "$PYTHON_BIN" -c 'import sys; from pathlib import Path; from src.application.runtime_bundle import load_manifest, current_platform, validate_runtime; load_manifest(Path.cwd(), expected_tag=sys.argv[1], expected_platform=current_platform(), verify_files=True); validate_runtime(Path.cwd())' "$TAG")
  else
    [ "$SOURCE_INSTALL" -eq 1 ] || die "runtime manifest missing; source mode must be explicit"
    (cd "$release_dir" && "$release_dir/.venv/bin/python" -c 'import src.application.bot.host; import src.application.bot.runtime')
    validate_feishu="$WITH_FEISHU"
    validate_wechat="$WITH_WECHAT"
    if [ "$WITH_DEV" -eq 1 ]; then
      if [ -f "$release_dir/plugins/feishu-channel/pyproject.toml" ]; then validate_feishu=1; fi
      if [ -f "$release_dir/plugins/wechat-channel/pyproject.toml" ]; then validate_wechat=1; fi
    fi
    if [ "$validate_feishu" -eq 1 ] || [ "$validate_wechat" -eq 1 ]; then
      (cd "$release_dir" && "$release_dir/.venv/bin/python" -c 'import subprocess,sys; from pathlib import Path; from src.application.service_upgrade import _validate_message_runtime; _validate_message_runtime(target_dir=Path.cwd(), include_feishu=sys.argv[1]=="1", include_wechat=sys.argv[2]=="1", run_cmd=subprocess.run, operations=[])' "$validate_feishu" "$validate_wechat")
    fi
  fi
}

quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
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
  if [ "$WITH_SERVER" -ne 1 ] && [ "$WITH_DEV" -ne 1 ] && [ "$WITH_POSITION_ADVICE" -ne 1 ] && [ "$WITH_FEISHU" -ne 1 ] && [ "$WITH_WECHAT" -ne 1 ] && [ "$WITH_BROKER_FUTU" -ne 1 ] && [ "$WITH_BROKER_LONGBRIDGE" -ne 1 ]; then
    return 0
  fi
  pip_bin="${release_dir}/.venv/bin/pip"
  [ -x "$pip_bin" ] || die "cannot install optional requirements; pip is missing: $pip_bin"
  (cd "$release_dir"
  if [ "$WITH_BROKER_LONGBRIDGE" -eq 1 ]; then
    "$pip_bin" install -r "$release_dir/requirements/longbridge.txt"
  fi
  if [ "$WITH_SERVER" -eq 1 ]; then
    "$pip_bin" install -r "$release_dir/requirements/server.txt" -c "$release_dir/constraints/server.txt"
  fi
  if [ "$WITH_DEV" -eq 1 ]; then
    "$pip_bin" install -r "$release_dir/requirements/dev.txt" -c "$release_dir/constraints/dev.txt"
  else
    if [ "$WITH_POSITION_ADVICE" -eq 1 ]; then
      "$pip_bin" install ./plugins/position-advice
    fi
    if [ "$WITH_FEISHU" -eq 1 ]; then
      "$pip_bin" install ./plugins/feishu-channel
    fi
    if [ "$WITH_WECHAT" -eq 1 ]; then
      "$pip_bin" install ./plugins/wechat-channel
    fi
    if [ "$WITH_BROKER_FUTU" -eq 1 ]; then
      "$pip_bin" install ./plugins/broker-futu
    fi
    if [ "$WITH_BROKER_LONGBRIDGE" -eq 1 ]; then
      "$pip_bin" install ./plugins/broker-longbridge
    fi
  fi
  )
}

install_default_market() {
  release_dir="$1"
  # Historical releases have no provider packages; their builtin contract stays intact.
  [ -f "$release_dir/src/application/plugin_runtime.py" ] || return 0
  if [ -f "$release_dir/om.runtime.json" ]; then
    market_python="$release_dir/runtime/python/bin/python3"
  else
    market_python="$release_dir/.venv/bin/python"
  fi
  (cd "$release_dir" && PYTHONDONTWRITEBYTECODE=1 "$market_python" -c '
import importlib.metadata as m
from pathlib import Path
import sys
from src.application.plugin_runtime import PLUGINS, require_plugin, validate_plugin_import
if "market-opend" not in PLUGINS:
    sys.exit(0)
from src.application.plugin_distribution import install_plugin
try:
    m.distribution(PLUGINS["market-opend"]["distribution"])
except m.PackageNotFoundError:
    install_plugin("market-opend", repo_root=Path.cwd(), confirm=True, python_executable=Path(sys.executable))
require_plugin("market-opend")
validate_plugin_import("market-opend")
for selected, plugin in ((sys.argv[1], "broker-futu"), (sys.argv[2], "broker-longbridge")):
    if selected == "1":
        validate_plugin_import(plugin)
' "$WITH_BROKER_FUTU" "$WITH_BROKER_LONGBRIDGE")
}


guard_managed_instance() {
  [ -L "$CURRENT_LINK" ] || return 0
  "$PYTHON_BIN" - "$PREFIX" "$CURRENT_LINK" "$OS_NAME" <<'PYGUARD'
import json, os, sys
from pathlib import Path
prefix, current = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
home = Path.home()
roots = [prefix / 'shared', prefix / 'runtime', current]
if sys.argv[3] == 'Darwin':
    roots.append(home / 'Library/Application Support/options-monitor')
else:
    roots.append(Path('/var/lib/options-monitor'))
if os.environ.get('OM_RUNTIME_ROOT'):
    roots.append(Path(os.environ['OM_RUNTIME_ROOT']))
record = home / '.config/options-monitor/runtime-root'
if record.exists():
    value = record.read_text().strip()
    if not value or not Path(value).is_absolute():
        raise SystemExit('install.sh: cannot bind runtime-root record; preserve current and inspect the record')
    roots.append(Path(value))
for root in roots:
    profile = root / 'service.profile.json'
    if not profile.exists():
        continue
    try:
        data = json.loads(profile.read_text())
        owner = Path(data['repo_root']).resolve()
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(f'install.sh: cannot bind service profile: {profile}; preserving current')
    if owner == current or owner.is_relative_to(prefix / 'releases') or owner == prefix / 'current':
        raise SystemExit(f'install.sh: managed instance detected: {profile}; use om service upgrade')
PYGUARD
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
exec $(quote "$target") "\$@"
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
exec $(quote "$target") "\$@"
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

cleanup_install_transaction() {
  local cleanup_status=$?
  local cleanup_failed=0 cleanup_state=indeterminate cleanup_current
  local cleanup_name cleanup_wrapper cleanup_backup cleanup_existed cleanup_armed
  trap - EXIT INT TERM

  if [ -L "$CURRENT_LINK" ]; then
    if cleanup_current="$(readlink "$CURRENT_LINK")"; then
      if [ "$cleanup_current" = "$TARGET_DIR" ]; then
        cleanup_state=committed
      elif [ "$initial_current_existed" -eq 1 ] && [ "$cleanup_current" = "$initial_current_target" ]; then
        cleanup_state=uncommitted
      fi
    fi
  elif [ "$initial_current_existed" -eq 0 ] && [ ! -e "$CURRENT_LINK" ]; then
    cleanup_state=uncommitted
  fi
  if [ "$cleanup_state" = indeterminate ]; then
    printf 'install.sh: cannot determine current commit state; preserving CLI backups: %s\n' "$CURRENT_LINK" >&2
    cleanup_failed=1
  fi

  if [ "$INSTALL_CLI" -eq 1 ]; then
    for cleanup_name in om om-agent; do
      cleanup_wrapper="${BIN_DIR}/${cleanup_name}"
      if [ "$cleanup_name" = om ]; then
        cleanup_backup="$backup_om_wrapper"
        cleanup_existed="$om_wrapper_existed"
        cleanup_armed="$om_rollback_armed"
      else
        cleanup_backup="$backup_agent_wrapper"
        cleanup_existed="$agent_wrapper_existed"
        cleanup_armed="$agent_rollback_armed"
      fi
      if [ "$cleanup_state" = indeterminate ]; then
        if [ -e "$cleanup_backup" ] || [ -L "$cleanup_backup" ]; then
          printf 'install.sh: retained CLI backup: %s -> %s\n' "$cleanup_backup" "$cleanup_wrapper" >&2
        fi
      elif [ "$cleanup_state" = uncommitted ] && [ "$cleanup_armed" -eq 1 ]; then
        if restore_cli_wrapper "$cleanup_wrapper" "$cleanup_backup" "$cleanup_existed"; then
          if [ "$cleanup_name" = om ]; then om_rollback_armed=0; else agent_rollback_armed=0; fi
        else
          printf 'install.sh: failed to restore CLI wrapper: %s; backup retained if present: %s\n' "$cleanup_wrapper" "$cleanup_backup" >&2
          cleanup_failed=1
        fi
      elif ! rm -f "$cleanup_backup"; then
        printf 'install.sh: failed to remove CLI backup: %s\n' "$cleanup_backup" >&2
        cleanup_failed=1
      fi
    done
    if ! rm -f "$staged_om_wrapper" "$staged_agent_wrapper"; then
      printf 'install.sh: failed to remove staged CLI wrappers: %s, %s\n' "$staged_om_wrapper" "$staged_agent_wrapper" >&2
      cleanup_failed=1
    fi
  fi
  if ! rm -rf "$tmp_dir"; then
    printf 'install.sh: failed to remove staged release: %s\n' "$tmp_dir" >&2
    cleanup_failed=1
  fi
  if ! rm -f "${CURRENT_LINK}.tmp.$$"; then
    printf 'install.sh: failed to remove staged current link: %s\n' "${CURRENT_LINK}.tmp.$$" >&2
    cleanup_failed=1
  fi
  if [ "$cleanup_status" -eq 0 ] && [ "$cleanup_failed" -ne 0 ]; then cleanup_status=1; fi
  exit "$cleanup_status"
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
  printf '\nOM %s 已安装，程序检查通过。\n' "$TAG"
  printf '下一步：运行 OM（首次配置或继续使用）。\n  '
  if [ "$INSTALL_CLI" -eq 1 ]; then
    printf '%s\n' "$(quote "${BIN_DIR}/om")"
  else
    printf '%s\n' "$(quote "${CURRENT_LINK}/om")"
  fi
  printf '任务帮助：om help；结构化工具：om tools spec；自然语言助手：om bot --help。om-agent 保留兼容。\n'
}

bundle_platform() {
  case "${OS_NAME}-$(uname -m)" in
    Darwin-arm64|Darwin-aarch64) printf 'macos-arm64' ;;
    Darwin-x86_64) printf 'macos-x86_64' ;;
    Linux-x86_64) printf 'linux-x86_64' ;;
    *) die "unsupported runtime platform: ${OS_NAME}-$(uname -m); no source fallback (use --source explicitly)" ;;
  esac
}

preflight_bundle_tools() {
  missing=""
  for tool in curl tar awk sed; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if command -v sha256sum >/dev/null 2>&1; then
    CHECKSUM_TOOL=sha256sum
  elif command -v shasum >/dev/null 2>&1; then
    CHECKSUM_TOOL=shasum
  else
    missing="$missing sha256sum-or-shasum"
  fi
  [ -z "$missing" ] || die "missing bundle installation tools:$missing"
  BUNDLE_PLATFORM="$(bundle_platform)"
  [ "$WITH_DEV" -eq 0 ] || die "--with-dev requires explicit --source mode"
  [ "$WITH_POSITION_ADVICE" -eq 0 ] || die "--with-position-advice requires explicit --source mode"
  [ "$WITH_FEISHU" -eq 0 ] || die "--with-feishu requires explicit --source mode"
  [ "$WITH_WECHAT" -eq 0 ] || die "--with-wechat requires explicit --source mode"
  [ "$WITH_BROKER_FUTU" -eq 0 ] || die "--with-broker-futu requires explicit --source mode"
  [ "$WITH_BROKER_LONGBRIDGE" -eq 0 ] || die "--with-broker-longbridge requires explicit --source mode"
  # Check the pinned artifact floor before executing any bundled binary.
  case "$BUNDLE_PLATFORM" in
    macos-*)
      command -v sw_vers >/dev/null 2>&1 || die "cannot determine macOS version"
      system_version="$(sw_vers -productVersion)"
      system_major="${system_version%%.*}"
      case "$system_major" in ''|*[!0-9]*) die "cannot determine macOS version" ;; esac
      [ "$system_major" -ge 14 ] || die "runtime bundle requires macOS >= 14.0; observed=$system_version"
      ;;
    linux-*)
      command -v getconf >/dev/null 2>&1 || die "getconf is required to verify glibc >= 2.28"
      libc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null)" || die "runtime bundle requires glibc >= 2.28; musl is unsupported"
      case "$libc_version" in 'glibc '*) libc_version="${libc_version#glibc }" ;; *) die "cannot determine glibc version" ;; esac
      libc_major="${libc_version%%.*}"
      libc_minor="${libc_version#*.}"
      libc_minor="${libc_minor%%.*}"
      case "$libc_major:$libc_minor" in *[!0-9:]*|:|*:) die "cannot determine glibc version" ;; esac
      if [ "$libc_major" -lt 2 ] || { [ "$libc_major" -eq 2 ] && [ "$libc_minor" -lt 28 ]; }; then
        die "runtime bundle requires glibc >= 2.28; observed=$libc_version"
      fi
      ;;
  esac
}

stage_bundle() {
  slug="$(github_repo_slug_from_url "$REPO_URL")" || die "bundle installation requires a GitHub repository URL"
  [ "$slug" = liuxie066/options-monitor ] || die "unsupported bundle repository: $slug"
  case "$TAG" in *-*) die "bundle installation requires a published stable version" ;; esac
  name="options-monitor-${TAG}-${BUNDLE_PLATFORM}.tar.gz"
  url="https://github.com/${slug}/releases/download/${TAG}/${name}"
  mkdir -p "$tmp_dir"
  archive="$tmp_dir/.download.tar.gz"
  checksum_file="$tmp_dir/.download.sha256"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 2 "$url.sha256" -o "$checksum_file" \
    || die "published runtime bundle unavailable for $BUNDLE_PLATFORM; no source fallback"
  expected="$(awk -v name="$name" 'NF == 2 && $2 == name && length($1) == 64 && $1 !~ /[^0-9a-f]/ { print $1 }' "$checksum_file")"
  [ "${#expected}" -eq 64 ] || die "invalid runtime asset checksum"
  curl --proto '=https' --tlsv1.2 -fsSL --retry 2 "$url" -o "$archive" || die "runtime bundle download failed"
  if [ "$CHECKSUM_TOOL" = sha256sum ]; then
    actual="$(sha256sum "$archive" | awk '{print $1}')"
  else
    actual="$(shasum -a 256 "$archive" | awk '{print $1}')"
  fi
  [ "$actual" = "$expected" ] || die "runtime bundle SHA256 mismatch"
  # No interpreter is available yet. The build contract forbids links and special files.
  tar -tzf "$archive" > "$tmp_dir/.archive-paths" || die "invalid runtime archive"
  awk '
    $0 !~ /^[A-Za-z0-9_.\/+@() -]+$/ || /^\// { exit 1 }
    { n=split($0,p,"/"); for(i=1;i<=n;i++) if(p[i]==".." || p[i]==".") exit 1; if(seen[$0]++) exit 1 }
  ' "$tmp_dir/.archive-paths" || die "unsafe runtime archive path"
  tar -tvzf "$archive" > "$tmp_dir/.archive-types" || die "invalid runtime archive"
  awk 'substr($0,1,1)!="-" && substr($0,1,1)!="d" { exit 1 }' "$tmp_dir/.archive-types" \
    || die "runtime archive links/special files are forbidden"
  tar -xOzf "$archive" om.runtime.json > "$tmp_dir/.runtime-manifest" || die "runtime manifest missing"
  manifest_platform="$(sed -n 's/^[[:space:]]*"platform"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp_dir/.runtime-manifest")"
  manifest_minimum="$(sed -n 's/^[[:space:]]*"minimum_system"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$tmp_dir/.runtime-manifest")"
  [ "$manifest_platform" = "$BUNDLE_PLATFORM" ] || die "runtime manifest platform mismatch"
  case "$BUNDLE_PLATFORM:$manifest_minimum" in
    macos-*:macOS\ 14.0) ;;
    linux-x86_64:'glibc 2.28; optional services require systemd and systemd-creds') ;;
    *) die "runtime bundle minimum_system differs from this installer; use the matching release installer" ;;
  esac
  tar -xzf "$archive" -C "$tmp_dir" || die "runtime bundle extraction failed"
  rm -f "$archive" "$checksum_file" "$tmp_dir/.archive-paths" "$tmp_dir/.archive-types" "$tmp_dir/.runtime-manifest"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source)
      SOURCE_INSTALL=1
      shift
      ;;
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
    --with-position-advice)
      WITH_POSITION_ADVICE=1
      shift
      ;;
    --with-feishu)
      WITH_FEISHU=1
      shift
      ;;
    --with-wechat)
      WITH_WECHAT=1
      shift
      ;;
    --with-broker-futu)
      WITH_BROKER_FUTU=1
      shift
      ;;
    --with-broker-longbridge)
      WITH_BROKER_LONGBRIDGE=1
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

if [ "$SOURCE_INSTALL" -eq 1 ]; then
  if [ -z "$PYTHON_BIN" ]; then
    if command -v python3.12 >/dev/null 2>&1; then PYTHON_BIN="python3.12"; else PYTHON_BIN="python3"; fi
  fi
  command -v git >/dev/null 2>&1 || die "$(missing_git_message)"
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "$(missing_python_message)"
  check_python_runtime
else
  preflight_bundle_tools
fi
if [ -z "$VERSION" ] || [ "$VERSION" = latest ]; then
  TAG="$(resolve_latest_release_tag)"
  printf '[install] resolved latest release: %s\n' "$TAG"
else
  TAG="$(normalize_tag "$VERSION")"
fi
validate_tag "$TAG"

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


if [ "$ALREADY_INSTALLED" -eq 1 ]; then
  printf '[install] options-monitor %s is already installed\n' "$TAG"
  installed_version="$(sed -n '1p' "$TARGET_DIR/VERSION" 2>/dev/null || true)"
  [ "$installed_version" = "${TAG#v}" ] \
    || die "active release VERSION mismatch; expected=${TAG#v}; observed=${installed_version:-missing}"
  run_bot_smoke "$TARGET_DIR"
  install_default_market "$TARGET_DIR"
else
  initial_current_existed=0
  initial_current_target=""
  if [ -L "$CURRENT_LINK" ]; then
    initial_current_target="$(readlink "$CURRENT_LINK")" \
      || die "cannot read initial current symlink: $CURRENT_LINK"
    initial_current_existed=1
    [ "$initial_current_target" != "$TARGET_DIR" ] \
      || die "initial current already points to target on new publication path: $CURRENT_LINK -> $TARGET_DIR"
  fi
  tmp_dir="${RELEASES_DIR}/.${TAG}.tmp.$$"
  staged_om_wrapper="${BIN_DIR}/.om.tmp.$$"
  staged_agent_wrapper="${BIN_DIR}/.om-agent.tmp.$$"
  backup_om_wrapper="${BIN_DIR}/.om.backup.$$"
  backup_agent_wrapper="${BIN_DIR}/.om-agent.backup.$$"
  om_wrapper_existed=0
  agent_wrapper_existed=0
  om_rollback_armed=0
  agent_rollback_armed=0
  trap cleanup_install_transaction EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  rm -rf "$tmp_dir"

  if [ "$SOURCE_INSTALL" -eq 1 ]; then
    printf '[install] cloning %s at %s\n' "$REPO_URL" "$TAG"
    git clone --depth 1 --branch "$TAG" "$REPO_URL" "$tmp_dir"
    source_tag_commit="$(git -C "$tmp_dir" rev-parse --verify "refs/tags/${TAG}^{commit}" 2>/dev/null)" \
      || die "source release tag missing or invalid: $TAG"
    source_checkout_commit="$(git -C "$tmp_dir" rev-parse HEAD)" \
      || die "cannot establish source checkout identity: $TAG"
    [ "$source_tag_commit" = "$source_checkout_commit" ] \
      || die "source checkout does not match requested release tag: $TAG"
    source_version="$(cat "$tmp_dir/VERSION" 2>/dev/null || true)"
    [ "$source_version" = "${TAG#v}" ] \
      || die "source release VERSION mismatch; expected=${TAG#v}; observed=${source_version:-missing}"
    # Clone does not honor export-ignore; also protect installs of older tags.
    find "$tmp_dir" -name .git -prune -o \
      \( -name AGENTS.md -o -name CLAUDE.md -o -name .agents \) -prune -exec rm -rf {} +
    printf '[install] creating virtualenv\n'
    "$PYTHON_BIN" -m venv "$tmp_dir/.venv"
    "$tmp_dir/.venv/bin/pip" install -U pip
    "$tmp_dir/.venv/bin/pip" install -r "$tmp_dir/requirements.txt" -c "$tmp_dir/constraints.txt"
    install_optional_requirements "$tmp_dir"
  else
    stage_bundle
  fi

  run_bot_smoke "$tmp_dir"
  guard_managed_instance

  if [ "$INSTALL_CLI" -eq 1 ]; then
    stage_cli_wrapper "om" "${CURRENT_LINK}/om" "$tmp_dir/om" "$staged_om_wrapper"
    stage_cli_wrapper "om-agent" "${CURRENT_LINK}/om-agent" "$tmp_dir/om-agent" "$staged_agent_wrapper"
    if backup_cli_wrapper "${BIN_DIR}/om" "$backup_om_wrapper"; then
      om_wrapper_existed=1
    fi
    om_rollback_armed=1
    if backup_cli_wrapper "${BIN_DIR}/om-agent" "$backup_agent_wrapper"; then
      agent_wrapper_existed=1
    fi
    agent_rollback_armed=1
  fi

  if [ "$REPLACE_INACTIVE_TARGET" -eq 1 ]; then
    rm -rf "$TARGET_DIR"
  fi
  mv "$tmp_dir" "$TARGET_DIR"
  if [ "$SOURCE_INSTALL" -eq 0 ]; then PYTHON_BIN="$TARGET_DIR/runtime/python/bin/python3"; fi
  install_default_market "$TARGET_DIR"

  if [ "$INSTALL_CLI" -eq 1 ]; then
    if ! mv -f "$staged_om_wrapper" "${BIN_DIR}/om"; then
      die "failed to publish CLI wrapper: ${BIN_DIR}/om"
    fi
    if ! mv -f "$staged_agent_wrapper" "${BIN_DIR}/om-agent"; then
      die "failed to publish CLI wrapper: ${BIN_DIR}/om-agent"
    fi
  fi

  if ! replace_current_link; then
    die "failed to atomically switch current release: $CURRENT_LINK"
  fi
fi

if [ "$INSTALL_CLI" -eq 1 ] && [ "$ALREADY_INSTALLED" -eq 1 ]; then
  write_cli_wrapper "om" "${CURRENT_LINK}/om"
  write_cli_wrapper "om-agent" "${CURRENT_LINK}/om-agent"
fi

print_next_steps
