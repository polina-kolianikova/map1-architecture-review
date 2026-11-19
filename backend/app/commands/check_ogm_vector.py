import argparse

from app.core.config import get_settings
from app.services.grid import TEST_REGION_COORDINATES
from app.services.ogm import OgmBolmeClient, OgmMescereWmsClient, OgmServiceError


def main() -> None:
    args = parse_args()
    settings = get_settings()
    bbox = _bbox_from_coordinates(TEST_REGION_COORDINATES)
    try:
        bolme_features = OgmBolmeClient(
            settings.ogm_bolme_feature_url,
            timeout=args.ogm_timeout,
            verify_tls=settings.ogm_tls_verify,
            proxy_url=settings.ogm_proxy_url,
        ).query_bbox(
            bbox,
            limit=5,
        )
        mescere_features = OgmMescereWmsClient(
            settings.ogm_mescere_wms_url,
            timeout=args.ogm_timeout,
            verify_tls=settings.ogm_tls_verify,
            proxy_url=settings.ogm_proxy_url,
        ).query_bbox(bbox)
    except OgmServiceError as exc:
        print(f"ogm vector unavailable error={exc}")
        raise SystemExit(1) from exc

    print(
        "ogm vector ok "
        f"bolme_features={len(bolme_features)} "
        f"mescere_features={len(mescere_features)} "
        f"mescere_sample_ids={','.join(feature.source_id for feature in mescere_features[:5])}"
    )


def _bbox_from_coordinates(coordinates: list[list[float]]) -> tuple[float, float, float, float]:
    longitudes = [point[0] for point in coordinates]
    latitudes = [point[1] for point in coordinates]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ogm-timeout", type=int, default=12)
    return parser.parse_args()


if __name__ == "__main__":
    main()
