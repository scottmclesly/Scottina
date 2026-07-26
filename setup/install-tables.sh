#!/usr/bin/env bash
#
# Scottina tables installer — the CAN/NMEA2K split's converter service.
#
# Installs the converter web app's dependencies (Flask, qrcode, pypdf), the
# kilodash-tables systemd unit, and the decode-table store directories
# (TABLES.md §1) with correct ownership. Idempotent: safe to re-run — a
# second pass reports everything "already correct" and changes nothing. Run
# as root:
#
#     sudo setup/install-tables.sh
#
# Lifecycle note: the unit is installed but deliberately NOT enabled at boot.
# It is on-demand — the Tables tile starts it and it idles itself out after
# ~15 min (Restart=no). Every always-on Scottina unit is `enable`d; this one
# is not, by design. Do not add `systemctl enable` here.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
UNIT="kilodash-tables.service"
UNIT_SRC="$SCRIPT_DIR/$UNIT"
UNIT_DST="/etc/systemd/system/$UNIT"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root:  sudo $0" >&2
  exit 1
fi

say()  { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
did()  { printf '  \033[1;32m+\033[0m %s\n' "$*"; }   # changed something
ok()   { printf '  \033[1;30m·\033[0m %s\n' "$*"; }   # already correct

# ---------------------------------------------------------------- apt deps ---
# Distro packages, not pip — the house discipline (see install-webmirror.sh).
# Never touch system Python via pip here.
say "APT dependencies (Flask + qrcode + pypdf, distro-packaged)"
pkgs=(python3-flask python3-qrcode python3-pypdf)
missing=()
for p in "${pkgs[@]}"; do
  if dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
    ok "$p already installed"
  else
    missing+=("$p")
  fi
done
if [ "${#missing[@]}" -gt 0 ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y "${missing[@]}"
  did "installed: ${missing[*]}"
fi

# ------------------------------------------------------------- table store ---
# Ownership is DERIVED from the unit's User= — the account tableconv actually
# writes the store as — never a hardcoded name. The unit runs as root, so the
# store is owned by root; the checkout's group is granted setgid group-write
# so bench work over SSH can still manage tables without sudo, and root (the
# service) writes regardless.
say "Decode-table store (TABLES.md §1)"
svc_user="$(awk -F= '/^User=/{print $2; exit}' "$UNIT_SRC")"
svc_user="${svc_user:-root}"
svc_group="$(id -gn "$svc_user" 2>/dev/null || echo "$svc_user")"
bench_group="$(stat -c '%G' "$REPO_DIR")"   # checkout group — bench SSH account
echo "  service runs as User=$svc_user ($svc_user:$svc_group); bench group=$bench_group"

for d in tables/pgn tables/dbc tables/uploads captures; do
  path="$REPO_DIR/$d"
  want_owner="$svc_user:$bench_group"
  if [ -d "$path" ] && [ "$(stat -c '%U:%G' "$path")" = "$want_owner" ] \
       && [ "$(stat -c '%a' "$path")" = "2775" ]; then
    ok "$d present ($want_owner, 2775)"
  else
    install -d -o "$svc_user" -g "$bench_group" -m 2775 "$path"
    did "$d ready ($want_owner, 2775)"
  fi
done

# ----------------------------------------------------------------- systemd ---
say "systemd unit ($UNIT)"
if [ -f "$UNIT_DST" ] && cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  ok "$UNIT already up to date"
else
  install -m644 "$UNIT_SRC" "$UNIT_DST"
  systemctl daemon-reload
  did "$UNIT installed + daemon-reload"
fi
# on-demand: loaded and startable, but NOT boot-enabled (see header note)
if systemctl is-enabled --quiet "$UNIT" 2>/dev/null; then
  printf '  \033[1;33m!\033[0m %s\n' \
    "$UNIT is boot-enabled — on-demand design expects it disabled; leaving as-is"
else
  ok "$UNIT left on-demand (tile-started, not boot-enabled)"
fi

say "Done"
echo "  The Tables tile now starts the converter on demand. To try it now:"
echo "    systemctl start $UNIT   # or just open the Tables tile"
echo "  Restart Scottina to pick up any screen changes:"
echo "    systemctl restart kilodash"
