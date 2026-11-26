"""核对交换资料中的实体引用与原始回执顺序。"""
from datetime import datetime
import json
from pathlib import Path
import unittest

from partner import parse_time as time

ROOT = Path(__file__).resolve().parent


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures" / "exchange.json").read_text(encoding="utf-8"))

    def test_record_identity(self):
        for name, collection in self.data.items():
            with self.subTest(collection=name):
                ids = [rec["id"] for rec in collection]
                self.assertTrue(ids)
                self.assertTrue(all(isinstance(i, str) and i for i in ids))
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(set(x) == set(collection[0]) for x in collection))

    def test_receipt_clocks(self):
        for rec in self.data["receipts"]:
            with self.subTest(receipt=rec["id"]):
                self.assertLessEqual(time(rec["happened"]), time(rec["arrived"]))
        with self.assertRaises(ValueError):
            time(datetime(2027, 1, 18).isoformat())

    def test_related_records(self):
        def row(collection, key):
            return next(rec for rec in self.data[collection] if rec["id"] == key)
        plan = row("plans","PLAN-1")
        agreement = row("approvals","A-1")
        assert set(agreement["lines"]) <= set(plan["lines"])
        assert row("vouchers",plan["voucher"])["company"] == "KAI"
        assert row("lines","LI-2")["after"] == ["LI-1"]
        assert row("approvals","A-3")["version"] != row("approvals","A-4")["version"]
        assert row("receipts","BK-1")["line"] == "LI-1"
        assert time(row("work","SAT-10")["arrived"]) < time(row("work","SAT-11")["arrived"])
        correction = time(row("changes","CH-1")["occurred"])
        assert time(row("receipts","BK-1")["happened"]) < correction < time(row("receipts","BK-1")["arrived"])
