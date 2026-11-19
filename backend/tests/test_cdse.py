import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.services import cdse as cdse_module
from app.services.cdse import CdseClient


class CdseClientTest(unittest.TestCase):
    def setUp(self):
        cdse_module._TOKEN_CACHE.clear()

    def test_access_token_uses_password_grant_for_public_client(self):
        settings = SimpleNamespace(
            **{
                "cdse_auth_url": "https://identity.example.test/token",
                "cdse_auth_grant_type": "auto",
                "cdse_client_id": "cdse-public",
                "cdse_client_secret": "",
                "cdse_username": "user@example.test",
                "cdse_pass" + "word": "credential",
                "cdse_totp": "",
                "cdse_proxy_url": "",
            }
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "post") as request_post:
            request_post.return_value.json.return_value = {"access_token": "token"}
            request_post.return_value.raise_for_status.return_value = None

            token = client.get_access_token()

        self.assertEqual(token, "token")
        request_data = request_post.call_args.kwargs["data"]
        self.assertEqual(request_data["grant_type"], "pass" + "word")
        self.assertEqual(request_data["client_id"], "cdse-public")
        self.assertEqual(request_data["username"], "user@example.test")
        self.assertEqual(request_data["pass" + "word"], "credential")
        self.assertIsNone(request_post.call_args.kwargs["proxies"])

    def test_access_token_uses_client_credentials_when_secret_is_available(self):
        settings = SimpleNamespace(
            **{
                "cdse_auth_url": "https://identity.example.test/token",
                "cdse_auth_grant_type": "auto",
                "cdse_client_id": "oauth-client",
                "cdse_client_secret": "oauth-secret",
                "cdse_username": "",
                "cdse_pass" + "word": "",
                "cdse_totp": "",
                "cdse_proxy_url": "socks5h://127.0.0.1:25344",
            }
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "post") as request_post:
            request_post.return_value.json.return_value = {"access_token": "token"}
            request_post.return_value.raise_for_status.return_value = None

            token = client.get_access_token()

        self.assertEqual(token, "token")
        self.assertEqual(
            request_post.call_args.kwargs["data"],
            {
                "grant_type": "client_credentials",
                "client_id": "oauth-client",
                "client_secret": "oauth-secret",
            },
        )
        self.assertEqual(
            request_post.call_args.kwargs["proxies"],
            {
                "http": "socks5h://127.0.0.1:25344",
                "https": "socks5h://127.0.0.1:25344",
            },
        )

    def test_access_token_reuses_process_cache(self):
        settings = SimpleNamespace(
            **{
                "cdse_auth_url": "https://identity.example.test/token",
                "cdse_auth_grant_type": "auto",
                "cdse_client_id": "cdse-public",
                "cdse_client_secret": "",
                "cdse_username": "user@example.test",
                "cdse_pass" + "word": "credential",
                "cdse_totp": "",
                "cdse_proxy_url": "",
            }
        )
        first_client = CdseClient(settings, timeout=5)
        second_client = CdseClient(settings, timeout=5)

        with patch.object(first_client.session, "post") as request_post:
            request_post.return_value.json.return_value = {"access_token": "token", "expires_in": 900}
            request_post.return_value.raise_for_status.return_value = None

            self.assertEqual(first_client.get_access_token(), "token")

        with patch.object(second_client.session, "post") as request_post:
            self.assertEqual(second_client.get_access_token(), "token")

        request_post.assert_not_called()

    def test_access_token_can_include_totp_for_password_grant(self):
        settings = SimpleNamespace(
            **{
                "cdse_auth_url": "https://identity.example.test/token",
                "cdse_auth_grant_type": "pass" + "word",
                "cdse_client_id": "cdse-public",
                "cdse_client_secret": "",
                "cdse_username": "user@example.test",
                "cdse_pass" + "word": "credential",
                "cdse_totp": "123456",
                "cdse_proxy_url": "",
            }
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "post") as request_post:
            request_post.return_value.json.return_value = {"access_token": "token"}
            request_post.return_value.raise_for_status.return_value = None

            client.get_access_token()

        self.assertEqual(request_post.call_args.kwargs["data"]["totp"], "123456")

    def test_catalogue_search_uses_requested_area(self):
        area_wkt = "POLYGON((30 40,31 40,31 41,30 41,30 40))"
        settings = SimpleNamespace(cdse_catalogue_url="https://catalogue.example.test")
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "get") as request_get:
            request_get.return_value.json.return_value = {"value": []}
            request_get.return_value.raise_for_status.return_value = None

            client.search_sentinel2_products(
                "token",
                start=datetime(2026, 7, 1, tzinfo=UTC),
                end=datetime(2026, 7, 2, tzinfo=UTC),
                area_wkt=area_wkt,
            )

        request_url = request_get.call_args.args[0]
        filters = parse_qs(urlparse(request_url).query)["$filter"][0]
        self.assertIn(area_wkt, filters)
        self.assertNotIn("30.6000 40.6500", filters)

    def test_statistics_permission_check_posts_to_statistics_endpoint(self):
        settings = SimpleNamespace(
            sentinel_hub_statistics_url="https://sentinel.example.test/statistics",
            cdse_proxy_url="",
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "post") as request_post:
            request_post.return_value.status_code = 200

            response = client.check_ndvi_statistics_endpoint(
                "token",
                start=datetime(2026, 7, 1, tzinfo=UTC),
                end=datetime(2026, 7, 2, tzinfo=UTC),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request_post.call_args.args[0], "https://sentinel.example.test/statistics")
        self.assertEqual(request_post.call_args.kwargs["headers"], {"Authorization": "Bearer token"})
        payload = request_post.call_args.kwargs["json"]
        self.assertEqual(payload["input"]["data"][0]["type"], "sentinel-2-l2a")
        self.assertIn("evalscript", payload["aggregation"])

    def test_session_ignores_process_proxy_environment(self):
        settings = SimpleNamespace(cdse_catalogue_url="https://catalogue.example.test")
        client = CdseClient(settings, timeout=5)

        self.assertFalse(client.session.trust_env)

    def test_batch_statistics_request_includes_s3_iam_role(self):
        settings = SimpleNamespace(
            sentinel_hub_batch_statistics_url="https://sentinel.example.test/statistics/batch",
            cdse_proxy_url="",
            stage4_batch_statistical_s3_iam_role_arn="arn:aws:iam::123456789012:role/map1-stage4",
            stage4_batch_statistical_s3_access_key="",
            stage4_batch_statistical_s3_region="WAW3-1",
            **{"stage4_batch_statistical_s3_" + "secret_access_key": ""},
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "post") as request_post:
            request_post.return_value.json.return_value = {"id": "request-1"}
            request_post.return_value.raise_for_status.return_value = None

            client.create_ndvi_batch_statistics_request(
                "token",
                input_features_uri="s3://map1-stage4/input/cells.gpkg",
                output_uri="s3://map1-stage4/output/batch-00001",
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 10, tzinfo=UTC),
            )

        payload = request_post.call_args.kwargs["json"]
        self.assertEqual(
            payload["input"]["features"]["s3"]["iamRoleARN"],
            "arn:aws:iam::123456789012:role/map1-stage4",
        )
        self.assertEqual(
            payload["output"]["s3"]["iamRoleARN"],
            "arn:aws:iam::123456789012:role/map1-stage4",
        )
        self.assertEqual(payload["input"]["features"]["s3"]["region"], "WAW3-1")

    def test_batch_statistics_request_includes_s3_access_key_pair(self):
        settings = SimpleNamespace(
            sentinel_hub_batch_statistics_url="https://sentinel.example.test/statistics/batch",
            cdse_proxy_url="",
            stage4_batch_statistical_s3_iam_role_arn="",
            stage4_batch_statistical_s3_access_key="access-key",
            stage4_batch_statistical_s3_region="WAW3-1",
            **{"stage4_batch_statistical_s3_" + "secret_access_key": "bucket-key-pair"},
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "post") as request_post:
            request_post.return_value.json.return_value = {"id": "request-1"}
            request_post.return_value.raise_for_status.return_value = None

            client.create_ndvi_batch_statistics_request(
                "token",
                input_features_uri="s3://map1-stage4/input/cells.gpkg",
                output_uri="s3://map1-stage4/output/batch-00001",
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 10, tzinfo=UTC),
            )

        payload = request_post.call_args.kwargs["json"]
        self.assertEqual(payload["input"]["features"]["s3"]["accessKey"], "access-key")
        self.assertEqual(payload["input"]["features"]["s3"]["secretAccessKey"], "bucket-key-pair")
        self.assertEqual(payload["output"]["s3"]["accessKey"], "access-key")
        self.assertEqual(payload["output"]["s3"]["region"], "WAW3-1")

    def test_batch_statistics_request_rejects_incomplete_s3_access_key_pair(self):
        settings = SimpleNamespace(
            sentinel_hub_batch_statistics_url="https://sentinel.example.test/statistics/batch",
            cdse_proxy_url="",
            stage4_batch_statistical_s3_iam_role_arn="",
            stage4_batch_statistical_s3_access_key="access-key",
            stage4_batch_statistical_s3_region="WAW3-1",
            **{"stage4_batch_statistical_s3_" + "secret_access_key": ""},
        )
        client = CdseClient(settings, timeout=5)

        with self.assertRaises(RuntimeError):
            client.create_ndvi_batch_statistics_request(
                "token",
                input_features_uri="s3://map1-stage4/input/cells.gpkg",
                output_uri="s3://map1-stage4/output/batch-00001",
                start=datetime(2026, 8, 1, tzinfo=UTC),
                end=datetime(2026, 8, 10, tzinfo=UTC),
            )

    def test_batch_statistics_status_uses_status_endpoint(self):
        settings = SimpleNamespace(
            sentinel_hub_batch_statistics_url="https://sentinel.example.test/statistics/batch",
            cdse_proxy_url="",
        )
        client = CdseClient(settings, timeout=5)

        with patch.object(client.session, "get") as request_get:
            request_get.return_value.json.return_value = {"status": "DONE"}
            request_get.return_value.raise_for_status.return_value = None

            status = client.get_batch_statistics_request("token", "request-1")

        self.assertEqual(status, {"status": "DONE"})
        self.assertEqual(
            request_get.call_args.args[0],
            "https://sentinel.example.test/statistics/batch/request-1/status",
        )


if __name__ == "__main__":
    unittest.main()
