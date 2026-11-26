"""并行办理：两个计划同时竞争同一笔季度额度，只有一个能拿到足额组合。"""
import threading
import unittest

from voucher.api import Service, make_handler
from voucher.store import DomainError, connect
from voucher.engine import Engine
from tests._helpers import fresh_engine

from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json


def build_two_plans(engine):
    engine.load_bundle({
        "funds": [{"id": "F1", "quarter": "2027-Q1", "capacity": 100000}],
        "vouchers": [
            {"id": "VA", "company": "KAI", "quarter": "2027-Q1",
             "sponsors": ["F1"], "use": "甲"},
            {"id": "VB", "company": "FEN", "quarter": "2027-Q1",
             "sponsors": ["F1"], "use": "乙"}]})
    engine.submit_plan("PA", "VA", "TERRA", [
        {"id": "LA1", "fee": 80000, "self_min": 10000, "exclusive": False,
         "limits": {"F1": 70000}, "after": []}])
    engine.submit_plan("PB", "VB", "TERRA", [
        {"id": "LB1", "fee": 60000, "self_min": 10000, "exclusive": False,
         "limits": {"F1": 50000}, "after": []}])
    engine.add_approval("AA", "PA", 1, "F1", {"LA1": 70000}, "fund:F1")
    engine.add_approval("AB", "PB", 1, "F1", {"LB1": 50000}, "fund:F1")


class EngineConcurrencyTests(unittest.TestCase):
    def test_parallel_confirm_only_one_wins(self):
        engine = fresh_engine()
        build_two_plans(engine)
        outcomes = {}

        def confirm(plan, name):
            try:
                engine.confirm_plan(plan, 1, f"center:{name}")
                outcomes[name] = "confirmed"
            except DomainError as err:
                outcomes[name] = err.code

        t1 = threading.Thread(target=confirm, args=("PA", "a"))
        t2 = threading.Thread(target=confirm, args=("PB", "b"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(sorted(outcomes.values()),
                         ["capacity_exhausted", "confirmed"])
        # 容量只被胜出方占用一次，绝不会把同一额度重复计给两个计划。
        cap = engine.fund_capacity("F1", "2027-Q1")
        self.assertEqual(cap["active"], 70000)
        self.assertEqual(cap["available"], 30000)
        winners = [p for p in ("PA", "PB")
                   if engine.get_plan(p)["status"] == "confirmed"]
        self.assertEqual(len(winners), 1)


class HttpConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        build_two_plans(Engine(self.db))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(Service(self.db)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_http_parallel_confirm_serialized_by_write_lock(self):
        results = []

        def post(plan):
            req = Request(f"{self.base}/v1/plans/{plan}/confirm/1",
                          data=b"{}",
                          headers={"X-Actor": "center:C1",
                                   "Content-Type": "application/json"},
                          method="POST")
            try:
                with urlopen(req, timeout=5) as resp:
                    results.append(json.loads(resp.read().decode())["status"])
            except HTTPError as err:
                results.append(json.loads(err.read().decode())["code"])

        threads = [threading.Thread(target=post, args=(p,)) for p in ("PA", "PB")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["capacity_exhausted", "confirmed"])


if __name__ == "__main__":
    unittest.main()
