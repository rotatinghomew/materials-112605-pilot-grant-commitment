"""证据更正：解除未开工承诺、遗留已完成项、接替重办与追还。"""
import unittest

from voucher.store import DomainError
from tests._helpers import fixture_engine, fresh_engine


def receipt(rec_id, transfer, line, fund, direction, state, amount,
            happened, arrived):
    return {"id": rec_id, "transfer": transfer, "line": line, "fund": fund,
            "direction": direction, "state": state, "amount": amount,
            "happened": happened, "arrived": arrived}


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.bundle = fixture_engine()
        # v1 足额组合确认 PLAN-1，占用 CITY 71000 / LAB 39000。
        self.engine.confirm_plan("PLAN-1", 1, "center:test")

    def test_change_releases_not_started_and_keeps_started_legacy(self):
        result = self.engine.apply_change("CH-1", "center:test")
        released = {(r["line"], r["fund"]): r["amount"] for r in result["released"]}
        legacy = {(r["line"], r["fund"]): r["amount"] for r in result["legacy"]}
        # LI-2 未开工：45000 承诺解除。
        self.assertEqual(released, {("LI-2", "CITY"): 45000})
        # LI-1 已完成人工与设备验收：26000 转为遗留，仍须找到付款人。
        self.assertEqual(legacy, {("LI-1", "CITY"): 26000})
        cap = self.engine.fund_capacity("CITY", "2027-Q1")
        # LI-1 的 CITY 承诺转为遗留仍占额度；LI-2 解除后 active 归零。
        active_rows = self.engine.db.execute(
            "SELECT line_id, amount FROM commitments WHERE fund_id='CITY' AND status='active'"
        ).fetchall()
        self.assertEqual([(r["line_id"], r["amount"]) for r in active_rows], [])
        self.assertEqual(cap["active"], 0)
        self.assertEqual(cap["legacy"], 26000)
        self.assertEqual(cap["available"], 78000 - 26000)

    def test_change_is_idempotent(self):
        self.engine.apply_change("CH-1", "center:test")
        with self.assertRaises(DomainError) as cm:
            self.engine.apply_change("CH-1", "center:test")
        self.assertEqual(cm.exception.code, "change_applied")

    def test_applied_ceiling_allows_takeover_within_declared_scope(self):
        # CH-2 办理前，POOL 不在 LI-1 的 limits 内，登记认可会被项目上限拒绝。
        with self.assertRaises(DomainError):
            self.engine.add_approval("A-P9", "PLAN-1", 2, "POOL",
                                     {"LI-1": 15000}, "fund:POOL")
        self.engine.apply_change("CH-2", "center:test")
        # 办理后新上限 15000 生效；夹具中的 A-5 正是 POOL 在版本 2 的接替认可。
        self.assertEqual(self.engine.effective_ceiling("V-1", "POOL", "LI-1"), 15000)
        self.engine.add_approval("A-P10", "PLAN-1", 3, "POOL",
                                 {"LI-1": 15000}, "fund:POOL")

    def _book_payments(self):
        # 机构在 LI-1 上收到 CITY 26000 后被追还 6000；LI-2 的 45000 付款被拒，机构未收款。
        for rec in self.bundle["receipts"]:
            self.engine.ingest_receipt(rec, "center:test")

    def test_rejected_payment_leaves_no_ledger_and_no_provider_cash(self):
        self.engine.apply_change("CH-1", "center:test")
        self._book_payments()
        recon = self.engine.line_reconciliation("LI-2")
        self.assertEqual(recon["net_paid_to_provider"], 0)
        # 被拒的 45000 没有形成台账，也不占用在途。
        self.assertEqual(recon["ledger"], [])

    def test_takeover_reprocess_reallocates_completed_line(self):
        self.engine.apply_change("CH-1", "center:test")
        self.engine.apply_change("CH-2", "center:test")
        self._book_payments()
        # 追还 6000 已到但对账期未关：CITY 遗留承诺 26000 仍全额占用，不提前释放。
        cap_before = self.engine.fund_capacity("CITY", "2027-Q1")
        self.assertEqual(cap_before["available"], 78000 - 26000)

        result = self.engine.reprocess_line("PLAN-1", "LI-1", 2, "center:test")
        # 新方案：LAB 19000（结转）+ POOL 15000（接替），企业补缴 11000。
        self.assertEqual(result["active_sponsor_total"], 34000)
        self.assertEqual(result["company_obligation"], 22000)
        self.assertEqual(result["company_makeup"], 11000)
        self.assertTrue(result["covered"])
        # CITY 机构净入账 20000，新方案下 CITY 最终承担 0，仍须追还 20000。
        due = {d["fund"]: d["amount"] for d in result["legacy_refund_due"]}
        self.assertEqual(due, {"CITY": 20000})

        recon = self.engine.line_reconciliation("LI-1")
        # 机构已净收 20000，费用 56000，剩余应付 36000，不会向机构重复付款。
        self.assertEqual(recon["net_paid_to_provider"], 20000)
        self.assertEqual(recon["balance_due_to_provider"], 36000)
        payable = recon["payable_by_fund"]
        self.assertEqual(payable.get("LAB"), 19000)
        self.assertEqual(payable.get("POOL"), 15000)

    def test_reprocess_requires_started_line(self):
        self.engine.apply_change("CH-1", "center:test")
        with self.assertRaises(DomainError) as cm:
            self.engine.reprocess_line("PLAN-1", "LI-2", 2, "center:test")
        self.assertEqual(cm.exception.code, "line_not_started")

    def test_takeover_cannot_exceed_its_own_ceiling(self):
        self.engine.apply_change("CH-1", "center:test")
        self.engine.apply_change("CH-2", "center:test")
        # POOL 只认可到 15000；超额认可在登记阶段即被拒，不能进入接替。
        with self.assertRaises(DomainError) as cm:
            self.engine.add_approval("A-P11", "PLAN-1", 4, "POOL",
                                     {"LI-1": 16000}, "fund:POOL")
        self.assertEqual(cm.exception.code, "above_ceiling")
        # 若通过直接留痕绕过登记，重办阶段仍会按 POOL 的认可上限拦截。
        self.engine.db.execute(
            "INSERT INTO approvals (id, plan_id, version, fund_id, shares_json, signed, pending) "
            "VALUES ('A-P12','PLAN-1',4,'POOL',?,?,'0')",
            ('{"LI-1": 16000}', "2027-01-19T08:00:00+08:00"))
        with self.assertRaises(DomainError) as cm:
            self.engine.reprocess_line("PLAN-1", "LI-1", 4, "center:test")
        self.assertEqual(cm.exception.code, "above_ceiling")

    def test_company_makeup_when_no_sponsor_takes_over(self):
        # 独立计划：取消资格后无人接替，缺口由企业补缴。
        engine = fresh_engine()
        engine.load_bundle({
            "funds": [{"id": "CITY", "quarter": "2027-Q1", "capacity": 100000}],
            "vouchers": [{"id": "V-9", "company": "NOR", "quarter": "2027-Q1",
                          "sponsors": ["CITY"], "use": "补缴联调"}]})
        engine.submit_plan("PLAN-9", "V-9", "TERRA", [
            {"id": "LI-9", "fee": 30000, "self_min": 5000, "exclusive": False,
             "limits": {"CITY": 25000}, "after": []}])
        engine.add_approval("A-9", "PLAN-9", 1, "CITY", {"LI-9": 25000}, "fund:CITY")
        engine.confirm_plan("PLAN-9", 1, "center:test")
        engine.declare_work({"id": "W-9", "line": "LI-9", "milestone": "device_accepted",
                             "principal": "PROBE-1",
                             "actor_time": "2027-01-20T09:00:00+08:00",
                             "arrived": "2027-01-20T09:10:00+08:00"})
        engine.db.execute(
            "INSERT INTO changes (id, fund_id, voucher_id, scope_json, new_ceiling, occurred) "
            "VALUES ('CH-9','CITY','V-9',?,0,'2027-01-20T10:00:00+08:00')",
            ('["LI-9"]',))
        engine.apply_change("CH-9", "center:test")
        # 没有任何新版认可：以原版本重办时企业须把 25000 缺口全部补上（自付合计 30000）。
        result = engine.reprocess_line("PLAN-9", "LI-9", 1, "center:test")
        self.assertEqual(result["active_sponsor_total"], 0)
        self.assertEqual(result["company_makeup"], 25000)
        self.assertEqual(result["company_obligation"], 30000)
        self.assertTrue(result["covered"])

    def test_refund_holds_capacity_until_reconciliation_then_releases(self):
        self.engine.apply_change("CH-1", "center:test")
        self.engine.apply_change("CH-2", "center:test")
        self._book_payments()
        self.engine.reprocess_line("PLAN-1", "LI-1", 2, "center:test")
        self.engine.open_period("P-2027Q1", "2027-Q1")
        # 关账前 CITY 额度仍被遗留承诺 26000 占用。
        self.assertEqual(self.engine.fund_capacity("CITY", "2027-Q1")["available"],
                         78000 - 26000)
        closed = self.engine.close_period("P-2027Q1", "center:test")
        # 已入账：pay 26000、refund 6000，净 20000；遗留承诺按净额保留 20000。
        adj = {(a["line"], a["fund"]): a for a in closed["legacy_adjustments"]}
        self.assertEqual(adj[("LI-1", "CITY")]["amount"], 20000)
        cap = self.engine.fund_capacity("CITY", "2027-Q1")
        # CITY 在 PLAN-1 的 LI-2 承诺此前已解除，active 归零；遗留按净额保留 20000。
        self.assertEqual(cap["active"], 0)
        self.assertEqual(cap["legacy"], 20000)
        self.assertEqual(cap["available"], 78000 - 20000)
        # 剩余 20000 追还到账并在下一期关账后，遗留承诺转为 refunded，额度释放。
        self.engine.ingest_receipt(receipt(
            "BK-4", "TR-73", "LI-1", "CITY", "refund", "received", 20000,
            "2027-02-02T10:00:00+08:00", "2027-02-02T10:30:00+08:00"), "center:test")
        self.engine.open_period("P-2027Q2", "2027-Q1")
        self.engine.close_period("P-2027Q2", "center:test")
        cap = self.engine.fund_capacity("CITY", "2027-Q1")
        self.assertEqual(cap["legacy"], 0)
        self.assertEqual(cap["available"], 78000)


if __name__ == "__main__":
    unittest.main()
