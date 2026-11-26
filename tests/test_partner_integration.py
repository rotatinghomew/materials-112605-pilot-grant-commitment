"""与 partner.py 回执回放服务的端到端集成（默认跳过，显式开启）。

运行方式：
    python3 tests/test_partner_integration.py
或设置 RUN_PARTNER_TESTS=1 后随 unittest 一起执行。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote
from urllib.request import urlopen

from voucher.engine import Engine
from voucher.store import connect

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(os.environ.get("RUN_PARTNER_TESTS"), "需要 partner.py 联调环境")
class PartnerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.journal = os.path.join(cls.tmp, "ingress.db")
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "partner.py"),
             "--port", "0", "--journal", cls.journal],
            cwd=ROOT, stdout=subprocess.PIPE, text=True)
        line = cls.proc.stdout.readline().strip()
        cls.base = line
        cls.engine = Engine(connect(":memory:"))
        with open(os.path.join(ROOT, "fixtures", "exchange.json"),
                  encoding="utf-8") as fh:
            bundle = json.load(fh)
        cls.engine.load_bundle(bundle)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)

    def _fetch(self, at):
        with urlopen(f"{self.base}/v1/receipts?at={quote(at)}", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))["receipts"]

    def test_fetch_then_ingest_books_only_received(self):
        receipts = self._fetch("2027-01-18T23:59:00+08:00")
        transfers = {r["transfer"] for r in receipts}
        self.assertIn("TR-70", transfers)
        for rec in receipts:
            self.engine.ingest_receipt(rec, "center:integration")
        # BK-1 收到 26000，BK-2 被拒不入账，BK-3 退款 6000，净入账 20000。
        self.assertEqual(self.engine.net_paid_to_provider("LI-1"), 20000)
        self.assertEqual(self.engine.net_paid_to_provider("LI-2"), 0)

    def test_early_query_omits_later_arrivals(self):
        # arrived 晚于 at 的回执尚不可取得。
        early = self._fetch("2027-01-18T12:45:00+08:00")
        self.assertNotIn("TR-70", {r["transfer"] for r in early})

    def test_dispatch_is_journaled_by_partner(self):
        import sqlite3
        from urllib.request import Request
        payload = {"transfer": "TR-INT-1", "line": "LI-3", "fund": "LAB",
                   "direction": "pay", "amount": 20000}
        request = Request(f"{self.base}/v1/dispatches",
                          data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=5) as resp:
            self.assertEqual(resp.status, 202)
        with sqlite3.connect(self.journal) as db:
            rows = db.execute("SELECT requested FROM ingress").fetchall()
        self.assertTrue(any("TR-INT-1" in r[0] for r in rows))


if __name__ == "__main__":
    os.environ["RUN_PARTNER_TESTS"] = "1"
    unittest.main(verbosity=2)
