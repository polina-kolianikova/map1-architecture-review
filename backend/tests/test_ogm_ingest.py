import unittest

from app.commands.ingest_ogm_test_subset import _insert_current_features
from app.services.ogm import OgmFeature


class InsertResult:
    def scalar_one_or_none(self):
        return None


class RecordingDb:
    def __init__(self):
        self.params = []

    def execute(self, _statement, params=None):
        self.params.append(params or {})
        return InsertResult()


class OgmIngestTest(unittest.TestCase):
    def test_clipped_insert_binds_region_id(self):
        db = RecordingDb()
        feature = OgmFeature(
            source_id="feature-1",
            attributes={"BLM_NO": "1"},
            geometry={
                "type": "Polygon",
                "coordinates": [
                    [
                        [31.0, 40.0],
                        [31.1, 40.0],
                        [31.1, 40.1],
                        [31.0, 40.1],
                        [31.0, 40.0],
                    ]
                ],
            },
        )

        _insert_current_features(
            db,
            {"id": 12, "code": "TR-TEST"},
            [feature],
            source_layer="ogm_esatis_bolme",
            source_url="https://example.test",
            pipeline="stage3_ogm_bolme",
            official_status="official_bolme",
            timeout=30,
            tls_verify=True,
            clip_to_region=True,
        )

        self.assertEqual(db.params[0]["region_id"], 12)
        self.assertTrue(db.params[0]["clip_to_region"])


if __name__ == "__main__":
    unittest.main()
