"""测试共用的构造工具。"""
import json
from pathlib import Path

from voucher.engine import Engine
from voucher.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "exchange.json"


def fresh_engine():
    return Engine(connect(":memory:"))


def fixture_engine(apply_changes=False, book_receipts=False):
    """载入合成交换资料；默认只登记，不自动办理更正与回执。"""
    engine = fresh_engine()
    bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
    engine.load_bundle(bundle)
    if apply_changes:
        for change in bundle["changes"]:
            engine.apply_change(change["id"], "center:test")
    if book_receipts:
        for receipt in bundle["receipts"]:
            engine.ingest_receipt(receipt, "center:test")
    return engine, bundle
