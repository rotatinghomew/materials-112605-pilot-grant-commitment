"""线上办理 HTTP 服务（仅标准库）。

鉴权采用请求头 X-Actor: role:subject，角色为 company/provider/fund/center。
对手方的付款、撤销和冲回记录沿用《中试券申报交换》的 receipts 进件格式，
通过 POST /v1/receipts 办理；POST /v1/dispatches 对应本地向对手方发起的付款。
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import threading
from urllib.parse import parse_qs, urlsplit

from .engine import Engine
from .store import DomainError, now_iso

ROLES = ("company", "provider", "fund", "center")

# 并行办理靠这把写锁串行化“校验额度→落承诺”，避免两个计划读到同一余量。
WRITE_LOCK = threading.RLock()


class Actor:
    def __init__(self, role, subject):
        self.role = role
        self.subject = subject

    @property
    def label(self):
        return f"{self.role}:{self.subject}"


def parse_actor(header):
    if not header or ":" not in header:
        raise DomainError("unauthorized", "请通过 X-Actor 提供 role:subject 身份", 401)
    role, subject = header.split(":", 1)
    if role not in ROLES or not subject:
        raise DomainError("unauthorized", f"角色须为 {ROLES} 之一且带主体", 401)
    return Actor(role, subject)


def require(actor, *roles):
    if actor.role not in roles:
        raise DomainError("forbidden", f"角色 {actor.role} 无权办理此操作", 403)


class Service:
    """把路由绑定到引擎，HTTP 层只做解析与鉴权。"""

    def __init__(self, db):
        self.engine = Engine(db)

    # ------------------------------------------------------------ 辅助
    @staticmethod
    def _body(handler):
        length = int(handler.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        try:
            if length > 1_048_576:
                raise ValueError
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (ValueError, UnicodeError, json.JSONDecodeError):
            raise DomainError("bad_request", "请求须为 JSON 对象", 400)

    def route(self, method, path, query, body, actor):
        e = self.engine
        if method == "GET" and path == "/health":
            return {"status": "ok"}

        if method == "POST" and path == "/v1/exchange/load":
            require(actor, "center")
            return e.load_bundle(body, actor.label)

        if method == "POST" and path == "/v1/plans":
            require(actor, "company", "provider")
            return e.submit_plan(body["id"], body["voucher"], body["provider"], body["lines"])

        m = re.fullmatch(r"/v1/plans/([^/]+)/quotes/(\d+)", path)
        if method == "GET" and m:
            return e.evaluate_version(m.group(1), int(m.group(2)))

        if method == "POST" and path == "/v1/approvals":
            require(actor, "fund")
            return e.add_approval(body["id"], body["plan"], body["version"],
                                  body["fund"], body["lines"], actor.label)

        m = re.fullmatch(r"/v1/plans/([^/]+)/confirm/(\d+)", path)
        if method == "POST" and m:
            require(actor, "center", "company")
            return e.confirm_plan(m.group(1), int(m.group(2)), actor.label)

        if method == "POST" and path == "/v1/work":
            require(actor, "provider")
            return e.declare_work(body)

        m = re.fullmatch(r"/v1/plans/([^/]+)/work-board", path)
        if method == "GET" and m:
            require(actor, "provider", "center", "company")
            return e.work_board(m.group(1))

        if method == "POST" and path == "/v1/changes":
            require(actor, "fund", "center")
            rec = dict(body)
            # 进件即登记；办理效果由 /apply 显式触发。
            rec.setdefault("occurred", now_iso())
            with e.db:
                e._insert_change(rec)
            return {"change": rec["id"], "registered": True, "applied": False}

        m = re.fullmatch(r"/v1/changes/([^/]+)/apply", path)
        if method == "POST" and m:
            require(actor, "fund", "center")
            return e.apply_change(m.group(1), actor.label)

        m = re.fullmatch(r"/v1/plans/([^/]+)/lines/([^/]+)/reprocess/(\d+)", path)
        if method == "POST" and m:
            require(actor, "center", "fund")
            return e.reprocess_line(m.group(1), m.group(2), int(m.group(3)), actor.label)

        if method == "POST" and path == "/v1/receipts":
            require(actor, "center", "provider", "fund")
            return e.ingest_receipt(body, actor.label)

        if method == "POST" and path == "/v1/dispatches":
            require(actor, "center", "fund", "company")
            return e.dispatch_payment(body["transfer"], body["line"], body.get("fund"),
                                      body["amount"], actor.label)

        m = re.fullmatch(r"/v1/lines/([^/]+)/reconciliation", path)
        if method == "GET" and m:
            require(actor, "center", "provider", "fund")
            return e.line_reconciliation(m.group(1))

        m = re.fullmatch(r"/v1/lines/([^/]+)/trace", path)
        if method == "GET" and m:
            require(actor, "center", "fund", "provider", "company")
            return e.trace_line(m.group(1))

        if method == "POST" and path == "/v1/periods":
            require(actor, "center")
            return e.open_period(body["id"], body["quarter"])

        m = re.fullmatch(r"/v1/periods/([^/]+)/close", path)
        if method == "POST" and m:
            require(actor, "center")
            return e.close_period(m.group(1), actor.label)

        m = re.fullmatch(r"/v1/periods/([^/]+)", path)
        if method == "GET" and m:
            require(actor, "center", "fund")
            return e.period_summary(m.group(1))

        if method == "POST" and path == "/v1/disputes":
            require(actor, "fund", "company", "provider")
            return e.open_dispute(body["id"], actor.label, body["reason"],
                                  body.get("subject_line"))

        m = re.fullmatch(r"/v1/disputes/([^/]+)/resolve", path)
        if method == "POST" and m:
            require(actor, "center")
            return e.resolve_dispute(m.group(1), actor.label)

        m = re.fullmatch(r"/v1/funds/([^/]+)/capacity", path)
        if method == "GET" and m:
            require(actor, "center", "fund")
            quarter = query.get("quarter", [None])[0]
            if not quarter:
                raise DomainError("bad_request", "请提供 quarter 参数", 400)
            return e.fund_capacity(m.group(1), quarter)

        m = re.fullmatch(r"/v1/vouchers/([^/]+)/dashboard", path)
        if method == "GET" and m:
            require(actor, "company", "center")
            voucher = e.get_voucher(m.group(1))
            if actor.role == "company" and actor.subject != voucher["company"]:
                raise DomainError("forbidden", "只能查看本企业持券的资料", 403)
            return e.company_dashboard(m.group(1))

        raise DomainError("not_found", "路径不存在", 404)


def make_handler(service):
    class ApiHandler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self):
            target = urlsplit(self.path)
            try:
                if target.path == "/health":
                    self._send(200, service.route("GET", "/health", {}, {}, None))
                    return
                actor = parse_actor(self.headers.get("X-Actor", ""))
                body = Service._body(self) if self.command == "POST" else {}
                if self.command == "POST":
                    with WRITE_LOCK:
                        result = service.route(self.command, target.path,
                                               parse_qs(target.query), body, actor)
                else:
                    result = service.route(self.command, target.path,
                                           parse_qs(target.query), body, actor)
                self._send(200, result)
            except DomainError as err:
                self._send(err.status, {"code": err.code, "message": err.message})
            except KeyError as err:
                self._send(400, {"code": "missing_field", "message": f"缺少字段 {err.args[0]}"})
            except (TypeError, ValueError) as err:
                self._send(400, {"code": "bad_request", "message": str(err)})

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format, *args):
            pass

    return ApiHandler
