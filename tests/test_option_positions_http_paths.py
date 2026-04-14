"""Regression tests for scripts/option_positions.py HTTP write paths."""

import unittest
from unittest.mock import patch

from scripts import option_positions as op


class OptionPositionsHttpPathsTest(unittest.TestCase):
    def test_bitable_create_record_uses_http_json(self):
        calls = []

        def fake_http_json(method, url, payload, headers=None, **kwargs):
            calls.append((method, url, payload, headers))
            return {"code": 0, "data": {"record": {"record_id": "rec_create"}}}

        with patch("scripts.feishu_bitable.http_json", fake_http_json):
            data = op.bitable_create_record("tenant_tok", "app_tok", "tbl_tok", {"status": "open"})

        self.assertTrue(calls)
        method, url, payload, headers = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/apps/app_tok/tables/tbl_tok/records"))
        self.assertEqual(payload, {"fields": {"status": "open"}})
        self.assertEqual(headers, {"Authorization": "Bearer tenant_tok"})
        self.assertEqual(data, {"record": {"record_id": "rec_create"}})

    def test_bitable_update_record_uses_http_json(self):
        calls = []

        def fake_http_json(method, url, payload, headers=None, **kwargs):
            calls.append((method, url, payload, headers))
            return {"code": 0, "data": {"record": {"record_id": "rec_update"}}}

        with patch("scripts.feishu_bitable.http_json", fake_http_json):
            data = op.bitable_update_record("tenant_tok", "app_tok", "tbl_tok", "rec_123", {"status": "close"})

        self.assertTrue(calls)
        method, url, payload, headers = calls[0]
        self.assertEqual(method, "PUT")
        self.assertTrue(url.endswith("/apps/app_tok/tables/tbl_tok/records/rec_123"))
        self.assertEqual(payload, {"fields": {"status": "close"}})
        self.assertEqual(headers, {"Authorization": "Bearer tenant_tok"})
        self.assertEqual(data, {"record": {"record_id": "rec_update"}})


if __name__ == "__main__":
    unittest.main()
