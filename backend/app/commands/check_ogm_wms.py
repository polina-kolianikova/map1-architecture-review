from pathlib import Path
import tempfile

from app.core.config import get_settings
from app.services.grid import TEST_REGION_COORDINATES
from app.services.ogm import (
    OGM_WMS_LAYERS,
    OgmClientBase,
    OgmServiceError,
    wms_capabilities_url,
    wms_getmap_url,
)


OUTPUT_DIR = Path(tempfile.gettempdir()) / "map1_ogm_wms"


def main() -> None:
    settings = get_settings()
    client = OgmClientBase(
        timeout=45,
        verify_tls=settings.ogm_tls_verify,
        proxy_url=settings.ogm_proxy_url,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    bbox = _bbox_from_coordinates(TEST_REGION_COORDINATES)
    failures = []
    required_failures = []

    for layer in OGM_WMS_LAYERS:
        try:
            capabilities = _get(client, wms_capabilities_url(layer.url), "xml")
            map_png = _get(client, wms_getmap_url(layer.url, bbox, layer.layer_name), "png")
        except (OgmServiceError, RuntimeError) as exc:
            failures.append(layer.service_key)
            if layer.stage1_required:
                required_failures.append(layer.service_key)
            print(
                "ogm wms unavailable "
                f"service={layer.service_key} "
                f"required={str(layer.stage1_required).lower()} "
                f"error={_short_error(exc)}"
            )
            continue
        (OUTPUT_DIR / f"{layer.service_key}_capabilities.xml").write_bytes(capabilities)
        (OUTPUT_DIR / f"{layer.service_key}_test_map.png").write_bytes(map_png)
        print(
            "ogm wms ok "
            f"service={layer.service_key} "
            f"capabilities_bytes={len(capabilities)} "
            f"map_bytes={len(map_png)}"
        )

    if failures:
        print(
            "ogm wms summary "
            f"failures={','.join(failures)} "
            f"required_failures={','.join(required_failures)}"
        )
    if required_failures:
        raise SystemExit(1)


def _get(client: OgmClientBase, url: str, expected: str) -> bytes:
    response = client.get(url)
    content_type = response.headers.get("Content-Type", "").lower()
    if expected == "xml" and "xml" not in content_type:
        raise RuntimeError(f"Expected XML response from {url}, got {content_type}")
    if expected == "png" and "png" not in content_type:
        raise RuntimeError(f"Expected PNG response from {url}, got {content_type}")
    return response.content


def _short_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}"


def _bbox_from_coordinates(coordinates: list[list[float]]) -> tuple[float, float, float, float]:
    longitudes = [point[0] for point in coordinates]
    latitudes = [point[1] for point in coordinates]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


if __name__ == "__main__":
    main()
