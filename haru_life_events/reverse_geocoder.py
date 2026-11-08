"""Japan reverse geocoder: HeartRails first, then Japan GSI as fallback.

Best-effort resolver with a roughly 100 m cache key.
No GPS is stored by this geocoder. External lookups receive coordinates
only when location sharing explicitly triggered.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

HEARTRAILS_REVERSE_GEOCODER_URL = "https://geoapi.heartrails.com/api/json"
HEARTRAILS_GEOCODER_TIMEOUT_SEC = 1.2
GSI_REVERSE_GEOCODER_URL = "https://mreversegeocoder.gsi.go.jp/reverse-geocoder/LonLatToAddress"
GSI_MUNICIPALITY_URL = "https://maps.gsi.go.jp/js/muni.js"
GSI_GEOCODER_TIMEOUT_SEC = 0.8
LOCATION_CACHE_LIMIT = 256

@dataclass(frozen=True, slots=True)
class ResolvedPlace:
    label: str
    source: str


class JapanReverseGeocoder:
    """Best-effort Japan reverse geocoding for user-turn location context."""

    def __init__(
        self,
        *,
        heartrails_timeout: float = HEARTRAILS_GEOCODER_TIMEOUT_SEC,
        gsi_timeout: float = GSI_GEOCODER_TIMEOUT_SEC,
    ) -> None:
        self.heartrails_timeout = heartrails_timeout
        self.gsi_timeout = gsi_timeout
        self._cache: dict[tuple[int, int], ResolvedPlace] = {}
        self._municipalities: dict[str, tuple[str, str]] | None = None

    @staticmethod
    def _cache_key(latitude: float, longitude: float) -> tuple[int, int]:
        # Approx. 100 m cells around Japan; enough for conversational place labels.
        return (int(round(latitude * 1000)), int(round(longitude * 1000)))

    @staticmethod
    def _in_japan_bounds(latitude: float, longitude: float) -> bool:
        return 20.0 <= latitude <= 46.5 and 122.0 <= longitude <= 154.5

    @staticmethod
    def _parse_municipalities(source: str) -> dict[str, tuple[str, str]]:
        municipalities: dict[str, tuple[str, str]] = {}
        pattern = re.compile(
            r"""GSI\.MUNI_ARRAY\["(?P<code>\d+)"\]\s*=\s*'(?P<body>[^']*)';"""
        )
        for match in pattern.finditer(source):
            fields = match.group("body").split(",", 3)
            if len(fields) != 4:
                continue
            _pref_code, prefecture, _muni_code, municipality = fields
            if prefecture and municipality:
                municipalities[match.group("code")] = (prefecture, municipality)
        return municipalities

    @staticmethod
    def _get_text(url: str, *, timeout: float) -> str:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "haru-life-events/0.1 reverse-geocoder"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def _resolve_heartrails(self, latitude: float, longitude: float) -> ResolvedPlace | None:
        query = urllib.parse.urlencode(
            {
                "method": "searchByGeoLocation",
                "x": f"{longitude:.7f}",
                "y": f"{latitude:.7f}",
            }
        )
        try:
            payload = json.loads(
                self._get_text(
                    f"{HEARTRAILS_REVERSE_GEOCODER_URL}?{query}",
                    timeout=self.heartrails_timeout,
                )
            )
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

        response = payload.get("response") if isinstance(payload, dict) else None
        locations = response.get("location") if isinstance(response, dict) else None
        if not isinstance(locations, list):
            return None

        candidates = [item for item in locations if isinstance(item, dict)]
        candidates.sort(
            key=lambda item: (
                float(item.get("distance"))
                if isinstance(item.get("distance"), (int, float))
                else float("inf")
            )
        )
        for item in candidates:
            town = item.get("town")
            city = item.get("city")
            prefecture = item.get("prefecture")
            parts = [
                part.strip()
                for part in (town, city, prefecture)
                if isinstance(part, str) and part.strip()
            ]
            if parts:
                return ResolvedPlace(
                    label=", ".join(dict.fromkeys(parts)),
                    source="HeartRails Geo API",
                )
        return None

    def _municipality(self, code: str) -> tuple[str, str] | None:
        if self._municipalities is None:
            try:
                source = self._get_text(
                    GSI_MUNICIPALITY_URL,
                    timeout=self.gsi_timeout,
                )
                self._municipalities = self._parse_municipalities(source)
            except (OSError, ValueError, UnicodeError):
                self._municipalities = {}
        return self._municipalities.get(code)

    def _resolve_gsi(self, latitude: float, longitude: float) -> ResolvedPlace | None:
        query = urllib.parse.urlencode(
            {"lon": f"{longitude:.7f}", "lat": f"{latitude:.7f}"}
        )
        try:
            payload = json.loads(
                self._get_text(
                    f"{GSI_REVERSE_GEOCODER_URL}?{query}",
                    timeout=self.gsi_timeout,
                )
            )
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            return None

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, dict):
            return None
        town = results.get("lv01Nm")
        code = results.get("muniCd")
        if not isinstance(town, str) or not town.strip():
            return None

        parts = [town.strip()]
        if isinstance(code, str) and code:
            municipality = self._municipality(code)
            if municipality is not None:
                prefecture, city = municipality
                parts.extend([city, prefecture])
        return ResolvedPlace(
            label=", ".join(dict.fromkeys(part for part in parts if part)),
            source="国土地理院",
        )

    def resolve(self, latitude: float, longitude: float) -> ResolvedPlace | None:
        if not self._in_japan_bounds(latitude, longitude):
            return None

        key = self._cache_key(latitude, longitude)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        # HeartRails is usually the faster of the two. GSI stays as a
        # short-timeout fallback because its town/chome result is useful when available.
        place = self._resolve_heartrails(latitude, longitude)
        if place is None:
            place = self._resolve_gsi(latitude, longitude)
        if place is None:
            return None

        if len(self._cache) >= LOCATION_CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = place
        return place
