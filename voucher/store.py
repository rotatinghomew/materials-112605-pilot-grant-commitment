"""中试券办理后端的存储结构。

只依赖标准库 sqlite3。金额单位统一为人民币分。所有业务表都保留
《中试券申报交换》中的记录 id，跨表引用沿用同一套 id。
"""
from datetime import datetime, timezone
import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
    id TEXT PRIMARY KEY,
    quarter TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity >= 0)
);

CREATE TABLE IF NOT EXISTS vouchers (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    quarter TEXT NOT NULL,
    sponsors TEXT NOT NULL,
    use_text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    voucher TEXT NOT NULL REFERENCES vouchers(id),
    provider TEXT NOT NULL,
    lines TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    confirmed_version INTEGER,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS lines (
    id TEXT PRIMARY KEY,
    fee INTEGER NOT NULL CHECK (fee >= 0),
    self_min INTEGER NOT NULL CHECK (self_min >= 0),
    exclusive INTEGER NOT NULL,
    limits TEXT NOT NULL,
    after_lines TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_lines (
    plan_id TEXT NOT NULL REFERENCES plans(id),
    line_id TEXT NOT NULL REFERENCES lines(id),
    PRIMARY KEY (plan_id, line_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    version INTEGER NOT NULL,
    fund_id TEXT NOT NULL REFERENCES funds(id),
    shares_json TEXT NOT NULL,
    signed TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    fund_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK (amount >= 0),
    status TEXT NOT NULL,
    superseded_by INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (plan_id, line_id, fund_id, version)
);

CREATE TABLE IF NOT EXISTS company_obligations (
    plan_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    base_amount INTEGER NOT NULL,
    makeup_amount INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_id, line_id)
);

CREATE TABLE IF NOT EXISTS work (
    id TEXT PRIMARY KEY,
    line_id TEXT NOT NULL REFERENCES lines(id),
    milestone TEXT NOT NULL,
    principal TEXT NOT NULL,
    actor_time TEXT NOT NULL,
    arrived TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    id TEXT PRIMARY KEY,
    fund_id TEXT NOT NULL,
    voucher_id TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    new_ceiling INTEGER NOT NULL,
    occurred TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_changes_voucher_fund ON changes(voucher_id, fund_id);

CREATE TABLE IF NOT EXISTS receipts (
    id TEXT PRIMARY KEY,
    transfer TEXT NOT NULL UNIQUE,
    line_id TEXT NOT NULL,
    fund_id TEXT,
    direction TEXT NOT NULL CHECK (direction IN ('pay','refund','cancel','chargeback')),
    state TEXT NOT NULL CHECK (state IN ('received','rejected','pending')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    happened TEXT NOT NULL,
    arrived TEXT NOT NULL,
    period_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_line ON receipts(line_id);
CREATE INDEX IF NOT EXISTS idx_receipts_transfer ON receipts(transfer);

CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer TEXT NOT NULL UNIQUE,
    line_id TEXT NOT NULL,
    fund_id TEXT,
    direction TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    requested_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id TEXT NOT NULL,
    fund_id TEXT,
    direction TEXT NOT NULL,
    amount INTEGER NOT NULL,
    ref_kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    happened TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_line ON ledger(line_id);

CREATE TABLE IF NOT EXISTS periods (
    id TEXT PRIMARY KEY,
    quarter TEXT NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS period_entries (
    period_id TEXT NOT NULL REFERENCES periods(id),
    ledger_id INTEGER NOT NULL REFERENCES ledger(id),
    PRIMARY KEY (period_id, ledger_id)
);

CREATE TABLE IF NOT EXISTS disputes (
    id TEXT PRIMARY KEY,
    opened_by TEXT NOT NULL,
    subject_line TEXT,
    reason TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id TEXT NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    at_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_line ON traces(line_id);
"""


class DomainError(Exception):
    """带错误码的业务拒绝，HTTP 层据此映射状态码。"""

    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def now_iso():
    """带时区的当前时刻（UTC 偏移形式，符合交换资料的时间约定）。"""
    return datetime.now(timezone.utc).isoformat()


def connect(path=":memory:"):
    # ThreadingHTTPServer 会在工作线程中访问连接；写操作由 api 层的全局锁串行化。
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    db.executescript(SCHEMA)
    return db


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(text):
    return json.loads(text)


def parse_time(text):
    """与 partner.parse_time 一致：业务时间必须带时区偏移。"""
    if not isinstance(text, str):
        raise ValueError("时间须为字符串")
    instant = datetime.fromisoformat(text)
    if instant.utcoffset() is None:
        raise ValueError("时间须包含时区偏移")
    return instant


def trace(db, line_id, event, payload, at_time=None):
    db.execute(
        "INSERT INTO traces (line_id, event, payload_json, at_time) VALUES (?,?,?,?)",
        (line_id, event, dumps(payload), at_time or now_iso()),
    )
