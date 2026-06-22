"""
movebank_client.py — a thin, honest wrapper over the Movebank REST API.

Surfaces (per the official movebank-api-doc, verified June 2026):
  - direct-read  : CSV; Basic-auth OR token-auth; supports the license-md5 flow
  - public/json  : JSON; public studies only, no auth
  - json-auth    : JSON; private studies, Basic auth

Design notes that matter:
  * Movebank rate-limits to 1 concurrent request per IP / 20 total. This client
    is deliberately synchronous and serializes calls. Do NOT wrap it in a thread
    pool and hammer it.
  * GPS sensor: sensor_type=gps (json) / sensor_type_id=653 (direct-read).
  * Reduction profiles run server-side BEFORE transfer. EURING_02 (>=50 km between
    fixes) is effectively a migration-extraction filter and your best friend for
    keeping payloads — and LLM context — small.
  * Internal ids (individual_id, tag_id) are NOT stable across re-imports. Prefer
    individual_local_identifier as the external key, exactly as Movebank advises.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import httpx

BASE = "https://www.movebank.org/movebank/service"
GPS_SENSOR_TYPE_ID = 653
REDUCTION_PROFILES = {
    "EURING_01": "Daily events (>=24h between fixes)",
    "EURING_02": "Long-distance (>=50 km between fixes) — migration extractor",
    "EURING_03": "Last 30 days only",
    "EURING_04": "0.25-degree gridded (>=0.25 deg movement)",
}


def _ms(dt: datetime) -> int:
    """UTC datetime -> epoch milliseconds (Movebank's JSON timestamp unit)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _csv_to_rows(text: str) -> list[dict[str, str]]:
    import csv
    import io
    return list(csv.DictReader(io.StringIO(text)))


@dataclass
class MovebankClient:
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    timeout: float = 60.0
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        auth = None
        if self.username and self.password and not self.api_token:
            auth = (self.username, self.password)
        # A Session-equivalent so the license-md5 cookie handshake works.
        self._client = httpx.Client(timeout=self.timeout, auth=auth, follow_redirects=True)

    # ---- auth -------------------------------------------------------------

    def request_token(self) -> str:
        """Exchange Basic credentials for an api-token (preferred for repeat use)."""
        if not (self.username and self.password):
            raise ValueError("username/password required to request a token")
        r = self._client.get(f"{BASE}/direct-read", params={"service": "request-token"})
        r.raise_for_status()
        # Response is a small JSON or plain token depending on server version.
        text = r.text.strip()
        token = text
        try:
            import json
            obj = json.loads(text)
            token = obj.get("api-token") or obj.get("token") or text
        except Exception:
            pass
        self.api_token = token
        return token

    def _auth_params(self) -> dict[str, str]:
        return {"api-token": self.api_token} if self.api_token else {}

    # ---- low-level direct-read (CSV) with license handling ----------------

    def _direct_read_csv(self, params: dict, accept_license: bool = True) -> list[dict]:
        p = {**self._auth_params(), **params}
        r = self._client.get(f"{BASE}/direct-read", params=p)
        r.raise_for_status()

        looks_like_license = (
            r.headers.get("accept-license", "").lower() == "true"
            or "License Terms:" in r.text
            or "License Information valid" in r.text
        )
        if looks_like_license:
            if not accept_license:
                raise PermissionError("Study requires license acceptance (accept_license=False).")
            md5 = hashlib.md5(r.text.encode("utf-8")).hexdigest()
            r = self._client.get(f"{BASE}/direct-read", params={**p, "license-md5": md5})
            r.raise_for_status()
            if "No data available" in r.text:
                raise PermissionError("No download permission for this study.")
        if "No data available" in r.text:
            raise PermissionError("No data available — likely no download permission.")
        return _csv_to_rows(r.text)

    # ---- study / individual metadata --------------------------------------

    def list_studies(self, only_downloadable: bool = True, attributes: Iterable[str] | None = None) -> list[dict]:
        attrs = list(attributes or [
            "id", "name", "license_type", "number_of_deployed_locations",
            "taxon_ids", "sensor_type_ids", "timestamp_first_deployed_location",
            "timestamp_last_deployed_location", "i_have_download_access",
            "main_location_lat", "main_location_long",
        ])
        params = {"entity_type": "study", "attributes": ",".join(attrs)}
        if only_downloadable:
            params["i_have_download_access"] = "true"
        return self._direct_read_csv(params)

    def get_study(self, study_id: int) -> dict:
        rows = self._direct_read_csv({"entity_type": "study", "study_id": study_id})
        return rows[0] if rows else {}

    def list_individuals(self, study_id: int) -> list[dict]:
        return self._direct_read_csv({"entity_type": "individual", "study_id": study_id})

    # ---- event data -------------------------------------------------------

    def get_tracks_json_public(
        self,
        study_id: int,
        individual_local_identifiers: list[str] | None = None,
        sensor_type: str = "gps",
        reduction_profile: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        max_events_per_individual: int | None = None,
    ) -> dict:
        """
        Public JSON endpoint. Returns Movebank's native nested shape:
            {"individuals":[{"individual_local_identifier":..., "locations":[
                {"timestamp": <ms>, "location_long":..., "location_lat":...}, ...]}]}
        No auth — only works for fully public studies.
        """
        params: list[tuple[str, str]] = [
            ("study_id", str(study_id)),
            ("sensor_type", sensor_type),
        ]
        for ind in (individual_local_identifiers or []):
            params.append(("individual_local_identifiers", ind))  # repeated key
        if reduction_profile:
            params.append(("event_reduction_profile", reduction_profile))
        if start:
            params.append(("timestamp_start", str(_ms(start))))
        if end:
            params.append(("timestamp_end", str(_ms(end))))
        if max_events_per_individual:
            params.append(("max_events_per_individual", str(max_events_per_individual)))

        r = self._client.get(f"{BASE}/public/json", params=params)
        r.raise_for_status()
        text = r.text.strip()
        if not text or text.startswith("<"):
            # HTML usually means the study isn't public or needs license/auth.
            return {"individuals": []}
        import json
        return json.loads(text)

    def get_tracks_csv(
        self,
        study_id: int,
        individual_local_identifiers: list[str] | None = None,
        sensor_type_id: int = GPS_SENSOR_TYPE_ID,
        reduction_profile: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        attributes: Iterable[str] | None = None,
        accept_license: bool = True,
    ) -> list[dict]:
        """direct-read CSV path. Use this for private/auth studies or richer attributes."""
        attrs = list(attributes or [
            "individual_local_identifier", "tag_local_identifier", "timestamp",
            "location_long", "location_lat", "visible",
            "individual_taxon_canonical_name",
        ])
        params: dict = {
            "entity_type": "event",
            "study_id": study_id,
            "sensor_type_id": sensor_type_id,
            "attributes": ",".join(attrs),
        }
        if individual_local_identifiers:
            params["individual_local_identifier"] = ",".join(individual_local_identifiers)
        if reduction_profile:
            params["event_reduction_profile"] = reduction_profile
        if start:
            params["timestamp_start"] = start.strftime("%Y%m%d%H%M%S") + "000"
        if end:
            params["timestamp_end"] = end.strftime("%Y%m%d%H%M%S") + "000"
        return self._direct_read_csv(params, accept_license=accept_license)

    def close(self) -> None:
        self._client.close()
