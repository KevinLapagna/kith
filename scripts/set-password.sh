#!/usr/bin/env bash
# Set (or replace) Kith's login password and switch the gate on.
#
# Run it on the HOST as the tenant user, from a real terminal:
#
#   ssh -t fedora@pantheon.opq.ch \
#     'sudo -iu barn XDG_RUNTIME_DIR=/run/user/1001 bash /home/barn/kith/scripts/set-password.sh'
#
# `-i` (login shell) is the reliable form: it starts in barn's home and sets HOME=/home/barn,
# which is also where the env file lives.
#
# With a plain `sudo -u barn` the command inherits the *caller's* working directory
# (/home/fedora, mode 0700), and rootless podman resolves its working directory on startup —
# so it aborts with "cannot chdir to /home/fedora: Permission denied". Because the password
# prompts come before that step, it looks like the password was rejected. The `cd` guard
# below makes this work anyway; `-i` avoids the situation entirely.
#
# Why this is a script rather than three commands you paste: the scrypt hash contains
# `$` characters, so doing it by hand with sed is a trap — in double quotes the shell
# expands `$16384` and silently writes a broken value that only shows up as "wrong
# password" later. This writes the file with awk, never echoes the password, keeps a
# backup, and proves at the end that the gate is actually on.
#
# Testable without touching the real credential:
#   KITH_ENV_FILE=/tmp/scratch.env KITH_SKIP_RESTART=1 bash scripts/set-password.sh
set -euo pipefail

# Rootless podman resolves the working directory when it starts and fails if it cannot read
# it ("cannot chdir to …: Permission denied"). Never inherit a directory the tenant cannot
# enter — this is what made the first run of this script look like a wrong password.
cd "$HOME" 2>/dev/null || cd / 2>/dev/null || true

ENV_FILE="${KITH_ENV_FILE:-$HOME/.config/kith/kith.env}"
CONTAINER="${KITH_CONTAINER:-kith}"
UNIT="${KITH_UNIT:-kith}"
PORT="${KITH_PORT_HOST:-8080}"

[[ -f "$ENV_FILE" ]] || { echo "no env file at $ENV_FILE" >&2; exit 1; }

read -r -s -p "New Kith password: " pw1; echo
read -r -s -p "Repeat: " pw2; echo
[[ -n "$pw1" ]] || { echo "refusing an empty password" >&2; exit 1; }
if [[ "$pw1" != "$pw2" ]]; then echo "the two entries differ — nothing was changed" >&2; exit 1; fi

# Hash it INSIDE the container, with the password arriving on stdin. Not as an
# argument: arguments are visible in `ps` on both sides of the exec.
hash="$(printf '%s' "$pw1" | podman exec -i "$CONTAINER" python -c \
  'import sys; from app import auth; print(auth.hash_password(sys.stdin.read()))')"
unset pw1 pw2

if [[ "$hash" != scrypt\$* ]]; then
  echo "hashing failed, the container said: $hash" >&2
  exit 1
fi

secret="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
backup="$ENV_FILE.$(date +%Y%m%d-%H%M%S).bak"
cp -p "$ENV_FILE" "$backup"

# Rewrite the four gate-related keys in place, adding any that are missing (the env
# file on the host may predate them). `q` is a literal single quote: the hash is
# quoted in the file so that both the systemd and the podman env-file parsers leave
# its `$` characters alone.
awk -v h="$hash" -v s="$secret" -v q="'" '
  /^KITH_PASSWORD_HASH=/  { print "KITH_PASSWORD_HASH=" q h q;  seen["p"]=1; next }
  /^KITH_AUTH=/           { print "KITH_AUTH=on";               seen["a"]=1; next }
  /^KITH_SECURE_COOKIES=/ { print "KITH_SECURE_COOKIES=on";     seen["c"]=1; next }
  /^KITH_SESSION_SECRET=/ { print "KITH_SESSION_SECRET=" s;     seen["s"]=1; next }
  { print }
  END {
    if (!seen["p"]) print "KITH_PASSWORD_HASH=" q h q
    if (!seen["a"]) print "KITH_AUTH=on"
    if (!seen["c"]) print "KITH_SECURE_COOKIES=on"
    if (!seen["s"]) print "KITH_SESSION_SECRET=" s
  }
' "$backup" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo
echo "written: $(grep -c . "$ENV_FILE") non-empty lines in $ENV_FILE"
echo "backup : $backup"
echo "         (KITH_PASSWORD_HASH is not printed — it is a credential even as a hash)"

if [[ "${KITH_SKIP_RESTART:-0}" == "1" ]]; then
  echo "KITH_SKIP_RESTART=1 — not restarting, not checking the gate."
  exit 0
fi

systemctl --user restart "$UNIT"
sleep 6

# Prove the gate is ON without knowing the password: the front page must redirect
# (303) and the login page must render (200).
root_code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/")"
login_code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/login")"
echo
echo "gate check:"
echo "  GET /      -> ${root_code}   (303 = redirected to the login page, as it should be)"
echo "  GET /login -> ${login_code}   (200 = the login page renders)"
if [[ "$root_code" != "303" || "$login_code" != "200" ]]; then
  echo "  ^ that is not the expected pair — check: journalctl --user -u $UNIT -n 30" >&2
  exit 1
fi
echo
echo "Done. Sign in with the password you just typed."
