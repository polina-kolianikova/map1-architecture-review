from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any
from urllib.parse import urlencode, urljoin

from defusedxml import ElementTree as ET
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass(frozen=True)
class EndpointCheck:
    url: str
    status: str
    http_status: int | None
    final_url: str | None
    note: str


@dataclass(frozen=True)
class WmsLayer:
    service_key: str
    title: str
    url: str
    layer_name: str = "0"
    version: str = "1.3.0"
    stage1_required: bool = False


@dataclass(frozen=True)
class OgmFeature:
    source_id: str
    attributes: dict[str, Any]
    geometry: dict[str, Any]


class OgmServiceError(RuntimeError):
    pass


DEFAULT_HEADERS = {
    "User-Agent": "Map1Turkey/1.0",
    "Accept": "application/json,text/xml,image/svg+xml,image/png,*/*",
}
MAX_MESCERE_SVG_BYTES = 2_000_000
MAX_MESCERE_SVG_PATHS = 250


OGM_WMS_LAYERS = [
    WmsLayer(
        "arazi_ortusu",
        "Arazi Ortusu",
        "https://wsapi.ogm.gov.tr/apigateway/ARAZI_ORTUSU_WMS",
    ),
    WmsLayer(
        "mescere",
        "Mescere",
        "https://wsapi.ogm.gov.tr/apigateway/YAYIN_MESCERE_WMS",
        stage1_required=True,
    ),
    WmsLayer(
        "seflik",
        "Isletme Seflikleri",
        "https://wsapi.ogm.gov.tr/apigateway/YAYIN_SEFLIK_WMS",
    ),
    WmsLayer(
        "isletme",
        "Isletme Mudurlukleri",
        "https://wsapi.ogm.gov.tr/apigateway/YAYIN_ISLETME_WMS",
    ),
    WmsLayer(
        "bolge",
        "Bolge Mudurlugu",
        "https://wsapi.ogm.gov.tr/apigateway/INT_BOLGE_WMS",
    ),
    WmsLayer(
        "indeks",
        "Pafta Indeksi",
        "https://wsapi.ogm.gov.tr/apigateway/YAYIN_INDEKS_WMS",
    ),
]


class OgmClientBase:
    def __init__(
        self,
        timeout: int = 45,
        verify_tls: bool = True,
        proxy_url: str = "",
        transport_retries: int = 2,
    ) -> None:
        if transport_retries < 0:
            raise ValueError("transport_retries must not be negative")
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = _build_session(transport_retries=transport_retries)
        if proxy_url:
            self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def get(self, url: str, **kwargs) -> requests.Response:
        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                verify=self.verify_tls,
                headers=DEFAULT_HEADERS,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise OgmServiceError(_short_request_error(exc)) from exc
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise OgmServiceError(_short_response_error(response)) from exc
        return response


class OgmBolmeClient(OgmClientBase):
    def __init__(
        self,
        feature_url: str,
        timeout: int = 45,
        verify_tls: bool = True,
        proxy_url: str = "",
        transport_retries: int = 2,
    ) -> None:
        super().__init__(
            timeout=timeout,
            verify_tls=verify_tls,
            proxy_url=proxy_url,
            transport_retries=transport_retries,
        )
        self.feature_url = feature_url.rstrip("/")

    def query_bbox(
        self,
        bbox: tuple[float, float, float, float],
        limit: int = 1000,
        max_pages: int = 500,
    ) -> list[OgmFeature]:
        min_lon, min_lat, max_lon, max_lat = bbox
        features = []
        seen = set()
        offset = 0

        for _ in range(max_pages):
            response = self.get(
                f"{self.feature_url}/query",
                params={
                    "f": "geojson",
                    "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                    "geometryType": "esriGeometryEnvelope",
                    "inSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "*",
                    "returnGeometry": "true",
                    "outSR": "4326",
                    "orderByFields": "OBJECTID ASC",
                    "resultRecordCount": str(limit),
                    "resultOffset": str(offset),
                },
            )
            payload = _json_payload(response, "OGM feature query")
            if "error" in payload:
                raise OgmServiceError(f"OGM feature query failed: {payload['error']}")

            page = payload.get("features", [])
            if not page:
                break

            added = 0
            for feature in page:
                attributes = feature.get("properties", {})
                object_id = feature.get("id") or attributes.get("OBJECTID")
                geometry = feature.get("geometry")
                if object_id is None or geometry is None:
                    continue
                source_id = str(object_id)
                if source_id in seen:
                    continue
                seen.add(source_id)
                added += 1
                features.append(
                    OgmFeature(
                        source_id=source_id,
                        attributes=attributes,
                        geometry=geometry,
                    )
                )

            if added == 0:
                if payload.get("exceededTransferLimit") or len(page) >= limit:
                    raise OgmServiceError(
                        "OGM feature query returned a full duplicate page; split the bbox"
                    )
                break
            offset += len(page)
            if not payload.get("exceededTransferLimit") and len(page) < limit:
                break
        else:
            raise OgmServiceError(
                "OGM feature query exceeded pagination safety limit; split the bbox"
            )
        return features


class OgmMescereWmsClient(OgmClientBase):
    def __init__(
        self,
        service_url: str,
        timeout: int = 45,
        verify_tls: bool = True,
        proxy_url: str = "",
        transport_retries: int = 2,
    ) -> None:
        super().__init__(
            timeout=timeout,
            verify_tls=verify_tls,
            proxy_url=proxy_url,
            transport_retries=transport_retries,
        )
        self.service_url = service_url

    def query_bbox(
        self,
        bbox: tuple[float, float, float, float],
        width: int = 512,
        height: int = 512,
    ) -> list[OgmFeature]:
        svg = self._get_svg(bbox, width, height)
        paths = _svg_paths(svg)
        if len(paths) > MAX_MESCERE_SVG_PATHS:
            raise OgmServiceError(
                f"OGM mescere WMS SVG returned {len(paths)} paths; split the bbox"
            )

        features = []
        for path in paths:
            pixel_x, pixel_y = _path_centroid_pixel(path, width, height)
            feature = self._feature_info(bbox, pixel_x, pixel_y, width, height)
            if feature is None:
                continue
            attributes, geometry = feature
            if not geometry:
                geometry = _path_to_geojson(path, bbox, width, height)
            object_id = attributes.get("OBJECTID") or attributes.get("objectid") or attributes.get("id")
            if object_id is None:
                continue
            features.append(
                OgmFeature(
                    source_id=str(object_id),
                    attributes=attributes,
                    geometry=geometry,
                )
            )
        return _merge_features_by_source_id(features)

    def _get_svg(self, bbox: tuple[float, float, float, float], width: int, height: int) -> bytes:
        response = self.get(
            self.service_url,
            params={
                "SERVICE": "WMS",
                "VERSION": "1.1.1",
                "REQUEST": "GetMap",
                "LAYERS": "0",
                "STYLES": "",
                "SRS": "EPSG:4326",
                "BBOX": _bbox_param(bbox),
                "WIDTH": str(width),
                "HEIGHT": str(height),
                "FORMAT": "image/svg+xml",
                "TRANSPARENT": "true",
            },
        )
        _ensure_wms_svg_response(response)
        if len(response.content) > MAX_MESCERE_SVG_BYTES:
            raise OgmServiceError(
                f"OGM mescere WMS SVG response exceeded {MAX_MESCERE_SVG_BYTES} bytes; split the bbox"
            )
        return response.content

    def _feature_info(
        self,
        bbox: tuple[float, float, float, float],
        pixel_x: int,
        pixel_y: int,
        width: int,
        height: int,
    ) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
        response = self.get(
            self.service_url,
            params={
                "SERVICE": "WMS",
                "VERSION": "1.1.1",
                "REQUEST": "GetFeatureInfo",
                "LAYERS": "0",
                "QUERY_LAYERS": "0",
                "STYLES": "",
                "SRS": "EPSG:4326",
                "BBOX": _bbox_param(bbox),
                "WIDTH": str(width),
                "HEIGHT": str(height),
                "X": str(max(0, min(width - 1, pixel_x))),
                "Y": str(max(0, min(height - 1, pixel_y))),
                "INFO_FORMAT": "application/geojson",
            },
        )
        _ensure_json_response(response, "OGM feature info")
        features = _json_payload(response, "OGM feature info").get("features", [])
        if not features:
            return None
        feature = features[0]
        geometry = feature.get("geometry")
        return feature.get("properties", {}), geometry


class OgmEndpointInspector(OgmClientBase):
    def __init__(
        self,
        timeout: int = 20,
        verify_tls: bool = True,
        proxy_url: str = "",
        transport_retries: int = 2,
    ) -> None:
        super().__init__(
            timeout=timeout,
            verify_tls=verify_tls,
            proxy_url=proxy_url,
            transport_retries=transport_retries,
        )

    def inspect(self, eharita_url: str, tucbs_orman_wms_url: str | None = None) -> list[EndpointCheck]:
        checks = [self._check(eharita_url, allow_redirects=False)]

        redirect_url = checks[0].final_url
        if redirect_url:
            checks.append(self._check(redirect_url))
            base_url = _base_url(redirect_url)
        else:
            base_url = "https://cbsportal.ogm.gov.tr"

        for path in (
            "/arcgis/rest/services",
            "/server/rest/services",
            "/portal/sharing/rest",
            "/vatandas/",
        ):
            checks.append(self._check(urljoin(base_url, path)))

        if tucbs_orman_wms_url:
            checks.append(
                self._check(
                    f"{tucbs_orman_wms_url}?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0"
                )
            )
        for layer in OGM_WMS_LAYERS:
            checks.append(self._check(wms_capabilities_url(layer.url)))

        return checks

    def _check(self, url: str, allow_redirects: bool = True) -> EndpointCheck:
        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=allow_redirects,
                verify=self.verify_tls,
                headers=DEFAULT_HEADERS,
            )
        except requests.RequestException as exc:
            return EndpointCheck(
                url=url,
                status="unavailable",
                http_status=None,
                final_url=None,
                note=_short_request_error(exc),
            )

        final_url = response.headers.get("Location") if response.is_redirect else response.url
        if response.ok or response.is_redirect:
            status = "reachable"
        else:
            status = "http_error"

        return EndpointCheck(
            url=url,
            status=status,
            http_status=response.status_code,
            final_url=final_url,
            note=_classify_response(response),
        )


def serialize_checks(checks: list[EndpointCheck]) -> dict:
    return {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checks": [check.__dict__ for check in checks],
    }


def _base_url(url: str) -> str:
    parts = url.split("/", 3)
    if len(parts) < 3:
        return url
    return f"{parts[0]}//{parts[2]}"


def _classify_response(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "")
    if response.is_redirect:
        return "redirect"
    if "json" in content_type.lower():
        return "json"
    if "html" in content_type.lower():
        return "html"
    return content_type or "response"


def _build_session(*, transport_retries: int = 2) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    retry = Retry(
        total=transport_retries,
        connect=transport_retries,
        read=transport_retries,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _short_request_error(exc: requests.RequestException) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"


def _short_response_error(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "")
    return (
        f"HTTP {response.status_code}: "
        f"{response.url[:180]} "
        f"content_type={content_type[:80]}"
    )


def _json_payload(response: requests.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("Content-Type", "")
        raise OgmServiceError(f"{label} returned non-JSON content_type={content_type}") from exc
    if not isinstance(payload, dict):
        raise OgmServiceError(f"{label} returned unexpected JSON payload")
    return payload


def _ensure_json_response(response: requests.Response, label: str) -> None:
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" not in content_type and "geojson" not in content_type:
        raise OgmServiceError(f"{label} returned unexpected content_type={content_type or 'unknown'}")


def _ensure_wms_svg_response(response: requests.Response) -> None:
    content_type = response.headers.get("Content-Type", "").lower()
    body_start = response.content[:512].lower()
    if b"exception" in body_start or b"serviceexception" in body_start:
        raise OgmServiceError("OGM WMS returned a service exception")
    if "svg" not in content_type and not body_start.lstrip().startswith(b"<svg"):
        raise OgmServiceError(f"OGM WMS returned unexpected content_type={content_type or 'unknown'}")


def wms_capabilities_url(service_url: str) -> str:
    return f"{service_url}?{urlencode({'SERVICE': 'WMS', 'REQUEST': 'GetCapabilities', 'VERSION': '1.3.0'})}"


def wms_getmap_url(
    service_url: str,
    bbox: tuple[float, float, float, float],
    layer_name: str = "0",
    width: int = 512,
    height: int = 512,
) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox
    params = {
        "SERVICE": "WMS",
        "VERSION": "1.1.1",
        "REQUEST": "GetMap",
        "LAYERS": layer_name,
        "STYLES": "",
        "SRS": "EPSG:4326",
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/png",
        "TRANSPARENT": "true",
    }
    return f"{service_url}?{urlencode(params)}"


def _bbox_param(bbox: tuple[float, float, float, float]) -> str:
    return ",".join(str(value) for value in bbox)


def _merge_features_by_source_id(features: list[OgmFeature]) -> list[OgmFeature]:
    merged: dict[str, OgmFeature] = {}
    for feature in features:
        current = merged.get(feature.source_id)
        if current is None:
            merged[feature.source_id] = feature
            continue
        merged[feature.source_id] = OgmFeature(
            source_id=feature.source_id,
            attributes={**current.attributes, **feature.attributes},
            geometry=_merge_geojson_polygon_parts(current.geometry, feature.geometry),
        )
    return list(merged.values())


def _merge_geojson_polygon_parts(left: dict, right: dict) -> dict:
    parts = _polygon_parts(left) + _polygon_parts(right)
    if not parts:
        return left or right
    if len(parts) == 1:
        return {"type": "Polygon", "coordinates": parts[0]}
    return {"type": "MultiPolygon", "coordinates": parts}


def _polygon_parts(geometry: dict | None) -> list:
    if not geometry:
        return []
    geometry_type = geometry.get("type")
    if geometry_type == "Polygon":
        return [geometry.get("coordinates") or []]
    if geometry_type == "MultiPolygon":
        return list(geometry.get("coordinates") or [])
    return []


def _svg_paths(svg: bytes) -> list[list[tuple[float, float]]]:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise OgmServiceError("OGM WMS returned SVG content that could not be parsed") from exc
    paths = []
    for element in root.iter():
        if not element.tag.endswith("path"):
            continue
        fill = element.attrib.get("fill")
        path_data = element.attrib.get("d", "")
        if not path_data.strip() or fill == "#FFFFFF":
            continue
        values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path_data)]
        points = list(zip(values[0::2], values[1::2], strict=False))
        if len(points) >= 4:
            paths.append(points)
    return paths


def _path_centroid_pixel(
    path: list[tuple[float, float]],
    width: int,
    height: int,
) -> tuple[int, int]:
    x = sum(point[0] for point in path) / len(path)
    y = sum(point[1] for point in path) / len(path)
    return _svg_point_to_pixel(x, y, width, height)


def _path_to_geojson(
    path: list[tuple[float, float]],
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> dict[str, Any]:
    ring = []
    for x, y in path:
        pixel_x, pixel_y = _svg_point_to_pixel(x, y, width, height)
        ring.append(_pixel_to_lonlat(pixel_x, pixel_y, bbox, width, height))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return {
        "type": "Polygon",
        "coordinates": [ring],
    }


def _svg_point_to_pixel(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    return (
        int(round((4 / 3) * x + width / 2)),
        int(round(-(4 / 3) * y + height / 2)),
    )


def _pixel_to_lonlat(
    pixel_x: int,
    pixel_y: int,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
) -> list[float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    lon = min_lon + (pixel_x / width) * (max_lon - min_lon)
    lat = max_lat - (pixel_y / height) * (max_lat - min_lat)
    return [round(lon, 7), round(lat, 7)]
