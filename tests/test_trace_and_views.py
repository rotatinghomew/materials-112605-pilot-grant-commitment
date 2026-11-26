"""争议追溯与企业视图隔离。"""
import json
import unittest

from tests._helpers import FIXTURE, fixture_engine


class TraceAndViewTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.bundle = fixture_engine()
        self.engine.confirm_plan("PLAN-1", 1, "center:test")

    def test_trace_follows_money_to_provider_and_refund(self):
        e = self.engine
        e.apply_change("CH-1", "center:test")
        for rec in self.bundle["receipts"]:
            e.ingest_receipt(rec, "center:test")
        trace = e.trace_line("LI-1")
        events = [ev["event"] for ev in trace["events"]]
        self.assertIn("commitment_created", events)
        self.assertIn("commitment_legacy", events)
        self.assertIn("receipt_booked", events)
        # 未关账前回执不属于任何结算期。
        self.assertEqual(trace["settlement_periods"], [])
        transfers = {r["transfer"] for r in trace["receipts"]}
        self.assertEqual(transfers, {"TR-70", "TR-72"})

    def test_dispute_lifecycle(self):
        e = self.engine
        e.open_dispute("D-1", "fund:CITY", "LI-1 资格取消后的承担争议", subject_line="LI-1")
        row = e.db.execute("SELECT * FROM disputes WHERE id='D-1'").fetchone()
        self.assertEqual(row["status"], "open")
        e.resolve_dispute("D-1", "center:test")
        row = e.db.execute("SELECT * FROM disputes WHERE id='D-1'").fetchone()
        self.assertEqual(row["status"], "resolved")

    def test_company_dashboard_only_shows_own_voucher(self):
        dash = self.engine.company_dashboard("V-1")
        self.assertEqual(dash["company"], "KAI")
        plan = next(p for p in dash["plans"] if p["plan"] == "PLAN-1")
        li1 = next(l for l in plan["lines"] if l["line"] == "LI-1")
        # 企业只看到自己的应付与可委托状态，不出现其他出资方额度字段。
        self.assertEqual(li1["company_base_self_pay"], 11000)
        self.assertNotIn("sponsors", li1)
        self.assertNotIn("capacity", li1)
        self.assertEqual({p["plan"] for p in dash["plans"]}, {"PLAN-1"})

    def test_other_company_voucher_not_listed(self):
        dash = self.engine.company_dashboard("V-2")
        self.assertEqual({p["plan"] for p in dash["plans"]}, {"PLAN-2"})

    def test_trace_includes_settlement_period_after_close(self):
        e = self.engine
        # 使用夹具未占用的 transfer，避免与预载回执混淆。
        e.ingest_receipt({
            "id": "BK-80", "transfer": "TR-800", "line": "LI-3", "fund": "LAB",
            "direction": "pay", "state": "received", "amount": 20000,
            "happened": "2027-01-25T11:00:00+08:00",
            "arrived": "2027-01-25T13:20:00+08:00"}, "center:test")
        e.open_period("Q1", "2027-Q1")
        e.close_period("Q1", "center:test")
        trace = e.trace_line("LI-3")
        self.assertEqual(trace["settlement_periods"], ["Q1"])
        # 同一笔调整在追溯中能查到唯一结算期，不会跨期重复。
        self.assertEqual(len(trace["receipts"]), 1)
        self.assertEqual(trace["receipts"][0]["period_id"], "Q1")

    def test_fixture_file_is_unchanged_json(self):
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertIn("receipts", bundle)


if __name__ == "__main__":
    unittest.main()
