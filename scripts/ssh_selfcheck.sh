#!/usr/bin/env bash
set -euo pipefail

REPO_NAME=${REPO_NAME:-options-monitor}
HOST_ALIAS=${HOST_ALIAS:-github-options-monitor}
# Use canonical GitHub host by default so we do NOT depend on any local ssh config / host alias.
REPO_SSH=${REPO_SSH:-git@github.com:liuxie85/options-monitor.git}
KEY_DIR=${KEY_DIR:-/home/node/.openclaw/secrets/ssh/${REPO_NAME}}
KEY_FILE=${KEY_FILE:-${KEY_DIR}/id_ed25519}
PUB_FILE=${PUB_FILE:-${KEY_DIR}/id_ed25519.pub}

red() { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }

fail() { red "[FAIL] $*"; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"; }

need_cmd ssh
need_cmd ssh-keygen
need_cmd git

printf "[INFO] repo=%s\n" "$REPO_NAME"
printf "[INFO] host_alias=%s\n" "$HOST_ALIAS"
printf "[INFO] repo_ssh=%s\n" "$REPO_SSH"
printf "[INFO] key_file=%s\n" "$KEY_FILE"

# 1) key existence
[ -f "$KEY_FILE" ] || fail "private key missing: $KEY_FILE"
[ -f "$PUB_FILE" ] || fail "public key missing: $PUB_FILE"

# 2) permissions sanity (best-effort)
perm=$(stat -c '%a' "$KEY_FILE" 2>/dev/null || echo "")
if [ -n "$perm" ] && [ "$perm" != "600" ]; then
  yellow "[WARN] key permission is $perm (recommended 600): $KEY_FILE"
fi

# 3) show fingerprint + pubkey (for GitHub Deploy keys)
fp=$(ssh-keygen -lf "$PUB_FILE" | awk '{print $2}')
echo "[INFO] pubkey fingerprint: $fp"
echo "[INFO] pubkey (copy this entire line into GitHub → Settings → Deploy keys, and enable write access):"
cat "$PUB_FILE"

# 4) ssh auth check
# We use -F /dev/null so we don't depend on machine-level ssh config.
# Use explicit identity.
set +e
ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$KEY_FILE" -T git@github.com 2>&1 | sed 's/^/[SSH] /'
rc=$?
set -e
if [ $rc -ne 1 ] && [ $rc -ne 255 ]; then
  # GitHub returns exit code 1 on success with the "no shell access" banner.
  yellow "[WARN] unexpected ssh exit code: $rc (still may be OK)"
fi

# 5) git remote sanity
set +e
GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i '$KEY_FILE'" \
  git ls-remote "$REPO_SSH" -h HEAD >/dev/null 2>&1
rc2=$?
set -e
if [ $rc2 -ne 0 ]; then
  cat <<EOF

[FAIL] git cannot access remote via SSH.

Likely causes:
- The public key above is not added to the GitHub repo Deploy keys, or "Allow write access" is not enabled.
- You're using a different key than what GitHub has.

Action:
1) Go to GitHub repo → Settings → Deploy keys
2) Add/replace the deploy key with the pubkey printed above, check "Allow write access".
3) Re-run this script.
EOF
  exit 2
fi

green "[OK] SSH + git remote access look good."
