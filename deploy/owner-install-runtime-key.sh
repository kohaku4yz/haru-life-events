#!/usr/bin/env bash
# Install a dedicated OpenAI Tunnel RUNTIME key. Never prints or logs the key.
# Does not install services or enable event processing.
set -euo pipefail
set +x
umask 077

target="/var/lib/haru-life-events/.config/haru-life-events/control-plane-runtime.key"
parent="$(dirname "$target")"

if [[ ! -t 0 || ! -r /dev/tty ]]; then
  printf '%s\n' "Use this script from an interactive VPS SSH terminal (TTY required)." >&2
  exit 2
fi
if [[ ! -d "$parent" || ! -f "$target" || -L "$target" ]]; then
  printf '%s\n' "Private Haru Life Events canary config not found." >&2
  exit 2
fi
if [[ "$(stat -c %a "$parent")" != "700" ]]; then
  printf '%s\n' "Private configuration directory must be mode 700." >&2
  exit 2
fi
if [[ "$(id -un)" != "root" && "$(id -un)" != "haru" ]]; then
  printf '%s\n' "Use the VPS root or haru account." >&2
  exit 2
fi

printf '%s\n' "Paste the dedicated OpenAI Tunnels runtime API key below."
printf '%s\n' "Input is hidden. Do not paste this key into ChatGPT."
IFS= read -r -s -p "Runtime API key: " supplied </dev/tty
printf '\n'
if [[ $(printf '%s' "$supplied" | wc -c) -lt 32 || "$supplied" != sk-* ]]; then
  unset supplied
  printf '%s\n' "Key format looks invalid; no changes made." >&2
  exit 2
fi

# A regular pre-created 0600 file is expected; bash builtin redirects avoid
# passing the token via argv/env to another process. It is never emitted.
printf '%s' "$supplied" > "$target"
unset supplied
chmod 600 "$target"
if [[ "$(id -un)" == "root" ]]; then
  chown haru:haru "$target"
fi

printf '%s\n' "Haru Life Events runtime key installed to private config (600)."
printf '%s\n' "NO services started; NO subscriptions or events enabled."
