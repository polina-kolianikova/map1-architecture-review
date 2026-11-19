from fastapi import APIRouter

from app.core.config import get_settings
from app.services.localization import localization_manifest

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/localization")
def localization() -> dict[str, object]:
    return localization_manifest(get_settings())
