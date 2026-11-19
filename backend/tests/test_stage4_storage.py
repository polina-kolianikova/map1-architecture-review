import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.stage4_storage import download_s3_prefix, parse_s3_uri, upload_s3_file


class Stage4StorageTest(unittest.TestCase):
    def test_parse_s3_uri_requires_bucket_and_key(self):
        parsed = parse_s3_uri("s3://map1-stage4/stage4/input/cells.gpkg")

        self.assertEqual(parsed.bucket, "map1-stage4")
        self.assertEqual(parsed.key, "stage4/input/cells.gpkg")

    def test_parse_s3_uri_rejects_non_s3_uri(self):
        with self.assertRaises(ValueError):
            parse_s3_uri("storagebox://map1-stage4/stage4/input")

    def test_parse_s3_uri_rejects_bucket_without_key(self):
        with self.assertRaises(ValueError):
            parse_s3_uri("s3://map1-stage4")

    def test_upload_s3_file_uses_configured_credentials_and_region(self):
        fake_client = MagicMock()
        client_factory = MagicMock(return_value=fake_client)
        settings = _settings()

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cells.gpkg"
            path.write_text("sample")
            with patch.dict("sys.modules", {"boto3": SimpleNamespace(client=client_factory)}):
                upload_s3_file(path, "s3://map1-stage4/stage4/input/cells.gpkg", settings=settings)

        client_factory.assert_called_once_with(
            "s3",
            aws_access_key_id="access-key",
            **{
                "aws_" + "secret_access_key": "bucket-key-pair",
                "region_name": "WAW3-1",
            },
        )
        fake_client.upload_file.assert_called_once_with(
            str(path),
            "map1-stage4",
            "stage4/input/cells.gpkg",
        )

    def test_upload_s3_file_uses_configured_endpoint_for_cdse_bucket(self):
        fake_client = MagicMock()
        client_factory = MagicMock(return_value=fake_client)
        settings = _settings(endpoint_url="https://s3.waw3-1.cloudferro.com")

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cells.gpkg"
            path.write_text("sample")
            with patch.dict("sys.modules", {"boto3": SimpleNamespace(client=client_factory)}):
                upload_s3_file(path, "s3://map1-stage4/stage4/input/cells.gpkg", settings=settings)

        client_factory.assert_called_once_with(
            "s3",
            aws_access_key_id="access-key",
            **{
                "aws_" + "secret_access_key": "bucket-key-pair",
                "region_name": "WAW3-1",
                "endpoint_url": "https://s3.waw3-1.cloudferro.com",
            },
        )

    def test_download_s3_prefix_downloads_json_outputs(self):
        fake_client = MagicMock()
        fake_client.get_paginator.return_value.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "stage4/output/batch-00001/result-1.json"},
                    {"Key": "stage4/output/batch-00001/readme.txt"},
                    {"Key": "stage4/output/batch-00001/nested/result-2.json"},
                ]
            }
        ]
        client_factory = MagicMock(return_value=fake_client)

        with TemporaryDirectory() as temp_dir:
            with patch.dict("sys.modules", {"boto3": SimpleNamespace(client=client_factory)}):
                paths = download_s3_prefix(
                    "s3://map1-stage4/stage4/output/batch-00001",
                    Path(temp_dir),
                    settings=_settings(),
                )

        self.assertEqual(
            [path.as_posix().split(temp_dir + "/", 1)[1] for path in paths],
            ["result-1.json", "nested/result-2.json"],
        )
        self.assertEqual(fake_client.download_file.call_count, 2)
        fake_client.get_paginator.return_value.paginate.assert_called_once_with(
            Bucket="map1-stage4",
            Prefix="stage4/output/batch-00001",
        )

    def test_s3_transfer_rejects_missing_credentials(self):
        settings = _settings(access_key="", bucket_key_pair="")

        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cells.gpkg"
            path.write_text("sample")
            with self.assertRaises(RuntimeError):
                upload_s3_file(path, "s3://map1-stage4/stage4/input/cells.gpkg", settings=settings)


def _settings(
    access_key: str = "access-key",
    bucket_key_pair: str = "bucket-key-pair",
    endpoint_url: str = "",
):
    return SimpleNamespace(
        stage4_batch_statistical_s3_access_key=access_key,
        **{
            "stage4_batch_statistical_s3_" + "secret_access_key": bucket_key_pair,
            "stage4_batch_statistical_s3_region": "WAW3-1",
            "stage4_batch_statistical_s3_endpoint_url": endpoint_url,
        },
    )


if __name__ == "__main__":
    unittest.main()
