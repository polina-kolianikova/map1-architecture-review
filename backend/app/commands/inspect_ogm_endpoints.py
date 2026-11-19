import argparse
import json
from pathlib import Path
import tempfile

from app.core.config import get_settings
from app.services.ogm import OgmEndpointInspector, serialize_checks
from app.services.ogm_webmap import OgmWebMapClient


OUTPUT_PATH = Path(tempfile.gettempdir()) / "map1_ogm_endpoint_check.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect public OGM endpoint availability")
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()
    if args.timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    settings = get_settings()
    inspector = OgmEndpointInspector(
        timeout=args.timeout,
        verify_tls=settings.ogm_tls_verify,
        proxy_url=settings.ogm_proxy_url,
    )
    checks = inspector.inspect(settings.ogm_eharita_url, settings.tucbs_orman_wms_url)
    webmap = _inspect_webmap(settings, args.timeout)
    payload = {
        "checks": serialize_checks(checks),
        "webmap": webmap,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    reachable = sum(1 for check in checks if check.status == "reachable")
    http_errors = sum(1 for check in checks if check.status == "http_error")
    unavailable = sum(1 for check in checks if check.status == "unavailable")
    webmap_candidates = len(webmap.get("service_candidates", []))
    print(
        "ogm endpoint check "
        f"reachable={reachable} "
        f"http_errors={http_errors} "
        f"unavailable={unavailable} "
        f"webmap_candidates={webmap_candidates} "
        f"output={OUTPUT_PATH}"
    )


def _inspect_webmap(settings, timeout: int) -> dict:
    try:
        candidates = OgmWebMapClient(
            settings.ogm_portal_url,
            settings.ogm_webmap_item_id,
            timeout=timeout,
            verify_tls=settings.ogm_tls_verify,
            proxy_url=settings.ogm_proxy_url,
        ).fetch_service_candidates()
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:240]}",
            "service_candidates": [],
        }
    return {
        "status": "ok",
        "service_candidates": [
            {
                "layer_id": candidate.layer_id,
                "title": candidate.title,
                "service_url": candidate.service_url,
                "layer_url": candidate.layer_url,
                "query_url": candidate.query_url,
                "layer_type": candidate.layer_type,
                "visible": candidate.visible,
                "parent_id": candidate.parent_id,
            }
            for candidate in candidates
        ],
    }


if __name__ == "__main__":
    main()
