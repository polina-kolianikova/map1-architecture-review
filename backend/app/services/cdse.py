from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
from time import sleep
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import requests

if TYPE_CHECKING:
    from app.core.config import Settings


_TOKEN_CACHE_REFRESH_MARGIN = timedelta(seconds=300)
_TOKEN_CACHE: dict[tuple[str, str, str, str, str], tuple[str, datetime]] = {}


@dataclass(frozen=True)
class SentinelProduct:
    product_id: str
    name: str
    acquisition_date: date
    cloud_coverage: float | None
    processing_level: str | None


class CdseClient:
    def __init__(self, settings: "Settings", timeout: int = 60) -> None:
        self.settings = settings
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False

    def get_access_token(self) -> str:
        data = self._token_request_data()
        cache_key = self._token_cache_key(data)
        now = datetime.now(UTC)
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is not None:
            token, expires_at = cached
            if expires_at - _TOKEN_CACHE_REFRESH_MARGIN > now:
                return token

        response = self.session.post(
            self.settings.cdse_auth_url,
            data=data,
            timeout=self.timeout,
            proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("CDSE token response did not include access_token")
        _TOKEN_CACHE[cache_key] = (token, _token_expires_at(now, payload))
        return token

    def _token_cache_key(self, data: dict[str, str]) -> tuple[str, str, str, str, str]:
        credential_material = "\0".join(
            [
                data.get("client_secret", ""),
                data.get("password", ""),
                data.get("totp", ""),
            ]
        )
        credential_hash = hashlib.sha256(credential_material.encode()).hexdigest()
        return (
            self.settings.cdse_auth_url,
            data.get("grant_type", ""),
            data.get("client_id", ""),
            data.get("username", ""),
            credential_hash,
        )

    def _token_request_data(self) -> dict[str, str]:
        grant_type = self._grant_type()
        if grant_type == "client_credentials":
            _require_setting(self.settings.cdse_client_id, "CDSE_CLIENT_ID")
            _require_setting(self.settings.cdse_client_secret, "CDSE_CLIENT_SECRET")
            return {
                "grant_type": "client_credentials",
                "client_id": self.settings.cdse_client_id,
                "client_secret": self.settings.cdse_client_secret,
            }
        if grant_type != "password":
            raise RuntimeError("CDSE_AUTH_GRANT_TYPE must be auto, password, or client_credentials")

        _require_setting(self.settings.cdse_client_id, "CDSE_CLIENT_ID")
        _require_setting(self.settings.cdse_username, "CDSE_USERNAME")
        _require_setting(self.settings.cdse_password, "CDSE_PASSWORD")
        data = {
            "grant_type": "password",
            "client_id": self.settings.cdse_client_id,
            "username": self.settings.cdse_username,
            "password": self.settings.cdse_password,
        }
        if self.settings.cdse_client_secret:
            data["client_secret"] = self.settings.cdse_client_secret
        if self.settings.cdse_totp:
            data["totp"] = self.settings.cdse_totp
        return data

    def _grant_type(self) -> str:
        configured = self.settings.cdse_auth_grant_type.strip().lower()
        if configured != "auto":
            return configured
        if self.settings.cdse_client_secret and self.settings.cdse_client_id != "cdse-public":
            return "client_credentials"
        return "password"

    def search_sentinel2_products(
        self,
        token: str,
        start: datetime,
        end: datetime,
        max_cloud_coverage: int = 40,
        limit: int = 100,
        area_wkt: str | None = None,
    ) -> list[SentinelProduct]:
        polygon = area_wkt or _wkt_polygon(_test_region_coordinates())
        filters = (
            "Collection/Name eq 'SENTINEL-2' "
            f"and OData.CSC.Intersects(area=geography'SRID=4326;{polygon}') "
            "and Attributes/OData.CSC.StringAttribute/any("
            "att:att/Name eq 'productType' "
            "and att/OData.CSC.StringAttribute/Value eq 'S2MSI2A') "
            "and Attributes/OData.CSC.DoubleAttribute/any("
            "att:att/Name eq 'cloudCover' "
            f"and att/OData.CSC.DoubleAttribute/Value lt {max_cloud_coverage}) "
            f"and ContentDate/Start gt {start.isoformat(timespec='milliseconds').replace('+00:00', 'Z')} "
            f"and ContentDate/Start lt {end.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}"
        )
        products = []
        skip = 0

        while True:
            query = urlencode(
                {
                    "$filter": filters,
                    "$expand": "Attributes",
                    "$orderby": "ContentDate/Start desc",
                    "$top": str(limit),
                    "$skip": str(skip),
                }
            )
            url = f"{self.settings.cdse_catalogue_url}/Products?{query}"
            response = self.session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
                proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
            )
            response.raise_for_status()
            page = response.json().get("value", [])
            for item in page:
                attrs = _attributes_by_name(item.get("Attributes", []))
                acquisition = _parse_datetime(item["ContentDate"]["Start"]).date()
                products.append(
                    SentinelProduct(
                        product_id=item["Id"],
                        name=item["Name"],
                        acquisition_date=acquisition,
                        cloud_coverage=_as_float(attrs.get("cloudCover")),
                        processing_level=attrs.get("productType") or "S2MSI2A",
                    )
                )
            if len(page) < limit:
                break
            skip += len(page)
        return products

    def request_ndvi_statistics(
        self,
        token: str,
        start: datetime,
        end: datetime,
        max_cloud_coverage: int = 40,
        geometry: dict[str, Any] | None = None,
        resolution: float = 0.0001,
    ) -> dict[str, Any]:
        geometry = geometry or {
            "type": "Polygon",
            "coordinates": [_test_region_coordinates()],
        }
        payload = self._ndvi_statistics_payload(
            start=start,
            end=end,
            max_cloud_coverage=max_cloud_coverage,
            geometry=geometry,
            resolution=resolution,
        )
        response = self._post_statistics(payload, token)
        response.raise_for_status()
        return response.json()

    def check_ndvi_statistics_endpoint(
        self,
        token: str,
        *,
        start: datetime,
        end: datetime,
        max_cloud_coverage: int = 80,
        resolution: float = 0.0001,
    ) -> requests.Response:
        payload = self._ndvi_statistics_payload(
            start=start,
            end=end,
            max_cloud_coverage=max_cloud_coverage,
            geometry={
                "type": "Polygon",
                "coordinates": [_test_region_coordinates()],
            },
            resolution=resolution,
        )
        return self._post_statistics(payload, token)

    def _ndvi_statistics_payload(
        self,
        *,
        start: datetime,
        end: datetime,
        max_cloud_coverage: int,
        geometry: dict[str, Any],
        resolution: float,
    ) -> dict[str, Any]:
        payload = {
            "input": {
                "bounds": {
                    "geometry": geometry,
                    "properties": {"crs": "http://www.opengis.net/def/crs/EPSG/0/4326"},
                },
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "timeRange": {
                                "from": _iso_z(start),
                                "to": _iso_z(end),
                            },
                            "maxCloudCoverage": max_cloud_coverage,
                            "mosaickingOrder": "leastCC",
                        },
                    }
                ],
            },
            "aggregation": {
                "timeRange": {
                    "from": _iso_z(start),
                    "to": _iso_z(end),
                },
                "aggregationInterval": {"of": "P1D"},
                "resx": resolution,
                "resy": resolution,
                "evalscript": NDVI_EVALSCRIPT,
            },
            "calculations": {
                "ndvi": {
                    "statistics": {
                        "default": {
                            "percentiles": {"k": [50]},
                        }
                    }
                }
            },
        }
        return payload

    def create_ndvi_batch_statistics_request(
        self,
        token: str,
        *,
        input_features_uri: str,
        output_uri: str,
        start: datetime,
        end: datetime,
        max_cloud_coverage: int = 40,
        resolution: float = 0.0001,
        description: str | None = None,
    ) -> dict[str, Any]:
        input_s3 = self._batch_statistics_s3_payload(input_features_uri)
        output_s3 = self._batch_statistics_s3_payload(output_uri)
        payload = {
            "description": description or "Map 1 Turkey Stage 4 NDVI",
            "input": {
                "features": {"s3": input_s3},
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "timeRange": {
                                "from": _iso_z(start),
                                "to": _iso_z(end),
                            },
                            "maxCloudCoverage": max_cloud_coverage,
                            "mosaickingOrder": "leastCC",
                        },
                    }
                ],
            },
            "aggregation": {
                "timeRange": {
                    "from": _iso_z(start),
                    "to": _iso_z(end),
                },
                "aggregationInterval": {"of": "P1D"},
                "resx": resolution,
                "resy": resolution,
                "evalscript": NDVI_EVALSCRIPT,
            },
            "calculations": {
                "ndvi": {
                    "statistics": {
                        "default": {
                            "percentiles": {"k": [50]},
                        }
                    }
                }
            },
            "output": {"s3": output_s3},
        }
        response = self.session.post(
            self.settings.sentinel_hub_batch_statistics_url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
            proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
        )
        response.raise_for_status()
        return response.json()

    def _batch_statistics_s3_payload(self, url: str) -> dict[str, str]:
        s3 = {"url": url}
        iam_role_arn = _setting_value(
            self.settings,
            "stage4_batch_statistical_s3_iam_role_arn",
        )
        access_key = _setting_value(
            self.settings,
            "stage4_batch_statistical_s3_access_key",
        )
        bucket_key_pair = _setting_value(
            self.settings,
            "stage4_batch_statistical_s3_secret_access_key",
        )
        region = _setting_value(self.settings, "stage4_batch_statistical_s3_region")
        if region:
            s3["region"] = region

        if iam_role_arn:
            s3["iamRoleARN"] = iam_role_arn
            return s3
        if access_key or bucket_key_pair:
            if not access_key or not bucket_key_pair:
                raise RuntimeError(
                    "STAGE4_BATCH_STATISTICAL_S3_ACCESS_KEY and "
                    "STAGE4_BATCH_STATISTICAL_S3_SECRET_ACCESS_KEY must be set together"
                )
            s3["accessKey"] = access_key
            s3["secretAccessKey"] = bucket_key_pair
        return s3

    def start_batch_statistics_request(self, token: str, request_id: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.settings.sentinel_hub_batch_statistics_url}/{request_id}/start",
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
            proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {"id": request_id, "status": "STARTED"}

    def get_batch_statistics_request(self, token: str, request_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.settings.sentinel_hub_batch_statistics_url}/{request_id}/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
            proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
        )
        response.raise_for_status()
        return response.json()

    def request_ndvi_raster(
        self,
        token: str,
        *,
        start: datetime,
        end: datetime,
        bbox: list[float],
        crs_epsg: int,
        width: int,
        height: int,
        max_cloud_coverage: int = 40,
    ) -> bytes:
        payload = {
            "input": {
                "bounds": {
                    "bbox": bbox,
                    "properties": {
                        "crs": f"http://www.opengis.net/def/crs/EPSG/0/{int(crs_epsg)}",
                    },
                },
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "timeRange": {
                                "from": _iso_z(start),
                                "to": _iso_z(end),
                            },
                            "maxCloudCoverage": max_cloud_coverage,
                            "mosaickingOrder": "leastCC",
                        },
                    }
                ],
            },
            "output": {
                "width": int(width),
                "height": int(height),
                "responses": [
                    {
                        "identifier": "default",
                        "format": {"type": "image/tiff"},
                    }
                ],
            },
            "evalscript": NDVI_RASTER_EVALSCRIPT,
        }
        response = self._post_process(payload, token)
        response.raise_for_status()
        return response.content

    def _post_statistics(self, payload: dict[str, Any], token: str) -> requests.Response:
        for attempt in range(4):
            response = self.session.post(
                self.settings.sentinel_hub_statistics_url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
                proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
            )
            if response.status_code != 429 or attempt == 3:
                return response
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait_seconds = min(int(retry_after), self.timeout)
            else:
                wait_seconds = 2 ** attempt
            sleep(wait_seconds)
        return response

    def _post_process(self, payload: dict[str, Any], token: str) -> requests.Response:
        for attempt in range(4):
            response = self.session.post(
                self.settings.sentinel_hub_process_url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
                proxies=_proxies(_setting_value(self.settings, "cdse_proxy_url")),
            )
            if response.status_code != 429 or attempt == 3:
                return response
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait_seconds = min(int(retry_after), self.timeout)
            else:
                wait_seconds = 2 ** attempt
            sleep(wait_seconds)
        return response


def _token_expires_at(now: datetime, payload: dict[str, Any]) -> datetime:
    try:
        expires_in = int(payload.get("expires_in") or 600)
    except (TypeError, ValueError):
        expires_in = 600
    return now + timedelta(seconds=max(expires_in, 60))


NDVI_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: [
      { id: "ndvi", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}

function evaluatePixel(sample) {
  var validClass = sample.SCL === 4 || sample.SCL === 5 || sample.SCL === 6;
  var validPixel = sample.dataMask === 1 && validClass;
  if (!validPixel) {
    return {
      ndvi: [0],
      dataMask: [0]
    };
  }
  var denom = sample.B08 + sample.B04;
  var ndvi = denom === 0 ? 0 : (sample.B08 - sample.B04) / denom;
  return {
    ndvi: [ndvi],
    dataMask: [1]
  };
}
"""


NDVI_RASTER_EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: { bands: 2, sampleType: "FLOAT32" }
  };
}

function evaluatePixel(sample) {
  var validClass = sample.SCL === 4 || sample.SCL === 5 || sample.SCL === 6;
  var denom = sample.B08 + sample.B04;
  var validPixel = sample.dataMask === 1 && validClass && denom !== 0;
  var ndvi = validPixel ? (sample.B08 - sample.B04) / denom : 0;
  return [ndvi, validPixel ? 1 : 0];
}
"""


def _attributes_by_name(attributes: list[dict[str, Any]]) -> dict[str, Any]:
    values = {}
    for item in attributes:
        name = item.get("Name")
        if not name:
            continue
        value = item.get("Value")
        if value is None:
            for key, candidate in item.items():
                if key.endswith("Attribute") and isinstance(candidate, dict):
                    value = candidate.get("Value")
                    break
        values[name] = value
    return values


def _require_setting(value: str, name: str) -> None:
    if not value:
        raise RuntimeError(f"{name} is required for CDSE authentication")


def _proxies(proxy_url: str) -> dict[str, str] | None:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _setting_value(settings: "Settings", name: str) -> str:
    return getattr(settings, name, "")


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso_z(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _wkt_polygon(coordinates: list[list[float]]) -> str:
    pairs = ",".join(f"{lon:.4f} {lat:.4f}" for lon, lat in coordinates)
    return f"POLYGON(({pairs}))"


def _test_region_coordinates() -> list[list[float]]:
    from app.services.grid import TEST_REGION_COORDINATES

    return TEST_REGION_COORDINATES
