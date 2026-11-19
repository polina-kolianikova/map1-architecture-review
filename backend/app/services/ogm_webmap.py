from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin


@dataclass(frozen=True)
class WebMapLayer:
    layer_id: str
    title: str
    url: str | None
    layer_type: str | None
    visible: bool
    parent_id: str | None = None
    service_layer_id: str | None = None


@dataclass(frozen=True)
class WebMapServiceCandidate:
    layer_id: str
    title: str
    service_url: str
    layer_url: str
    query_url: str
    layer_type: str | None
    visible: bool
    parent_id: str | None = None


class OgmWebMapClient:
    def __init__(
        self,
        portal_url: str,
        webmap_item_id: str,
        timeout: int = 45,
        verify_tls: bool = True,
        proxy_url: str = "",
    ) -> None:
        from app.services.ogm import OgmClientBase

        self.client = OgmClientBase(timeout=timeout, verify_tls=verify_tls, proxy_url=proxy_url)
        self.portal_url = portal_url.rstrip("/")
        self.webmap_item_id = webmap_item_id

    def fetch_layers(self) -> list[WebMapLayer]:
        return parse_webmap_layers(self.fetch_payload())

    def fetch_service_candidates(self) -> list[WebMapServiceCandidate]:
        return parse_webmap_service_candidates(self.fetch_payload())

    def fetch_payload(self) -> dict[str, Any]:
        from app.services.ogm import OgmServiceError

        response = self.client.get(self._item_data_url(), params={"f": "json"})
        payload = response.json()
        if not isinstance(payload, dict):
            raise OgmServiceError("OGM webmap returned unexpected payload")
        return payload

    def _item_data_url(self) -> str:
        base = self.portal_url
        if "/sharing/rest" not in base:
            base = urljoin(f"{base}/", "sharing/rest")
        return f"{base.rstrip('/')}/content/items/{self.webmap_item_id}/data"


def parse_webmap_layers(payload: dict[str, Any]) -> list[WebMapLayer]:
    layers: list[WebMapLayer] = []
    for raw_layer in payload.get("operationalLayers", []):
        if not isinstance(raw_layer, dict):
            continue
        parent = _layer_from_payload(raw_layer)
        layers.append(parent)
        layers.extend(_child_layers(raw_layer, parent))
    return layers


def parse_webmap_service_candidates(payload: dict[str, Any]) -> list[WebMapServiceCandidate]:
    candidates = []
    for layer in parse_webmap_layers(payload):
        candidate = _service_candidate(layer)
        if candidate:
            candidates.append(candidate)
    return candidates


def _layer_from_payload(
    payload: dict[str, Any],
    parent_id: str | None = None,
    inherited_url: str | None = None,
) -> WebMapLayer:
    raw_layer_id = _payload_value(payload, "id", "layerId", "title")
    layer_id = str(raw_layer_id if raw_layer_id is not None else "layer")
    return WebMapLayer(
        layer_id=layer_id,
        title=str(payload.get("title") or payload.get("name") or layer_id),
        url=payload.get("url") or inherited_url,
        layer_type=payload.get("layerType") or payload.get("type"),
        visible=bool(payload.get("visibility", payload.get("visible", True))),
        parent_id=parent_id,
        service_layer_id=_service_layer_id(payload),
    )


def _child_layers(payload: dict[str, Any], parent: WebMapLayer) -> list[WebMapLayer]:
    layers = []
    for key in ("layers", "sublayers"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                child = _layer_from_payload(item, parent_id=parent.layer_id, inherited_url=parent.url)
                layers.append(child)
                layers.extend(_child_layers(item, child))
    return layers


def _service_candidate(layer: WebMapLayer) -> WebMapServiceCandidate | None:
    if not layer.url:
        return None
    url = layer.url.rstrip("/")
    if _is_wms_layer(url, layer.layer_type):
        return WebMapServiceCandidate(
            layer_id=layer.layer_id,
            title=layer.title,
            service_url=url,
            layer_url=url,
            query_url=url,
            layer_type=layer.layer_type,
            visible=layer.visible,
            parent_id=layer.parent_id,
        )
    layer_url = _layer_url(url, layer.service_layer_id or layer.layer_id)
    if not _is_arcgis_layer(layer_url):
        return None
    return WebMapServiceCandidate(
        layer_id=layer.layer_id,
        title=layer.title,
        service_url=_service_url(layer_url),
        layer_url=layer_url,
        query_url=f"{layer_url}/query",
        layer_type=layer.layer_type,
        visible=layer.visible,
        parent_id=layer.parent_id,
    )


def _layer_url(url: str, layer_id: str) -> str:
    if _is_arcgis_layer(url):
        return url
    if url.endswith(("/MapServer", "/FeatureServer")) and layer_id.isdigit():
        return f"{url}/{layer_id}"
    return url


def _service_url(layer_url: str) -> str:
    parts = layer_url.rsplit("/", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return layer_url


def _is_arcgis_layer(url: str) -> bool:
    parts = url.rsplit("/", 1)
    return len(parts) == 2 and parts[1].isdigit() and (
        "/MapServer/" in url or "/FeatureServer/" in url
    )


def _is_wms_layer(url: str, layer_type: str | None) -> bool:
    normalized = url.lower()
    layer_hint = (layer_type or "").lower()
    return "wms" in layer_hint or "service=wms" in normalized or normalized.endswith("/wms")


def _service_layer_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("layerId")
    if value is None and isinstance(payload.get("id"), int):
        value = payload.get("id")
    if value is None:
        return None
    return str(value)


def _payload_value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None
