"""回执入账、在途付款、防重复付款与结算期归属。"""
import unittest

from voucher.store import DomainError
from tests._helpers import fresh_engine


def make_engine():
    e = fresh_engine()
    e.load_bundle({
        "funds": [{"id": "LAB", "quarter": "2027-Q1", "capacity": 100000}],
        "vouchers": [{"id": "V-R", "company": "KAI", "quarter": "2027-Q1",
                      "sponsors": ["LAB"], "use": "回执联调"}],
    })
    e.submit_plan("PLAN-R", "V-R", "TERRA", [
        {"id": "R-1", "fee": 40000, "self_min": 8000, "exclusive": False,
         "limits": {"LAB": 32000}, "after": []},
        {"id": "R-2", "fee": 20000, "self_min": 4000, "exclusive": False,
         "limits": {"LAB": 16000}, "after": []}])
    e.add_approval("AR-1", "PLAN-R", 1, "LAB",
                   {"R-1": 32000, "R-2": 16000}, "fund:LAB")
    e.confirm_plan("PLAN-R", 1, "center:test")
    return e


def pay(transfer, line, fund, amount, happened, arrived, state="received", rec_id=None):
    return {"id": rec_id or f"BK-{transfer}", "transfer": transfer, "line": line,
            "fund": fund, "direction": "pay", "state": state, "amount": amount,
            "happened": happened, "arrived": arrived}


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def test_received_receipt_books_ledger_at_happened_time(self):
        self.engine.ingest_receipt(pay(
            "TR-90", "R-1", "LAB", 20000,
            "2027-01-20T09:00:00+08:00", "2027-01-20T09:10:00+08:00"), "center:test")
        self.assertEqual(self.engine.net_paid_to_provider("R-1"), 20000)
        row = self.engine.db.execute(
            "SELECT happened FROM ledger WHERE line_id='R-1'").fetchone()
        # 入账时点采用 happened（业务时间），不是 arrived。
        self.assertEqual(row["happened"], "2027-01-20T09:00:00+08:00")

    def test_duplicate_transfer_rejected(self):
        self.engine.ingest_receipt(pay(
            "TR-91", "R-1", "LAB", 1000,
            "2027-01-20T09:00:00+08:00", "2027-01-20T09:10:00+08:00"), "center:test")
        with self.assertRaises(DomainError) as cm:
            self.engine.ingest_receipt(pay(
                "TR-91", "R-1", "LAB", 1000,
                "2027-01-20T09:00:00+08:00", "2027-01-20T09:10:00+08:00",
                rec_id="BK-OTHER"), "center:test")
        self.assertEqual(cm.exception.code, "duplicate_transfer")

    def test_same_receipt_replay_is_idempotent(self):
        rec = pay("TR-92", "R-1", "LAB", 1000,
                  "2027-01-20T09:00:00+08:00", "2027-01-20T09:10:00+08:00")
        first = self.engine.ingest_receipt(rec, "center:test")
        second = self.engine.ingest_receipt(rec, "center:test")
        self.assertEqual(first, second)
        self.assertEqual(self.engine.db.execute(
            "SELECT COUNT(*) AS c FROM ledger WHERE line_id='R-1'").fetchone()["c"], 1)

    def test_rejected_receipt_does_not_book_ledger(self):
        self.engine.ingest_receipt(pay(
            "TR-93", "R-2", "LAB", 16000,
            "2027-01-20T09:00:00+08:00", "2027-01-20T09:10:00+08:00",
            state="rejected"), "center:test")
        self.assertEqual(self.engine.net_paid_to_provider("R-2"), 0)

    def test_dispatch_then_receipt_links_inflight(self):
        self.engine.dispatch_payment("TR-100", "R-2", "LAB", 16000, "center:test")
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM dispatches WHERE transfer='TR-100'").fetchone()["state"],
            "pending")
        self.engine.ingest_receipt(pay(
            "TR-100", "R-2", "LAB", 16000,
            "2027-01-21T09:00:00+08:00", "2027-01-21T09:30:00+08:00"), "center:test")
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM dispatches WHERE transfer='TR-100'").fetchone()["state"],
            "settled")

    def test_dispatch_rejected_marks_failed_and_can_be_resent(self):
        self.engine.dispatch_payment("TR-101", "R-2", "LAB", 16000, "center:test")
        self.engine.ingest_receipt(pay(
            "TR-101", "R-2", "LAB", 16000,
            "2027-01-21T09:00:00+08:00", "2027-01-21T09:30:00+08:00",
            state="rejected"), "center:test")
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM dispatches WHERE transfer='TR-101'").fetchone()["state"],
            "failed")
        # 按实际余量重新办理：新 transfer，不重复入账。
        self.engine.dispatch_payment("TR-102", "R-2", "LAB", 16000, "center:test")
        self.assertEqual(self.engine.net_paid_to_provider("R-2"), 0)

    def test_cannot_dispatch_more_than_line_fee(self):
        self.engine.ingest_receipt(pay(
            "TR-110", "R-2", "LAB", 15000,
            "2027-01-21T09:00:00+08:00", "2027-01-21T09:30:00+08:00"), "center:test")
        with self.assertRaises(DomainError) as cm:
            self.engine.dispatch_payment("TR-111", "R-2", "LAB", 16000, "center:test")
        self.assertEqual(cm.exception.code, "over_paid")

    def test_refund_reduces_net_paid(self):
        self.engine.ingest_receipt(pay(
            "TR-120", "R-2", "LAB", 16000,
            "2027-01-21T09:00:00+08:00", "2027-01-21T09:30:00+08:00"), "center:test")
        self.engine.ingest_receipt({
            "id": "BK-TR-121", "transfer": "TR-121", "line": "R-2", "fund": "LAB",
            "direction": "refund", "state": "received", "amount": 5000,
            "happened": "2027-01-22T09:00:00+08:00",
            "arrived": "2027-01-22T09:30:00+08:00"}, "center:test")
        self.assertEqual(self.engine.net_paid_to_provider("R-2"), 11000)

    def test_chargeback_and_cancel_follow_same_ingress_format(self):
        for direction, transfer in (("chargeback", "TR-130"), ("cancel", "TR-131")):
            self.engine.ingest_receipt({
                "id": f"BK-{transfer}", "transfer": transfer, "line": "R-2",
                "fund": "LAB", "direction": direction, "state": "received",
                "amount": 1000,
                "happened": "2027-01-23T09:00:00+08:00",
                "arrived": "2027-01-23T09:30:00+08:00"}, "center:test")
        self.assertEqual(self.engine.net_paid_to_provider("R-2"), -2000)


class PeriodTests(unittest.TestCase):
    def setUp(self):
        self.engine = make_engine()

    def test_each_ledger_entry_counts_in_only_one_period(self):
        e = self.engine
        e.ingest_receipt(pay("TR-200", "R-2", "LAB", 16000,
                             "2027-01-21T09:00:00+08:00",
                             "2027-01-21T09:30:00+08:00"), "center:test")
        e.open_period("Q1", "2027-Q1")
        first = e.close_period("Q1", "center:test")
        self.assertEqual(len(first["ledger_entries"]), 1)
        e.open_period("Q2", "2027-Q1")
        second = e.close_period("Q2", "center:test")
        # 同一笔调整不会在第二个结算期重复计入。
        self.assertEqual(second["ledger_entries"], [])
        summary = e.period_summary("Q1")
        self.assertEqual(summary["by_fund"]["LAB"]["pay"], 16000)

    def test_period_cannot_close_twice(self):
        self.engine.open_period("Q1", "2027-Q1")
        self.engine.close_period("Q1", "center:test")
        with self.assertRaises(DomainError) as cm:
            self.engine.close_period("Q1", "center:test")
        self.assertEqual(cm.exception.code, "period_closed")


if __name__ == "__main__":
    unittest.main()
