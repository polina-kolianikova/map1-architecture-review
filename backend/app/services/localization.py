from __future__ import annotations

import re

DEFAULT_LOCALE = "en"
FALLBACK_LOCALE = "en"
LOCALE_ALIASES = {
    "en-gb": "en",
    "en-us": "en",
}
LOCALE_PATTERN = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
MAX_LOCALE_LENGTH = 35


def normalize_locale(value: str | None) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if not normalized:
        return ""
    locale = LOCALE_ALIASES.get(normalized, normalized)
    if len(locale) > MAX_LOCALE_LENGTH:
        return ""
    return locale if LOCALE_PATTERN.fullmatch(locale) else ""


def parse_supported_locales(
    value: str | list[str] | tuple[str, ...] | None,
) -> tuple[str, ...]:
    if isinstance(value, str):
        candidates = value.split(",")
    else:
        candidates = value or ()
    locales: list[str] = []
    for candidate in candidates:
        locale = normalize_locale(str(candidate))
        if locale and locale not in locales:
            locales.append(locale)
    return tuple(locales or (DEFAULT_LOCALE,))


def resolve_locale(
    requested: str | None,
    supported_locales: tuple[str, ...] | list[str] | None = None,
    fallback: str = FALLBACK_LOCALE,
) -> str:
    supported = parse_supported_locales(supported_locales)
    fallback_locale = normalize_locale(fallback)
    if fallback_locale not in supported:
        fallback_locale = supported[0]
    requested_locale = normalize_locale(requested)
    if not requested_locale:
        return fallback_locale
    if requested_locale in supported:
        return requested_locale
    language = requested_locale.split("-")[0]
    return language if language in supported else fallback_locale


def localization_manifest(settings) -> dict[str, object]:
    supported = parse_supported_locales(settings.app_supported_locales)
    default_locale = resolve_locale(settings.app_default_locale, supported)
    return {
        "default_locale": default_locale,
        "fallback_locale": FALLBACK_LOCALE,
        "supported_locales": list(supported),
        "version": "stage6-localization-v1",
    }
