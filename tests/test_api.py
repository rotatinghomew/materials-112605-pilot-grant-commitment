"""HTTP 层：鉴权、角色边界、进件格式与隔离。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from voucher.api import Service, make_handler
from voucher.store import connect
from tests._helpers import FIXTURE


class ApiCase(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Service(self.db)))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, payload=None, actor=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if actor:
            headers["X-Actor"] = actor
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def load_fixture(self, actor="center:CENTER"):
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        status, body = self.call("POST", "/v1/exchange/load", bundle, actor)
        self.assertEqual(status, 200, body)
        return bundle

    def test_health(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_actor_required(self):
        status, body = self.call("GET", "/v1/funds/CITY/capacity?quarter=2027-Q1")
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], "unauthorized")

    def test_role_boundary(self):
        self.load_fixture()
        # 企业不能替出资方登记认可（先用能通过校验的金额，证明拒绝来自角色而非数据）。
        status, body = self.call("POST", "/v1/approvals", {
            "id": "A-Z", "plan": "PLAN-1", "version": 5, "fund": "CITY",
            "lines": {"LI-1": 1000}}, "company:KAI")
        self.assertEqual(status, 403)
        self.assertEqual(body["code"], "forbidden")
        # 机构不能关结算期。
        status, body = self.call("POST", "/v1/periods/Q1/close", {}, "provider:TERRA")
        self.assertEqual(status, 403)

    def test_full_flow_over_http(self):
        self.load_fixture()
        status, quote = self.call("GET", "/v1/plans/PLAN-1/quotes/1",
                                  actor="company:KAI")
        self.assertEqual(status, 200)
        self.assertTrue(quote["confirmable"])
        status, confirmed = self.call("POST", "/v1/plans/PLAN-1/confirm/1", {},
                                      "center:CENTER")
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")

        status, board = self.call("GET", "/v1/plans/PLAN-1/work-board",
                                  actor="provider:TERRA")
        self.assertEqual(status, 200)
        by_line = {b["line"]: b for b in board["board"]}
        # 夹具中 LI-1 已有设备验收回执；确认后其阶段如实呈现，LI-3 受前序约束。
        self.assertEqual(by_line["LI-1"]["stage"], "device_accepted")
        self.assertTrue(by_line["LI-1"]["covered"])
        self.assertIn("stage", by_line["LI-2"])

    def test_receipt_ingress_uses_exchange_format(self):
        self.load_fixture()
        receipt = {
            "id": "BK-9", "transfer": "TR-900", "line": "LI-1", "fund": "CITY",
            "direction": "pay", "state": "received", "amount": 26000,
            "happened": "2027-01-21T11:00:00+08:00",
            "arrived": "2027-01-21T13:20:00+08:00"}
        status, body = self.call("POST", "/v1/receipts", receipt, "provider:TERRA")
        self.assertEqual(status, 200)
        self.assertTrue(body["booked"])
        status, recon = self.call("GET", "/v1/lines/LI-1/reconciliation",
                                  actor="center:CENTER")
        self.assertEqual(status, 200)
        self.assertEqual(recon["net_paid_to_provider"], 26000)

    def test_company_cannot_see_other_voucher_dashboard(self):
        self.load_fixture()
        status, body = self.call("GET", "/v1/vouchers/V-2/dashboard",
                                 actor="company:KAI")
        self.assertEqual(status, 403)
        status, own = self.call("GET", "/v1/vouchers/V-1/dashboard",
                                actor="company:KAI")
        self.assertEqual(status, 200)
        self.assertEqual(own["company"], "KAI")

    def test_unknown_route_and_bad_json(self):
        status, body = self.call("GET", "/v1/nope", actor="center:CENTER")
        self.assertEqual(status, 404)
        req = Request(self.base + "/v1/receipts", data=b"not-json",
                      headers={"X-Actor": "center:CENTER",
                               "Content-Type": "application/json"}, method="POST")
        try:
            urlopen(req, timeout=5)
            self.fail("应当返回 400")
        except HTTPError as err:
            self.assertEqual(err.code, 400)

    def test_capacity_endpoint(self):
        self.load_fixture()
        status, body = self.call("GET", "/v1/funds/CITY/capacity?quarter=2027-Q1",
                                 actor="fund:CITY")
        self.assertEqual(status, 200)
        self.assertEqual(body["gross"], 78000)
        self.assertEqual(body["available"], 78000)


if __name__ == "__main__":
    unittest.main()
