# Third-party notices

haru-life-events installs these packages (none are vendored in this repository):

| Package | Used for | License |
|---|---|---|
| [cryptography](https://github.com/pyca/cryptography) | subscription vault, keys | Apache-2.0 OR BSD-3-Clause |
| [uvicorn](https://github.com/encode/uvicorn) (`server` extra) | the loopback Events MCP backend | BSD-3-Clause |
| [pytest](https://github.com/pytest-dev/pytest) (`test` extra) | tests | MIT |
| [httpx](https://github.com/encode/httpx) (`test` extra) | tests | BSD-3-Clause |

## External services

- **Reverse geocoding (location only).** Place labels come from the
  [HeartRails Geo API](https://geoapi.heartrails.com/), with the reverse geocoder and
  municipality table of the [Geospatial Information Authority of Japan (GSI)](https://maps.gsi.go.jp/)
  as fallback. Coordinates are sent only when a live-location share triggers a lookup, and results
  are cached on a roughly 100 m key. Check each service's terms before heavy use.
- **OpenAI.** Subscriptions and events follow
  [MCP Events](https://developers.openai.com/plugins/build/mcp-events). The private Tunnel uses
  OpenAI's `tunnel-client`, which is not included here.
- **Vitals.** Vitals changes come from [pulse-relay](https://github.com/kohaku4yz/pulse-relay) (MIT),
  which receives data from the [PulsHealth](https://github.com/PulsHealth/pulshealth) iOS app
  (Apache-2.0). No PulsHealth code is included. The PulsHealth name belongs to its project.
