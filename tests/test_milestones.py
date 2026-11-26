"""里程碑看板：足额承诺才能出工、前序工序、人工与设备回执分别到达。"""
import unittest

from voucher.store import DomainError
from tests._helpers import fresh_engine


def work(rec_id, line, milestone, principal="TERRA",
         actor="2027-01-20T09:00:00+08:00", arrived="2027-01-20T09:05:00+08:00"):
    return {"id": rec_id, "line": line, "milestone": milestone, "principal": principal,
            "actor_time": actor, "arrived": arrived}


class MilestoneTests(unittest.TestCase):
    def setUp(self):
        # 用独立计划，避免与夹具中预先登记的 work 记录互相干扰。
        self.engine = fresh_engine()
        e = self.engine
        e.load_bundle({
            "funds": [
                {"id": "CITY", "quarter": "2027-Q1", "capacity": 100000},
                {"id": "LAB", "quarter": "2027-Q1", "capacity": 100000}],
            "vouchers": [{"id": "V-M", "company": "KAI", "quarter": "2027-Q1",
                          "sponsors": ["CITY", "LAB"], "use": "里程碑联调"}],
        })
        e.submit_plan("PLAN-M", "V-M", "TERRA", [
            {"id": "M-1", "fee": 40000, "self_min": 8000, "exclusive": False,
             "limits": {"CITY": 20000, "LAB": 20000}, "after": []},
            {"id": "M-2", "fee": 20000, "self_min": 4000, "exclusive": False,
             "limits": {"LAB": 16000}, "after": ["M-1"]}])
        e.add_approval("AM-1", "PLAN-M", 1, "CITY", {"M-1": 16000}, "fund:CITY")
        e.add_approval("AM-2", "PLAN-M", 1, "LAB",
                       {"M-1": 16000, "M-2": 16000}, "fund:LAB")

    def test_unconfirmed_plan_cannot_start(self):
        board = {b["line"]: b for b in self.engine.work_board("PLAN-M")["board"]}
        self.assertFalse(board["M-1"]["can_start"])

    def test_full_combination_unblocks_first_line(self):
        self.engine.confirm_plan("PLAN-M", 1, "center:test")
        board = {b["line"]: b for b in self.engine.work_board("PLAN-M")["board"]}
        self.assertTrue(board["M-1"]["can_start"])
        # M-2 以 M-1 为前序，M-1 未验收前不可继续。
        self.assertFalse(board["M-2"]["can_start"])

    def test_manual_done_without_device_receipt_stays_pending(self):
        self.engine.confirm_plan("PLAN-M", 1, "center:test")
        self.engine.declare_work(work("WM-1", "M-1", "work_done"))
        board = {b["line"]: b for b in self.engine.work_board("PLAN-M")["board"]}
        self.assertEqual(board["M-1"]["stage"], "work_done")
        self.assertTrue(board["M-1"]["pending_device"])
        # 待查状态下后道工序仍不可开工。
        self.assertFalse(board["M-2"]["can_start"])

    def test_device_acceptance_advances_successor(self):
        self.engine.confirm_plan("PLAN-M", 1, "center:test")
        self.engine.declare_work(work("WM-1", "M-1", "work_done"))
        self.engine.declare_work(work("WM-2", "M-1", "device_accepted", principal="PROBE-8",
                                      actor="2027-01-20T10:00:00+08:00",
                                      arrived="2027-01-20T10:20:00+08:00"))
        board = {b["line"]: b for b in self.engine.work_board("PLAN-M")["board"]}
        self.assertEqual(board["M-1"]["stage"], "device_accepted")
        self.assertFalse(board["M-1"]["pending_device"])
        self.assertTrue(board["M-2"]["can_start"])

    def test_work_with_bad_milestone_rejected(self):
        with self.assertRaises(DomainError) as cm:
            self.engine.declare_work(work("WM-9", "M-1", "shipped"))
        self.assertEqual(cm.exception.code, "bad_milestone")

    def test_naive_datetime_rejected(self):
        bad = work("WM-9", "M-1", "work_done")
        bad["actor_time"] = "2027-01-20T09:00:00"
        with self.assertRaises(ValueError):
            self.engine.declare_work(bad)

    def test_half_combination_does_not_unblock_work(self):
        # 费用 50000、self_min 40000：企业至多可被资助 10000。
        # CITY 承诺 12000 会把企业自付压到 38000，低于下限，组合不能确认。
        self.engine.submit_plan("PLAN-H", "V-M", "TERRA", [
            {"id": "H-1", "fee": 50000, "self_min": 40000, "exclusive": False,
             "limits": {"CITY": 45000}, "after": []}])
        self.engine.add_approval("AH-1", "PLAN-H", 1, "CITY",
                                 {"H-1": 12000}, "fund:CITY")
        quote = self.engine.evaluate_version("PLAN-H", 1)
        self.assertFalse(quote["confirmable"])
        with self.assertRaises(DomainError):
            self.engine.confirm_plan("PLAN-H", 1, "center:test")
        board = {b["line"]: b for b in self.engine.work_board("PLAN-H")["board"]}
        self.assertFalse(board["H-1"]["can_start"])


if __name__ == "__main__":
    unittest.main()
