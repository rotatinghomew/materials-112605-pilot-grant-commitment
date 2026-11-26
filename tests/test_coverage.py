"""费用覆盖与计划确认规则：上限、self_min、exclusive、同版本、额度竞争。"""
import unittest

from voucher.store import DomainError
from tests._helpers import fixture_engine, fresh_engine


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.engine, self.bundle = fixture_engine()

    def test_quote_shows_each_party_share(self):
        # 夹具中 POOL 的 v2 认可依赖尚未办理的 CH-2，载入后挂起；
        # 因此 v1 是当前唯一生效版本，报价逐分项展示各方可能承担的金额。
        quote = self.engine.evaluate_version("PLAN-1", 1)
        self.assertTrue(quote["same_version"])
        by_line = {v["line"]: v for v in quote["lines"]}
        li1 = by_line["LI-1"]
        self.assertEqual(li1["sponsor_total"], 45000)
        self.assertEqual(li1["company_self_pay"], 11000)
        self.assertTrue(all(v["covered"] for v in quote["lines"]))
        self.assertTrue(quote["confirmable"])

    def test_self_min_floor_blocks_confirmation(self):
        # LI-3 费用 25000、self_min 5000；LAB 认可 21000 会使企业自付 4000，低于下限。
        # 该认可登记时不超过项目上限 20000 的部分由另一版本演示，这里直接构造计划数据。
        self.engine.db.execute(
            "INSERT INTO lines (id, fee, self_min, exclusive, limits, after_lines) "
            "VALUES ('LI-3B',25000,5000,0,?,?)",
            ('{"LAB": 22000}', '["LI-1"]'))
        self.engine.db.execute("INSERT INTO plan_lines (plan_id, line_id) VALUES ('PLAN-1','LI-3B')")
        self.engine.add_approval("A-X", "PLAN-1", 3, "LAB",
                                 {"LI-1": 19000, "LI-3B": 21000}, "fund:LAB")
        quote = self.engine.evaluate_version("PLAN-1", 3)
        li3b = next(v for v in quote["lines"] if v["line"] == "LI-3B")
        self.assertFalse(li3b["self_pay_ok"])
        self.assertFalse(quote["confirmable"])

    def test_exclusive_line_rejects_multiple_sponsors(self):
        # LI-2 为 exclusive：同一版本中 CITY 与 POOL 都出资即违规。
        self.engine.add_approval("A-P1", "PLAN-1", 9, "POOL", {"LI-2": 20000}, "fund:POOL")
        self.engine.add_approval("A-C1", "PLAN-1", 9, "CITY", {"LI-2": 25000}, "fund:CITY")
        quote = self.engine.evaluate_version("PLAN-1", 9)
        li2 = next(v for v in quote["lines"] if v["line"] == "LI-2")
        self.assertFalse(li2["covered"])
        self.assertTrue(any("exclusive" in r for r in li2["reasons"]))
        self.assertFalse(quote["confirmable"])

    def test_mixed_versions_cannot_confirm(self):
        # PLAN-2 的两张认可分属版本 1 与 2。
        quote_v2 = self.engine.evaluate_version("PLAN-2", 2)
        self.assertFalse(quote_v2["same_version"])
        self.assertFalse(quote_v2["confirmable"])
        with self.assertRaises(DomainError) as cm:
            self.engine.confirm_plan("PLAN-2", 2, "center:test")
        self.assertEqual(cm.exception.code, "not_confirmable")

    def test_approval_above_line_ceiling_rejected(self):
        with self.assertRaises(DomainError) as cm:
            self.engine.add_approval("A-BAD", "PLAN-1", 7, "CITY",
                                     {"LI-1": 36000}, "fund:CITY")
        self.assertEqual(cm.exception.code, "above_ceiling")

    def test_confirm_consumes_quarter_capacity_atomically(self):
        self.engine.confirm_plan("PLAN-1", 1, "center:test")
        cap_city = self.engine.fund_capacity("CITY", "2027-Q1")
        cap_lab = self.engine.fund_capacity("LAB", "2027-Q1")
        self.assertEqual(cap_city["active"], 71000)
        self.assertEqual(cap_city["available"], 7000)
        self.assertEqual(cap_lab["active"], 39000)
        self.assertEqual(cap_lab["available"], 12000)

    def test_second_plan_loses_capacity_race(self):
        # 用宽上限、紧容量的独立数据演示并行办理：项目上限都够，只剩季度容量竞争。
        e = fresh_engine()
        e.load_bundle({
            "funds": [{"id": "F1", "quarter": "2027-Q1", "capacity": 100000}],
            "vouchers": [
                {"id": "VA", "company": "KAI", "quarter": "2027-Q1",
                 "sponsors": ["F1"], "use": "甲"},
                {"id": "VB", "company": "FEN", "quarter": "2027-Q1",
                 "sponsors": ["F1"], "use": "乙"}]})
        e.submit_plan("PA", "VA", "TERRA", [
            {"id": "LA1", "fee": 80000, "self_min": 10000, "exclusive": False,
             "limits": {"F1": 70000}, "after": []}])
        e.submit_plan("PB", "VB", "TERRA", [
            {"id": "LB1", "fee": 60000, "self_min": 10000, "exclusive": False,
             "limits": {"F1": 50000}, "after": []}])
        e.add_approval("AA", "PA", 1, "F1", {"LA1": 70000}, "fund:F1")
        e.add_approval("AB", "PB", 1, "F1", {"LB1": 50000}, "fund:F1")
        e.confirm_plan("PA", 1, "center:test")
        # 基金容量 100000 已被占用 70000，第二笔 50000 无法再取得足额组合承诺。
        with self.assertRaises(DomainError) as cm:
            e.confirm_plan("PB", 1, "center:test")
        self.assertEqual(cm.exception.code, "capacity_exhausted")
        self.assertEqual(e.fund_capacity("F1", "2027-Q1")["available"], 30000)

    def test_half_commitment_is_not_confirmable(self):
        # PLAN-2 的 v2 只有 CITY 单张认可，LAB 未在同版本签认：
        # LI-5 完全没有资助，组合不足额。
        quote = self.engine.evaluate_version("PLAN-2", 2)
        li5 = next(v for v in quote["lines"] if v["line"] == "LI-5")
        self.assertEqual(li5["sponsor_total"], 0)
        self.assertFalse(li5["covered"])
        self.assertFalse(quote["confirmable"])

    def test_duplicate_confirmation_rejected(self):
        self.engine.confirm_plan("PLAN-1", 1, "center:test")
        with self.assertRaises(DomainError) as cm:
            self.engine.confirm_plan("PLAN-1", 1, "center:test")
        self.assertEqual(cm.exception.code, "already_confirmed")


if __name__ == "__main__":
    unittest.main()
