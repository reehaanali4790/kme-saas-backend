"""Server-side client for VesselAPI vessel search and tracking."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from config.settings import settings


class VesselAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _first_dict(payload: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload if isinstance(payload, dict) else {}


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return error.get("message") or error.get("code") or response.text
        return str(error)
    except Exception:
        return response.text or f"VesselAPI returned HTTP {response.status_code}"


class VesselAPIClient:
    def __init__(self) -> None:
        if not settings.VESSEL_API_KEY:
            raise VesselAPIError(
                "VesselAPI is not configured. Set VESSEL_API_KEY in backend/.env.",
                status_code=503,
            )
        self.base_url = settings.VESSEL_API_BASE_URL.rstrip("/")
        self.headers = {"Authorization": f"Bearer {settings.VESSEL_API_KEY}"}
        self.timeout = settings.VESSEL_API_TIMEOUT_SECONDS

    def _get(self, path: str, params: Optional[dict] = None) -> tuple[dict, httpx.Headers]:
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                headers=self.headers,
                params=params,
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise VesselAPIError("VesselAPI request timed out.", 504) from exc
        except httpx.HTTPError as exc:
            raise VesselAPIError(f"Could not connect to VesselAPI: {exc}", 502) from exc

        if response.status_code >= 400:
            status_map = {400: 400, 401: 503, 402: 402, 403: 403, 404: 404, 429: 429}
            raise VesselAPIError(
                _error_message(response),
                status_map.get(response.status_code, 502),
            )
        return response.json(), response.headers

    def search(self, name: str) -> list[dict]:
        payload, _ = self._get(
            "/search/vessels",
            {"filter.name": name, "pagination.limit": 10},
        )
        return payload.get("vessels") or []

    def details(self, identifier: str, id_type: str) -> dict:
        payload, _ = self._get(
            f"/vessel/{identifier}",
            {"filter.idType": id_type},
        )
        return _first_dict(payload, "vessel")

    def position(self, identifier: str, id_type: str) -> tuple[dict, dict]:
        payload, headers = self._get(
            f"/vessel/{identifier}/position",
            {"filter.idType": id_type},
        )
        return (
            _first_dict(payload, "vesselPosition", "vessel_position", "position"),
            {
                "remaining": headers.get("X-RateLimit-Remaining"),
                "source": headers.get("X-Data-Source", "terrestrial"),
            },
        )

    def eta(self, identifier: str, id_type: str) -> dict:
        try:
            payload, _ = self._get(
                f"/vessel/{identifier}/eta",
                {"filter.idType": id_type},
            )
            return _first_dict(payload, "vesselETA", "vesselEta", "vessel_eta", "eta")
        except VesselAPIError as exc:
            if exc.status_code == 404:
                return {}
            raise


def _exact_name_match(query: str, vessel: dict) -> bool:
    return str(vessel.get("name") or "").strip().casefold() == query.strip().casefold()


def track_vessel_by_name(name: str) -> dict:
    client = VesselAPIClient()
    query = name.strip()
    if query.isdigit() and len(query) in (7, 9):
        id_type = "mmsi" if len(query) == 9 else "imo"
        vessel = client.details(query, id_type)
        matches = [vessel]
    else:
        matches = client.search(query)
        if not matches:
            raise VesselAPIError(f'No vessel found for "{name}".', 404)
        exact = [row for row in matches if _exact_name_match(query, row)]
        vessel = exact[0] if exact else matches[0]

    mmsi = vessel.get("mmsi")
    imo = vessel.get("imo")
    identifier = str(mmsi or imo or "")
    id_type = "mmsi" if mmsi else "imo"
    if not identifier:
        raise VesselAPIError("Matched vessel has no MMSI or IMO identifier.", 422)

    position, quota = client.position(identifier, id_type)
    eta = client.eta(identifier, id_type)

    from integrations.vessel_api.aisstream_service import is_karachi_destination

    dest = (
        eta.get("destination")
        or eta.get("reported_destination")
        or vessel.get("destination")
        or ""
    )
    karachi_dest = is_karachi_destination(str(dest))

    return {
        "query": name,
        "match_count": len(matches),
        "matches": matches,
        "vessel": vessel,
        "position": position,
        "eta": eta,
        "quota": quota,
        "provider": "vesselapi",
        "karachi_destination": karachi_dest,
        "karachi_relevant": karachi_dest,
        "coverage": "global",
    }


async def track_vessel_with_fallback(name: str) -> dict:
    """Try VesselAPI first; on not-found or failure, fall back to AISStream live AIS."""
    import logging

    from integrations.vessel_api.aisstream_service import AISStreamError, track_via_aisstream

    logger = logging.getLogger("uvicorn")
    query = (name or "").strip()
    vesselapi_error: Optional[Exception] = None

    try:
        return track_vessel_by_name(query)
    except VesselAPIError as exc:
        vesselapi_error = exc
        logger.info(
            "[VesselTracker] VesselAPI miss/fail for %r (%s) — trying AISStream",
            query,
            exc,
        )
    except Exception as exc:
        vesselapi_error = exc
        logger.warning(
            "[VesselTracker] VesselAPI unexpected error for %r: %s — trying AISStream",
            query,
            exc,
        )

    try:
        result = await track_via_aisstream(query)
        result["fallback_from"] = "vesselapi"
        result["vesselapi_error"] = str(vesselapi_error) if vesselapi_error else None
        return result
    except AISStreamError as stream_exc:
        # Prefer the more specific AISStream 404 if VesselAPI also missed;
        # otherwise surface both failures.
        if isinstance(vesselapi_error, VesselAPIError) and vesselapi_error.status_code == 404:
            raise stream_exc from vesselapi_error
        detail = str(stream_exc)
        if vesselapi_error:
            detail = f"VesselAPI: {vesselapi_error} | AISStream: {stream_exc}"
        raise VesselAPIError(detail, getattr(stream_exc, "status_code", 502)) from stream_exc
