import base64
import ipaddress
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.meta import router as meta_router
from app.api.stage1 import router as stage1_router
from app.api.stage2 import router as stage2_router
from app.api.stage4 import router as stage4_router
from app.api.stage5 import router as stage5_router
from app.core.config import get_settings

settings = get_settings()


def _validate_production_provider_urls() -> None:
    allowed_suffixes = (
        ".dataspace.copernicus.eu",
        ".ogm.gov.tr",
        ".csb.gov.tr",
        "dataspace.copernicus.eu",
        "ogm.gov.tr",
        "csb.gov.tr",
    )
    for value in (
        settings.cdse_auth_url,
        settings.cdse_catalogue_url,
        settings.sentinel_hub_statistics_url,
        settings.sentinel_hub_process_url,
        settings.ogm_base_url,
        settings.ogm_eharita_url,
        settings.ogm_portal_url,
        settings.ogm_arazi_ortusu_wms_url,
        settings.ogm_mescere_wms_url,
        settings.ogm_seflik_wms_url,
        settings.ogm_isletme_wms_url,
        settings.ogm_bolge_wms_url,
        settings.ogm_indeks_wms_url,
        settings.ogm_bolme_feature_url,
        settings.tucbs_orman_wms_url,
    ):
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise RuntimeError("Production provider URLs must use HTTPS")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (address.is_private or address.is_loopback or address.is_link_local):
            raise RuntimeError("Production provider URLs must not target private network addresses")
        if not address and not any(host == suffix or host.endswith(suffix) for suffix in allowed_suffixes):
            raise RuntimeError("Production provider URL host is not allowed")


def _strong_runtime_secret(value: str) -> bool:
    candidate = value.strip()
    normalized = candidate.lower().replace("-", "").replace("_", "").replace(" ", "")
    placeholder_markers = ("changeme", "placeholder", "password", "secret", "example", "default")
    if len(candidate) < 16 or any(marker in normalized for marker in placeholder_markers):
        return False
    return len(set(candidate)) >= 8


production_docs = settings.app_env == "production"
if (
    production_docs
    and not settings.trust_local_reverse_proxy_only
    and (not settings.backend_basic_auth_user or not settings.backend_basic_auth_password)
):
    raise RuntimeError("Production API requires Basic Auth or an explicit local reverse-proxy trust boundary")
if production_docs and settings.backend_basic_auth_password:
    if not _strong_runtime_secret(settings.backend_basic_auth_password):
        raise RuntimeError("Production backend Basic Auth password is not strong enough")
if production_docs and settings.postgres_password:
    if not _strong_runtime_secret(settings.postgres_password):
        raise RuntimeError("Production database password is not strong enough")
if production_docs:
    if not settings.ogm_tls_verify:
        raise RuntimeError("Production OGM TLS verification must be enabled")
    _validate_production_provider_urls()
    for origin in settings.cors_origins:
        normalized = origin.lower()
        local_http = normalized.startswith("http://127.0.0.1") or normalized.startswith("http://localhost")
        if origin == "*" or (normalized.startswith("http://") and not local_http):
            raise RuntimeError("Production CORS origins must be explicit HTTPS origins")

app = FastAPI(
    title="Map 1 Turkey API",
    docs_url=None if production_docs else "/docs",
    redoc_url=None if production_docs else "/redoc",
    openapi_url=None if production_docs else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_backend_basic_auth(request: Request, call_next):
    if not settings.backend_basic_auth_user or not settings.backend_basic_auth_password:
        return await call_next(request)
    if request.url.path == "/health":
        return await call_next(request)
    if _valid_basic_auth(request.headers.get("authorization", "")):
        return await call_next(request)
    return JSONResponse(
        {"detail": "Unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Map 1 Turkey"'},
    )


def _valid_basic_auth(header: str) -> bool:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return secrets.compare_digest(username, settings.backend_basic_auth_user) and secrets.compare_digest(
        password,
        settings.backend_basic_auth_password,
    )


app.include_router(health_router)
app.include_router(meta_router)
app.include_router(stage1_router)
app.include_router(stage2_router)
app.include_router(stage4_router)
app.include_router(stage5_router)


from sqlalchemy.exc import SQLAlchemyError


@app.exception_handler(SQLAlchemyError)
async def unavailable_database(_request, _error):
    return JSONResponse({"detail": "Database unavailable or schema not initialized."}, status_code=503)


_original_openapi = app.openapi


def review_openapi():
    schema = _original_openapi()
    private_routes = [("get", "/stage5/rules"), ("post", "/stage5/rules/validate"), ("put", "/stage5/rules"), ("post", "/stage5/rules/reset"), ("get", "/stage5/rules/history")]
    for method, path in private_routes:
        schema["paths"][path][method].setdefault("responses", {})["501"] = {"description": "Private calculation rules/configuration is not distributed."}
    return schema


app.openapi = review_openapi
