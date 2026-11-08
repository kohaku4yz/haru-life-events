#!/usr/bin/env bash
# Root-only installer for the Vitals source (stages only; the context worker delivers).
set -euo pipefail
set +x
unit=haru-vitals-source.service
repo=/opt/haru-life-events
mode="${1:---status}"
case "$mode" in
  --status)
    printf 'SOURCE_ONLY_UNIT=';systemctl is-active "$unit" 2>/dev/null || true
    printf 'WORK_VITALS_DELIVERY=via_context_worker\n'
    exit 0;;
  --install|--disable) ;;
  *) echo 'Usage: --install | --status | --disable' >&2;exit 2;;
esac
[[ "$(id -u)" == 0 ]] || { echo 'Root SSH required' >&2; exit 2; }
if [[ "$mode" == --disable ]];then
  systemctl disable --now "$unit" || true
  printf 'VITALS_SOURCE=disabled\nEXISTING_LOCATION=unchanged\n'
  exit 0
fi
[[ -z "$(git -c safe.directory="$repo" -C "$repo" status --porcelain)" ]] || {
  echo 'Events source has uncommitted changes; stop' >&2; exit 3;
}
[[ -f "$repo/deploy/systemd/$unit" ]] || exit 3
[[ -f "$repo/.venv/bin/python" ]] || exit 3
[[ -d /var/lib/haru-life-events/.local/share/haru-life-events ]] || {
  echo 'Existing private state directory missing' >&2; exit 3;
}
# Never let a previously customized unit be silently replaced.
if [[ -e /etc/systemd/system/$unit ]] && ! grep -qE '^Description=Haru PulsHealth Vitals source \(stages for the context worker\)$' "/etc/systemd/system/$unit";then
  echo 'Unexpected existing Vitals unit; refusing replacement' >&2; exit 4
fi
# Confirm the deployed Health Service has the approved read-only endpoint.
result="$(curl -sS --max-time 4 -o /dev/null -w '%{http_code}' 'http://127.0.0.1:8768/v1/health/vitals-outbox?limit=1')"
[[ "$result" == 200 ]] || { echo 'Health outbox unavailable' >&2; exit 4; }
unit_file="$repo/deploy/systemd/$unit"
# The stage-only module holds no Work credentials and cannot send.
grep -q -- '-m scripts.vitals_source$' "$unit_file" || {
  echo 'Vitals unit must run the stage-only scripts.vitals_source' >&2;exit 4;
}
grep -q '^Environment=HOME=/var/lib/haru-life-events$' "$unit_file" || exit 4
install -m 0644 -o root -g root "$unit_file" "/etc/systemd/system/$unit"
systemctl daemon-reload
systemctl enable --now "$unit"
systemctl is-active --quiet "$unit"
printf 'VITALS_SOURCE=active\n'
printf 'WORK_VITALS_NOTIFICATIONS=disabled\n'
printf 'WORK_SUBSCRIPTIONS_CREATED=0\n'
printf 'EXISTING_LOCATION=unchanged\n'
printf 'HEALTH_SOURCE=pulshealth_only\n'
printf 'EVENTS_SOURCE_COMMIT='; git -c safe.directory="$repo" -C "$repo" rev-parse --short HEAD
