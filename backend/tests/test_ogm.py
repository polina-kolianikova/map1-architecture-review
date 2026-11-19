import json
import unittest

from requests import Response

from app.services.ogm import (
    MAX_MESCERE_SVG_PATHS,
    OgmBolmeClient,
    OgmClientBase,
    OgmMescereWmsClient,
    OgmServiceError,
    _ensure_json_response,
    _ensure_wms_svg_response,
)


class OgmClientSafetyTest(unittest.TestCase):
    def test_proxy_url_is_applied_to_ogm_session(self):
        client = OgmClientBase(proxy_url="socks5h://127.0.0.1:25344")

        self.assertEqual(client.session.proxies["https"], "socks5h://127.0.0.1:25344")
        self.assertEqual(client.session.proxies["http"], "socks5h://127.0.0.1:25344")
        self.assertFalse(client.session.trust_env)

    def test_transport_retries_can_be_disabled_for_bulk_ingest(self):
        client = OgmClientBase(transport_retries=0)

        adapter = client.session.get_adapter("https://example.test")
        self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(adapter.max_retries.connect, 0)
        self.assertEqual(adapter.max_retries.read, 0)

    def test_transport_retries_must_not_be_negative(self):
        with self.assertRaisesRegex(ValueError, "transport_retries"):
            OgmClientBase(transport_retries=-1)

    def test_wms_service_exception_is_rejected(self):
        response = Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/xml"
        response._content = b"<ServiceExceptionReport><ServiceException>blocked</ServiceException></ServiceExceptionReport>"

        with self.assertRaisesRegex(OgmServiceError, "service exception"):
            _ensure_wms_svg_response(response)

    def test_feature_info_requires_json_response(self):
        response = Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/html"
        response._content = b"<html>login</html>"

        with self.assertRaisesRegex(OgmServiceError, "unexpected content_type"):
            _ensure_json_response(response, "feature info")

    def test_arcgis_pagination_rejects_full_duplicate_page(self):
        response = Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "type": "FeatureCollection",
                "exceededTransferLimit": True,
                "features": [
                    {
                        "id": 7,
                        "properties": {"OBJECTID": 7},
                        "geometry": {"type": "Polygon", "coordinates": []},
                    }
                ],
            }
        ).encode()
        client = OgmBolmeClient("https://example.test/FeatureServer/0")
        client.get = lambda *args, **kwargs: response

        with self.assertRaisesRegex(OgmServiceError, "duplicate page"):
            client.query_bbox((30, 40, 31, 41), limit=1, max_pages=3)

    def test_mescere_uses_svg_geometry_when_feature_info_has_only_attributes(self):
        svg_response = Response()
        svg_response.status_code = 200
        svg_response.headers["Content-Type"] = "image/svg+xml"
        svg_response._content = b"""
        <svg xmlns="http://www.w3.org/2000/svg">
          <path fill="#008000" d="M -150 -150 L -120 -150 L -120 -120 L -150 -120 Z"/>
        </svg>
        """
        info_response = Response()
        info_response.status_code = 200
        info_response.headers["Content-Type"] = "application/json"
        info_response._content = json.dumps(
            {
                "features": [
                    {
                        "properties": {"OBJECTID": 42, "MES_TIP": "forest"},
                    }
                ]
            }
        ).encode()
        client = OgmMescereWmsClient("https://example.test/wms")
        responses = iter([svg_response, info_response])
        client.get = lambda *args, **kwargs: next(responses)

        features = client.query_bbox((30, 40, 31, 41), width=600, height=600)

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].source_id, "42")
        self.assertEqual(features[0].geometry["type"], "Polygon")
        self.assertGreaterEqual(len(features[0].geometry["coordinates"][0]), 4)

    def test_mescere_merges_svg_parts_for_same_object_id(self):
        svg_response = Response()
        svg_response.status_code = 200
        svg_response.headers["Content-Type"] = "image/svg+xml"
        svg_response._content = b"""
        <svg xmlns="http://www.w3.org/2000/svg">
          <path fill="#008000" d="M -150 -150 L -120 -150 L -120 -120 L -150 -120 Z"/>
          <path fill="#008000" d="M 120 120 L 150 120 L 150 150 L 120 150 Z"/>
        </svg>
        """
        info_payload = {
            "features": [
                {
                    "properties": {"OBJECTID": 42, "MES_TIP": "forest"},
                }
            ]
        }
        info_response_one = Response()
        info_response_one.status_code = 200
        info_response_one.headers["Content-Type"] = "application/json"
        info_response_one._content = json.dumps(info_payload).encode()
        info_response_two = Response()
        info_response_two.status_code = 200
        info_response_two.headers["Content-Type"] = "application/json"
        info_response_two._content = json.dumps(info_payload).encode()
        client = OgmMescereWmsClient("https://example.test/wms")
        responses = iter([svg_response, info_response_one, info_response_two])
        client.get = lambda *args, **kwargs: next(responses)

        features = client.query_bbox((30, 40, 31, 41), width=600, height=600)

        self.assertEqual(len(features), 1)
        self.assertEqual(features[0].source_id, "42")
        self.assertEqual(features[0].geometry["type"], "MultiPolygon")
        self.assertEqual(len(features[0].geometry["coordinates"]), 2)

    def test_mescere_rejects_svg_with_too_many_paths(self):
        svg_response = Response()
        svg_response.status_code = 200
        svg_response.headers["Content-Type"] = "image/svg+xml"
        path = '<path fill="#008000" d="M -150 -150 L -120 -150 L -120 -120 L -150 -120 Z"/>'
        svg_response._content = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            + (path * (MAX_MESCERE_SVG_PATHS + 1))
            + "</svg>"
        ).encode()
        client = OgmMescereWmsClient("https://example.test/wms")
        client.get = lambda *args, **kwargs: svg_response

        with self.assertRaisesRegex(OgmServiceError, "split the bbox"):
            client.query_bbox((30, 40, 31, 41), width=600, height=600)


if __name__ == "__main__":
    unittest.main()
