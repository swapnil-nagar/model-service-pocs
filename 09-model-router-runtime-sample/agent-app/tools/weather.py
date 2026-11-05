import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from azure_functions_agents import tool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

logger = logging.getLogger("poc09.weather")
SOURCE = "US National Weather Service"
BASE_URL = "https://api.weather.gov"
HEADERS = {
    "User-Agent": "HostedSkillsPOC09/0.1 (https://github.com/swapnil-nagar/model-service-pocs)",
    "Accept": "application/geo+json",
}


class WeatherQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: FiniteFloat = Field(ge=-90, le=90, strict=True, description="WGS84 latitude in decimal degrees.")
    longitude: FiniteFloat = Field(ge=-180, le=180, strict=True, description="WGS84 longitude in decimal degrees.")


class Grid(BaseModel):
    gridId: str = Field(pattern=r"^[A-Z]{3}$")
    gridX: int = Field(ge=0)
    gridY: int = Field(ge=0)


class PointResponse(BaseModel):
    properties: Grid


class StationProperties(BaseModel):
    stationIdentifier: str = Field(pattern=r"^[A-Za-z0-9]{3,12}$")
    name: str = Field(min_length=1, max_length=200)


class Geometry(BaseModel):
    coordinates: tuple[FiniteFloat, FiniteFloat]


class Station(BaseModel):
    properties: StationProperties
    geometry: Geometry


class StationsResponse(BaseModel):
    features: list[Station]


class Temperature(BaseModel):
    unitCode: Literal["wmoUnit:degC"]
    value: FiniteFloat | None


class ObservationProperties(BaseModel):
    timestamp: AwareDatetime
    temperature: Temperature
    textDescription: str | None = Field(default=None, max_length=200)


class ObservationResponse(BaseModel):
    properties: ObservationProperties


def _failure(status: str, code: str, message: str) -> dict[str, object]:
    logger.warning("poc09_weather_result status=%s code=%s", status, code)
    return {"status": status, "source": SOURCE, "error": {"code": code, "message": message}}


async def read_weather(
    client: httpx.AsyncClient, query: WeatherQuery, *, now: datetime | None = None,
) -> dict[str, object]:
    try:
        async with asyncio.timeout(20):
            point_response = await client.get(
                f"{BASE_URL}/points/{query.latitude:.4f},{query.longitude:.4f}", headers=HEADERS,
            )
            if point_response.status_code in {400, 404}:
                return _failure(
                    "unavailable", "outside_coverage",
                    "NWS cannot provide weather at these coordinates; its coverage is limited to supported US areas.",
                )
            point_response.raise_for_status()
            grid = PointResponse.model_validate_json(point_response.content).properties
            station_response = await client.get(
                f"{BASE_URL}/gridpoints/{grid.gridId}/{grid.gridX},{grid.gridY}/stations", headers=HEADERS,
            )
            station_response.raise_for_status()
            stations = StationsResponse.model_validate_json(station_response.content).features
            if not stations:
                return _failure("unavailable", "no_station", "NWS returned no nearby observation station.")
            station = stations[0]
            station_id = station.properties.stationIdentifier
            observation_url = f"{BASE_URL}/stations/{station_id}/observations/latest"
            response = await client.get(observation_url, params={"require_qc": "true"}, headers=HEADERS)
            if response.status_code == 404:
                return _failure("unavailable", "no_observation", "The nearby station has no available observation.")
            response.raise_for_status()
            observation = ObservationResponse.model_validate_json(response.content).properties
            age = (now or datetime.now(UTC)) - observation.timestamp
            if age > timedelta(hours=2):
                return _failure(
                    "unavailable", "stale_observation",
                    "The nearby station's latest observation is over two hours old; current weather is unavailable.",
                )
            if age < -timedelta(minutes=5):
                return _failure("error", "invalid_observation_time", "NWS returned an observation timestamp in the future.")
            if observation.temperature.value is None:
                return _failure("unavailable", "missing_temperature", "The nearby station did not report a temperature.")
            return {
                "status": "ok",
                "source": SOURCE,
                "source_url": observation_url,
                "requested_location": {"latitude": query.latitude, "longitude": query.longitude},
                "station": {
                    "id": station_id, "name": station.properties.name,
                    "latitude": station.geometry.coordinates[1],
                    "longitude": station.geometry.coordinates[0],
                },
                "observed_at": observation.timestamp.isoformat(),
                "temperature_c": round(observation.temperature.value, 1),
                "conditions": observation.textDescription,
            }
    except (httpx.TimeoutException, TimeoutError):
        return _failure("error", "provider_timeout", "The NWS request timed out; no weather reading is available.")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return _failure("unavailable", "provider_rate_limited", "NWS is rate limiting requests; try again later.")
        if exc.response.status_code >= 500:
            return _failure("unavailable", "provider_unavailable", "NWS is temporarily unavailable; try again later.")
        return _failure("error", "provider_http_error", f"NWS rejected the weather request (HTTP {exc.response.status_code}).")
    except httpx.RequestError:
        return _failure("error", "provider_connection_error", "NWS could not be reached; no weather reading is available.")
    except ValidationError:
        return _failure("error", "invalid_provider_response", "NWS returned invalid or incomplete weather data.")


@tool(schema=WeatherQuery)
async def get_weather(params: WeatherQuery) -> str:
    """Read a recent NWS station observation near coordinates; US coverage only, not a city geocoder."""
    async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False) as client:
        result = await read_weather(client, params)
    logger.info("poc09_tool_executed tool=get_weather status=%s", result["status"])
    return json.dumps(result, allow_nan=False)
