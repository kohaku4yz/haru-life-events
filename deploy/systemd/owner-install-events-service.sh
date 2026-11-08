#!/usr/bin/env bash
# Explicit owner operation from root VPS SSH. Never enables location or health producers.
set -euo pipefail
set +x
unit=haru-life-events-canary.service
repo=/opt/haru-life-events
source_unit="$repo/deploy/systemd/$unit"
destination="/etc/systemd/system/$unit"
if [[ "$(id -u)" != "0" ]];then echo "Requires root SSH shell." >&2;exit 2;fi
if [[ "$#" -eq 0 ]];then set -- --install;fi
case "$1" in
  --install)
    if [[ ! -f "$source_unit" || ! -f "$repo/scripts/owner_tunnel_canary.py" ]];then
      echo "Source files missing." >&2;exit 3
    fi
    if [[ -e "$destination" ]] && ! cmp -s "$source_unit" "$destination";then
      echo "Existing unit differs; refusing overwrite." >&2;exit 3
    fi
    if ! test -s /var/lib/haru-life-events/.config/haru-life-events/control-plane-runtime.key;then
      echo "Runtime key missing." >&2;exit 3
    fi
    install -m 0644 "$source_unit" "$destination"
    systemctl daemon-reload
    systemctl enable --now "$unit"
    if ! systemctl is-active --quiet "$unit";then
      echo "Managed unit failed to start." >&2;exit 4
    fi
    echo "Independent Life Events service active + enabled."
    echo "Telegram/Vitals capture and autonomous events are unchanged and disabled."
    ;;
  --status)
    systemctl is-enabled "$unit" || :
    systemctl is-active "$unit" || :
    ;;
  --stop)
    systemctl disable --now "$unit" 2>/dev/null || :
    echo "Independent Life Events service stopped. Work subscription untouched."
    ;;
  *)
    echo "Usage: bash $0 --install|--status|--stop" >&2
    exit 2
    ;;
esac
