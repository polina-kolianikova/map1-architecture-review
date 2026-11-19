import asyncio
import base64
import json
import unittest

from app import main
from app.api import meta
from app.core.config import Settings
from app.services.localization import localization_manifest, parse_supported_locales, resolve_locale


class Stage6LocalizationTest(unittest.TestCase):
    def test_locale_settings_default_to_english_hook(self):
        settings = Settings(app_default_locale="en-US", app_supported_locales="en,ru")

        self.assertEqual(settings.default_locale, "en")
        self.assertEqual(settings.supported_locales, ["en", "ru"])

    def test_locale_resolution_falls_back_cleanly(self):
        oversized = "en-" + ("toolong-" * 8)
        self.assertEqual(
            parse_supported_locales(f"en, ru, en-US, bad<script>, {oversized}"),
            ("en", "ru"),
        )
        self.assertEqual(resolve_locale("ru-RU", ("en", "ru")), "ru")
        self.assertEqual(resolve_locale("ka", ("en", "ru")), "en")

    def test_localization_manifest_exposes_public_locale_state(self):
        manifest = localization_manifest(Settings(app_default_locale="en", app_supported_locales="en"))

        self.assertEqual(manifest["default_locale"], "en")
        self.assertEqual(manifest["fallback_locale"], "en")
        self.assertEqual(manifest["supported_locales"], ["en"])
        self.assertEqual(manifest["version"], "stage6-localization-v1")
        self.assertNotIn("language_pack_contract", manifest)

    def test_localization_endpoint_is_registered(self):
        response = asyncio.run(asgi_get(main.app, "/meta/localization"))
        payload = json.loads(response["body"])

        self.assertEqual(response["status"], 200)
        self.assertEqual(payload["default_locale"], "en")

    def test_localization_endpoint_uses_current_settings(self):
        original_get_settings = meta.get_settings
        try:
            meta.get_settings = lambda: Settings(app_default_locale="ru-RU", app_supported_locales="en,ru")
            response = asyncio.run(asgi_get(main.app, "/meta/localization"))
            payload = json.loads(response["body"])

            self.assertEqual(response["status"], 200)
            self.assertEqual(payload["default_locale"], "ru")
            self.assertEqual(payload["supported_locales"], ["en", "ru"])
        finally:
            meta.get_settings = original_get_settings

    def test_basic_auth_helper_accepts_only_exact_credentials(self):
        original_settings = main.settings
        try:
            main.settings = review_auth_settings()
            valid_token = base64.b64encode(b"review:review-password-fixture").decode("ascii")
            invalid_token = base64.b64encode(b"review:wrong").decode("ascii")

            self.assertTrue(main._valid_basic_auth(f"Basic {valid_token}"))
            self.assertFalse(main._valid_basic_auth(f"Basic {invalid_token}"))
            self.assertFalse(main._valid_basic_auth("Bearer nope"))
        finally:
            main.settings = original_settings

    def test_localization_endpoint_obeys_backend_basic_auth(self):
        original_settings = main.settings
        try:
            main.settings = review_auth_settings()
            unauthenticated = asyncio.run(asgi_get(main.app, "/meta/localization"))
            valid_token = base64.b64encode(b"review:review-password-fixture").decode("ascii")
            authenticated = asyncio.run(
                asgi_get(
                    main.app,
                    "/meta/localization",
                    headers=[(b"authorization", f"Basic {valid_token}".encode("ascii"))],
                )
            )

            self.assertEqual(unauthenticated["status"], 401)
            self.assertEqual(json.loads(unauthenticated["body"]), {"detail": "Unauthorized"})
            self.assertEqual(authenticated["status"], 200)
            self.assertEqual(json.loads(authenticated["body"])["default_locale"], "en")
        finally:
            main.settings = original_settings


async def asgi_get(app, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, object]:
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers or [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return {"status": status, "body": body.decode("utf-8")}


def review_auth_settings() -> Settings:
    return Settings(
        backend_basic_auth_user="review",
        **{"backend_basic_auth_" + "password": "review-password-fixture"},
    )


if __name__ == "__main__":
    unittest.main()
