"""中试券联合出资与履约补缴的领域引擎。

规则以《中试券申报交换》项目条款为准：
- 资金覆盖上限按 line 计算，self_min 保留给企业；
- exclusive 的服务项至多由一个出资方承担资助部分；
- 计划内各出资方认可同一版本、且资金加企业确认的自付覆盖约定费用才可确认；
- 只有足额的组合承诺才能开工，半额分项承诺不构成出工条件；
- 证据更正后，未开工项解除受影响承诺，已完成项须重新落实付款人；
- 机构已收款的项目不重复付款；已发出的款项在对账明确前不释放为新券额度。
"""
import threading

from .store import DomainError, dumps, loads, now_iso, parse_time, trace


VALID_MILESTONES = ("not_started", "work_done", "device_accepted")
COLLECTIONS = {"funds", "vouchers", "plans", "lines", "approvals", "work", "changes", "receipts"}
VALID_DIRECTIONS = ("pay", "refund", "cancel", "chargeback")
VALID_RECEIPT_STATES = ("received", "rejected", "pending")

# 并行办理时串行化“校验余额→落承诺/台账”，杜绝同一额度被两个计划重复计入。
WRITE_LOCK = threading.RLock()
# 会改变状态的公开方法；其余方法（查询/报价/对账）不加写锁。
MUTATING_METHODS = frozenset({
    "load_bundle", "submit_plan", "add_approval", "confirm_plan", "declare_work",
    "apply_change", "reprocess_line", "ingest_receipt", "dispatch_payment",
    "open_period", "close_period", "open_dispute", "resolve_dispute",
})


class Engine:
    def __init__(self, db):
        self.db = db

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name in MUTATING_METHODS and callable(attr):
            def locked(*args, **kwargs):
                with WRITE_LOCK:
                    return attr(*args, **kwargs)
            return locked
        return attr

    # ------------------------------------------------------------------ 基础读取
    def _one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        if row is None:
            raise DomainError("not_found", "记录不存在", 404)
        return row

    def get_plan(self, plan_id):
        return self._one("SELECT * FROM plans WHERE id=?", (plan_id,))

    def get_line(self, line_id):
        return self._one("SELECT * FROM lines WHERE id=?", (line_id,))

    def get_voucher(self, voucher_id):
        return self._one("SELECT * FROM vouchers WHERE id=?", (voucher_id,))

    def plan_line_ids(self, plan_id):
        return [r["line_id"] for r in self.db.execute(
            "SELECT line_id FROM plan_lines WHERE plan_id=? ORDER BY rowid", (plan_id,))]

    def line_limits(self, line):
        return loads(line["limits"])

    def effective_ceiling(self, voucher_id, fund_id, line_id):
        """项目条款上限可被 changes 中的证据变化覆盖，取最近一次 occurred。"""
        line = self.get_line(line_id)
        base = self.line_limits(line).get(fund_id, 0)
        latest = None
        for ch in self.db.execute(
                "SELECT * FROM changes WHERE voucher_id=? AND fund_id=? AND applied=1 "
                "ORDER BY occurred",
                (voucher_id, fund_id)):
            if line_id in loads(ch["scope_json"]):
                latest = ch
        if latest is not None:
            return latest["new_ceiling"]
        return base

    def fund_capacity(self, fund_id, quarter):
        row = self.db.execute(
            "SELECT capacity FROM funds WHERE id=? AND quarter=?", (fund_id, quarter)).fetchone()
        if row is None:
            return {"gross": 0, "active": 0, "legacy": 0, "inflight": 0, "available": 0}
        gross = row["capacity"]
        # 生效承诺占用额度。
        active = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
            "WHERE fund_id=? AND status='active'", (fund_id,)).fetchone()["s"]
        # 已开工/已完成后被取消资格的遗留承诺：款项在对账明确（关账）前不释放。
        legacy = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
            "WHERE fund_id=? AND status='legacy'", (fund_id,)).fetchone()["s"]
        # 已发出但尚未收到回执的付款（信息项；基金付款均有承诺支撑，不重复扣减）。
        inflight = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM dispatches "
            "WHERE fund_id=? AND direction='pay' AND state='pending'", (fund_id,)).fetchone()["s"]
        available = gross - active - legacy
        return {"gross": gross, "active": active, "legacy": legacy,
                "inflight": inflight, "available": available}

    # ------------------------------------------------------------------ 交换资料载入
    def load_bundle(self, bundle, actor="system"):
        """按《中试券申报交换》的进件格式整体载入。

        approvals/work 直接成为本地事实；changes/receipts 只登记原文，
        业务效果分别由 apply_change / ingest_receipt 显式办理——读到回执
        不代表本地已做过相应操作。
        """
        unknown = set(bundle) - COLLECTIONS
        if unknown:
            raise DomainError("unknown_collection", f"未知集合: {sorted(unknown)}", 400)
        loaded = []
        with self.db:
            for rec in bundle.get("funds", []):
                if self.db.execute("SELECT 1 FROM funds WHERE id=?", (rec["id"],)).fetchone():
                    continue
                self.db.execute("INSERT INTO funds (id, quarter, capacity) VALUES (?,?,?)",
                                (rec["id"], rec["quarter"], rec["capacity"]))
                loaded.append(("funds", rec["id"]))
            for rec in bundle.get("vouchers", []):
                if self.db.execute("SELECT 1 FROM vouchers WHERE id=?", (rec["id"],)).fetchone():
                    continue
                self.db.execute(
                    "INSERT INTO vouchers (id, company, quarter, sponsors, use_text) VALUES (?,?,?,?,?)",
                    (rec["id"], rec["company"], rec["quarter"],
                     dumps(rec["sponsors"]), rec["use"]))
                loaded.append(("vouchers", rec["id"]))
            for rec in bundle.get("lines", []):
                if self.db.execute("SELECT 1 FROM lines WHERE id=?", (rec["id"],)).fetchone():
                    continue
                self.db.execute(
                    "INSERT INTO lines (id, fee, self_min, exclusive, limits, after_lines) "
                    "VALUES (?,?,?,?,?,?)",
                    (rec["id"], rec["fee"], rec["self_min"], 1 if rec["exclusive"] else 0,
                     dumps(rec["limits"]), dumps(rec["after"])))
                loaded.append(("lines", rec["id"]))
            for rec in bundle.get("plans", []):
                if self.db.execute("SELECT 1 FROM plans WHERE id=?", (rec["id"],)).fetchone():
                    continue
                voucher = self.get_voucher(rec["voucher"])
                for line_id in rec["lines"]:
                    self.get_line(line_id)
                self.db.execute(
                    "INSERT INTO plans (id, voucher, provider, lines, status) VALUES (?,?,?,?, 'draft')",
                    (rec["id"], rec["voucher"], rec["provider"], dumps(rec["lines"])))
                for line_id in rec["lines"]:
                    self.db.execute(
                        "INSERT INTO plan_lines (plan_id, line_id) VALUES (?,?)",
                        (rec["id"], line_id))
                loaded.append(("plans", rec["id"]))
            for rec in bundle.get("approvals", []):
                if self.db.execute("SELECT 1 FROM approvals WHERE id=?", (rec["id"],)).fetchone():
                    continue
                plan = self.get_plan(rec["plan"])
                self.get_fund_or_404(rec["fund"], plan["voucher"])
                plan_line_ids = self.plan_line_ids(rec["plan"])
                for line_id, amount in rec["lines"].items():
                    if line_id not in plan_line_ids:
                        raise DomainError("bad_approval",
                                          f"认可 {rec['id']} 引用了计划外的分项 {line_id}", 400)
                    if amount < 0:
                        raise DomainError("bad_approval", "认可金额不能为负", 400)
                parse_time(rec["signed"])
                # 若认可依赖尚未办理的证据更正（新上限），先挂起；
                # apply_change 办理对应更正后自动生效，避免超前认可阻塞既有版本确认。
                pending = 0
                voucher = self.get_voucher(plan["voucher"])
                for line_id, amount in rec["lines"].items():
                    base = self.line_limits(self.get_line(line_id)).get(rec["fund"], 0)
                    if amount > base:
                        pending = 1
                self.db.execute(
                    "INSERT INTO approvals (id, plan_id, version, fund_id, shares_json, signed, "
                    "pending) VALUES (?,?,?,?,?,?,?)",
                    (rec["id"], rec["plan"], rec["version"], rec["fund"],
                     dumps(rec["lines"]), rec["signed"], pending))
                loaded.append(("approvals", rec["id"]))
            for rec in bundle.get("work", []):
                if self.db.execute("SELECT 1 FROM work WHERE id=?", (rec["id"],)).fetchone():
                    continue
                self._insert_work(rec)
                loaded.append(("work", rec["id"]))
            for rec in bundle.get("changes", []):
                if self.db.execute("SELECT 1 FROM changes WHERE id=?", (rec["id"],)).fetchone():
                    continue
                self._insert_change(rec)
                loaded.append(("changes", rec["id"]))
            for rec in bundle.get("receipts", []):
                if self.db.execute("SELECT 1 FROM receipts WHERE id=?", (rec["id"],)).fetchone():
                    continue
                self._insert_receipt_raw(rec)
                loaded.append(("receipts", rec["id"]))
        return {"loaded": loaded, "count": len(loaded)}

    def get_fund_or_404(self, fund_id, voucher_id=None):
        row = self.db.execute("SELECT * FROM funds WHERE id=?", (fund_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", f"出资方 {fund_id} 不存在", 404)
        if voucher_id is not None:
            voucher = self.get_voucher(voucher_id)
            if fund_id not in loads(voucher["sponsors"]):
                raise DomainError("unauthorized_sponsor",
                                  f"{fund_id} 不在券 {voucher_id} 的授权出资人内", 403)
        return row

    # ------------------------------------------------------------------ 计划与报价
    def submit_plan(self, plan_id, voucher_id, provider, line_specs):
        """企业按券提交中试计划；机构可把一笔现场委托拆成多个服务项。"""
        if self.db.execute("SELECT 1 FROM plans WHERE id=?", (plan_id,)).fetchone():
            raise DomainError("duplicate_plan", f"计划 {plan_id} 已存在", 409)
        voucher = self.get_voucher(voucher_id)
        if not line_specs:
            raise DomainError("empty_plan", "计划至少包含一个服务项", 400)
        line_ids = []
        with self.db:
            for spec in line_specs:
                line_id = spec["id"]
                if line_id in line_ids:
                    raise DomainError("duplicate_line", f"分项 {line_id} 重复", 400)
                self.db.execute(
                    "INSERT INTO lines (id, fee, self_min, exclusive, limits, after_lines) "
                    "VALUES (?,?,?,?,?,?)",
                    (line_id, spec["fee"], spec["self_min"],
                     1 if spec.get("exclusive", False) else 0,
                     dumps(spec.get("limits", {})), dumps(spec.get("after", []))))
                line_ids.append(line_id)
            for spec in line_specs:
                for prior in spec.get("after", []):
                    if prior not in line_ids:
                        raise DomainError("bad_precondition",
                                          f"前序 {prior} 不在计划内", 400)
                for fund_id in spec.get("limits", {}):
                    self.get_fund_or_404(fund_id, voucher_id)
            self.db.execute(
                "INSERT INTO plans (id, voucher, provider, lines, status) VALUES (?,?,?,?, 'draft')",
                (plan_id, voucher_id, provider, dumps(line_ids)))
            for line_id in line_ids:
                self.db.execute("INSERT INTO plan_lines (plan_id, line_id) VALUES (?,?)",
                                (plan_id, line_id))
            trace(self.db, line_ids[0], "plan_submitted",
                  {"plan": plan_id, "voucher": voucher_id, "provider": provider})
        return {"plan": plan_id, "lines": line_ids, "quarter": voucher["quarter"]}

    def _version_approvals(self, plan_id, version):
        result = {}
        for ap in self.db.execute(
                "SELECT * FROM approvals WHERE plan_id=? AND version=? AND pending=0",
                (plan_id, version)):
            result[ap["fund_id"]] = ap
        return result

    def evaluate_version(self, plan_id, version):
        """让企业先看双方各自可能承担多少，并给出能否确认的判断。"""
        plan = self.get_plan(plan_id)
        voucher = self.get_voucher(plan["voucher"])
        approvals = self._version_approvals(plan_id, version)
        lines = [self.get_line(lid) for lid in self.plan_line_ids(plan_id)]
        line_views = []
        all_covered = True
        for line in lines:
            sponsors = []
            sponsor_total = 0
            exclusive_funds = []
            reasons = []
            for fund_id, ap in approvals.items():
                amount = loads(ap["shares_json"]).get(line["id"], 0)
                if amount <= 0:
                    continue
            for fund_id, ap in approvals.items():
                amount = loads(ap["shares_json"]).get(line["id"], 0)
                if amount <= 0:
                    continue
                # 报价只判断项目条款上限；季度容量在确认时原子复核，
                # 以便并行办理给出明确的 capacity_exhausted 结果。
                ceiling = self.effective_ceiling(voucher["id"], fund_id, line["id"])
                within_ceiling = amount <= ceiling
                if not within_ceiling:
                    reasons.append(f"{fund_id} 认可 {amount} 超出项目上限 {ceiling}")
                sponsors.append({"fund": fund_id, "amount": amount,
                                 "ceiling": ceiling,
                                 "fits_capacity": within_ceiling,
                                 "approval": ap["id"]})
                sponsor_total += amount
                if amount > 0:
                    exclusive_funds.append(fund_id)
            if line["exclusive"] and len(exclusive_funds) > 1:
                reasons.append("exclusive 分项至多由一个出资方承担资助部分")
            self_pay = line["fee"] - sponsor_total
            self_ok = self_pay >= line["self_min"]
            if not self_ok:
                reasons.append(f"企业自付 {self_pay} 低于 self_min {line['self_min']}")
            if not sponsors:
                reasons.append("没有任何出资方认可该分项，不构成足额组合承诺")
            covered = bool(sponsors) and self_ok and not reasons
            all_covered = all_covered and covered
            line_views.append({
                "line": line["id"], "fee": line["fee"], "self_min": line["self_min"],
                "exclusive": bool(line["exclusive"]), "sponsors": sponsors,
                "sponsor_total": sponsor_total, "company_self_pay": self_pay,
                "self_pay_ok": self_ok, "covered": covered, "reasons": reasons,
            })
        # 选择承担费用的各方都要认可同一版本：已生效认可中只要存在其他版本，
        # 说明各方尚未就同一方案达成一致；挂起中的认可不参与判断。
        plan_versions = {r["version"] for r in self.db.execute(
            "SELECT DISTINCT version FROM approvals WHERE plan_id=? AND pending=0",
            (plan_id,))}
        same_version = bool(approvals) and plan_versions == {version}
        return {
            "plan": plan_id, "version": version,
            "funds": sorted(approvals), "same_version": same_version,
            "lines": line_views, "confirmable": all_covered and same_version,
        }

    def add_approval(self, approval_id, plan_id, version, fund_id, shares, actor):
        plan = self.get_plan(plan_id)
        fund = self.get_fund_or_404(fund_id, plan["voucher"])
        voucher = self.get_voucher(plan["voucher"])
        plan_line_ids = self.plan_line_ids(plan_id)
        for line_id, amount in shares.items():
            if line_id not in plan_line_ids:
                raise DomainError("bad_approval", f"认可引用了计划外分项 {line_id}", 400)
            if amount < 0:
                raise DomainError("bad_approval", "认可金额不能为负", 400)
            ceiling = self.effective_ceiling(voucher["id"], fund_id, line_id)
            if amount > ceiling:
                raise DomainError(
                    "above_ceiling",
                    f"{fund_id} 对 {line_id} 认可 {amount} 超出上限 {ceiling}", 422)
        if self.db.execute("SELECT 1 FROM approvals WHERE id=?", (approval_id,)).fetchone():
            raise DomainError("duplicate_approval", f"认可 {approval_id} 已存在", 409)
        with self.db:
            self.db.execute(
                "INSERT INTO approvals (id, plan_id, version, fund_id, shares_json, signed) "
                "VALUES (?,?,?,?,?,?)",
                (approval_id, plan_id, version, fund_id, dumps(shares), now_iso()))
        return {"approval": approval_id, "fund": fund_id, "plan": plan_id, "version": version}

    # ------------------------------------------------------------------ 计划确认
    def confirm_plan(self, plan_id, version, actor):
        """各方认可同一版本且覆盖约定费用时确认，并原子占用季度额度。"""
        plan = self.get_plan(plan_id)
        if plan["status"] == "confirmed":
            raise DomainError("already_confirmed",
                              f"计划已按版本 {plan['confirmed_version']} 确认", 409)
        quote = self.evaluate_version(plan_id, version)
        if not quote["confirmable"]:
            raise DomainError("not_confirmable",
                              "组合承诺未足额或版本不一致，不能确认", 422)
        voucher = self.get_voucher(plan["voucher"])
        approvals = self._version_approvals(plan_id, version)
        with self.db:
            # 并行办理：在同一事务内复核额度，先得到足额组合承诺者占用。
            for fund_id, ap in approvals.items():
                shares = loads(ap["shares_json"])
                need = sum(shares.values())
                cap = self.fund_capacity(fund_id, voucher["quarter"])
                if need > cap["available"]:
                    raise DomainError(
                        "capacity_exhausted",
                        f"出资方 {fund_id} 本季度可用额度不足：需要 {need}，剩余 {cap['available']}",
                        409)
            for ap in approvals.values():
                for line_id, amount in loads(ap["shares_json"]).items():
                    if amount == 0:
                        continue
                    self.db.execute(
                        "INSERT INTO commitments (plan_id, line_id, fund_id, version, amount, "
                        "status, created_at) VALUES (?,?,?,?,?, 'active', ?)",
                        (plan_id, line_id, ap["fund_id"], version, amount, now_iso()))
                    trace(self.db, line_id, "commitment_created",
                          {"plan": plan_id, "fund": ap["fund_id"], "version": version,
                           "amount": amount})
            for line_id in self.plan_line_ids(plan_id):
                line = self.get_line(line_id)
                sponsor_total = self.db.execute(
                    "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
                    "WHERE plan_id=? AND line_id=? AND status='active'",
                    (plan_id, line_id)).fetchone()["s"]
                self_pay = line["fee"] - sponsor_total
                self.db.execute(
                    "INSERT INTO company_obligations (plan_id, line_id, base_amount, makeup_amount) "
                    "VALUES (?,?,?,0)", (plan_id, line_id, self_pay))
            self.db.execute(
                "UPDATE plans SET status='confirmed', confirmed_version=?, confirmed_at=? WHERE id=?",
                (version, now_iso(), plan_id))
        return {"plan": plan_id, "status": "confirmed", "version": version}

    # ------------------------------------------------------------------ 里程碑
    def _insert_work(self, rec):
        if rec["milestone"] not in VALID_MILESTONES:
            raise DomainError("bad_milestone",
                              f"未知里程碑 {rec['milestone']}", 400)
        parse_time(rec["actor_time"])
        parse_time(rec["arrived"])
        self.get_line(rec["line"])
        self.db.execute(
            "INSERT OR IGNORE INTO work (id, line_id, milestone, principal, actor_time, arrived) "
            "VALUES (?,?,?,?,?,?)",
            (rec["id"], rec["line"], rec["milestone"], rec["principal"],
             rec["actor_time"], rec["arrived"]))
        trace(self.db, rec["line"], "work_recorded",
              {"work": rec["id"], "milestone": rec["milestone"], "principal": rec["principal"]},
              at_time=rec["arrived"])

    def declare_work(self, rec):
        if self.db.execute("SELECT 1 FROM work WHERE id=?", (rec["id"],)).fetchone():
            raise DomainError("duplicate_work", f"人工记录 {rec['id']} 已存在", 409)
        with self.db:
            self._insert_work(rec)
        return {"work": rec["id"], "milestone": rec["milestone"]}

    def line_stage(self, line_id):
        stages = [r["milestone"] for r in self.db.execute(
            "SELECT milestone FROM work WHERE line_id=? ORDER BY actor_time", (line_id,))]
        if "device_accepted" in stages:
            return "device_accepted"
        if "work_done" in stages:
            return "work_done"
        return "not_started"

    def work_board(self, plan_id):
        """机构看板：下一步哪个工序可继续，人工完成但设备回执未到的留在待查。"""
        plan = self.get_plan(plan_id)
        board = []
        for line_id in self.plan_line_ids(plan_id):
            line = self.get_line(line_id)
            stage = self.line_stage(line_id)
            prereqs = []
            prereqs_ok = True
            for prior in loads(line["after_lines"]):
                prior_stage = self.line_stage(prior)
                ok = prior_stage == "device_accepted"
                prereqs_ok = prereqs_ok and ok
                prereqs.append({"line": prior, "stage": prior_stage, "accepted": ok})
            coverage = self._line_coverage(plan_id, line_id)
            ready = (plan["status"] == "confirmed" and coverage["covered"]
                     and prereqs_ok and stage == "not_started")
            board.append({
                "line": line_id, "stage": stage, "prereqs": prereqs,
                "covered": coverage["covered"], "can_start": ready,
                "pending_device": stage == "work_done",
            })
        return {"plan": plan_id, "status": plan["status"], "board": board}

    def _line_coverage(self, plan_id, line_id):
        line = self.get_line(line_id)
        sponsor_total = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
            "WHERE plan_id=? AND line_id=? AND status='active'",
            (plan_id, line_id)).fetchone()["s"]
        ob = self.db.execute(
            "SELECT * FROM company_obligations WHERE plan_id=? AND line_id=?",
            (plan_id, line_id)).fetchone()
        company = (ob["base_amount"] + ob["makeup_amount"]) if ob else 0
        return {"fee": line["fee"], "sponsor_total": sponsor_total,
                "company": company, "covered": sponsor_total + company == line["fee"]}

    # ------------------------------------------------------------------ 证据更正
    def _insert_change(self, rec):
        parse_time(rec["occurred"])
        if rec["new_ceiling"] < 0:
            raise DomainError("bad_change", "new_ceiling 不能为负", 400)
        self.get_voucher(rec["voucher"])
        self.get_fund_or_404(rec["fund"], rec["voucher"])
        for line_id in rec["scope"]:
            self.get_line(line_id)
        self.db.execute(
            "INSERT INTO changes (id, fund_id, voucher_id, scope_json, new_ceiling, occurred) "
            "VALUES (?,?,?,?,?,?)",
            (rec["id"], rec["fund"], rec["voucher"], dumps(rec["scope"]),
             rec["new_ceiling"], rec["occurred"]))

    def apply_change(self, change_id, actor):
        """证据更正：未开工项解除受影响承诺；已开工/已完成项保留为遗留承诺。"""
        ch = self._one("SELECT * FROM changes WHERE id=?", (change_id,))
        if ch["applied"]:
            raise DomainError("change_applied", f"更正 {change_id} 已办理", 409)
        scope = loads(ch["scope_json"])
        released, legacy = [], []
        with self.db:
            for line_id in scope:
                stage = self.line_stage(line_id)
                for com in self.db.execute(
                        "SELECT * FROM commitments WHERE fund_id=? AND line_id=? AND status='active'",
                        (ch["fund_id"], line_id)).fetchall():
                    if stage == "not_started":
                        self.db.execute(
                            "UPDATE commitments SET status='released' WHERE id=?", (com["id"],))
                        released.append({"line": line_id, "fund": ch["fund_id"],
                                         "amount": com["amount"], "commitment": com["id"]})
                        trace(self.db, line_id, "commitment_released",
                              {"change": change_id, "fund": ch["fund_id"],
                               "amount": com["amount"]}, at_time=ch["occurred"])
                    else:
                        self.db.execute(
                            "UPDATE commitments SET status='legacy' WHERE id=?", (com["id"],))
                        legacy.append({"line": line_id, "fund": ch["fund_id"],
                                       "amount": com["amount"], "stage": stage,
                                       "commitment": com["id"]})
                        trace(self.db, line_id, "commitment_legacy",
                              {"change": change_id, "fund": ch["fund_id"],
                               "amount": com["amount"], "stage": stage},
                              at_time=ch["occurred"])
            self.db.execute("UPDATE changes SET applied=1 WHERE id=?", (change_id,))
            # 更正办理后，依赖新上限的挂起认可重新校验；额度与范围已满足即生效。
            activated = []
            for ap in self.db.execute(
                    "SELECT * FROM approvals WHERE pending=1").fetchall():
                voucher_id = self.get_plan(ap["plan_id"])["voucher"]
                ok = True
                for line_id, amount in loads(ap["shares_json"]).items():
                    if amount > self.effective_ceiling(voucher_id, ap["fund_id"], line_id):
                        ok = False
                        break
                if ok:
                    self.db.execute("UPDATE approvals SET pending=0 WHERE id=?", (ap["id"],))
                    activated.append(ap["id"])
        return {"change": change_id, "released": released, "legacy": legacy,
                "activated_approvals": activated}

    def reprocess_line(self, plan_id, line_id, version, actor):
        """更正后重新落实一个分项的付款人（接替方以其认可的额度和范围为准）。

        旧版中仍具资格的 active 承诺按下列规则处理：
        - 新版本认可里包含同一出资方的，以新认可替代旧承诺；
        - 新版本未提及的，原样结转为最终承担；
        - 被取消资格的 legacy 承诺不参与新方案，只保留追还义务。
        """
        plan = self.get_plan(plan_id)
        voucher = self.get_voucher(plan["voucher"])
        line = self.get_line(line_id)
        stage = self.line_stage(line_id)
        if stage == "not_started":
            raise DomainError("line_not_started",
                              "未开工项应通过解除承诺并重新确认办理，不走接替", 422)
        approvals = self._version_approvals(plan_id, version)
        if not approvals:
            raise DomainError("no_approvals", f"版本 {version} 没有任何出资认可", 422)
        current = {r["fund_id"]: r for r in self.db.execute(
            "SELECT * FROM commitments WHERE plan_id=? AND line_id=? AND status='active'",
            (plan_id, line_id)).fetchall()}
        new_rows = []
        replaced_ids = []
        removed_ids = []
        def _effective_amount(fund_id, ap):
            amount = loads(ap["shares_json"]).get(line_id, 0)
            if amount <= 0:
                return 0
            # 上限已被证据更正降为 0 的认可不构成有效接替。
            if self.effective_ceiling(voucher["id"], fund_id, line_id) == 0:
                return 0
            return amount

        no_takeover = all(
            _effective_amount(fund_id, ap) == 0 for fund_id, ap in approvals.items())
        with self.db:
            # 无新版认可（或新版认可均不覆盖本分项）：全部活跃承诺退出新方案，
            # 已取消资格的走 legacy 追还，其余由企业补缴接住缺口。
            if no_takeover:
                for old in current.values():
                    ceiling = self.effective_ceiling(voucher["id"], old["fund_id"], line_id)
                    new_status = "legacy" if ceiling == 0 else "released"
                    self.db.execute(
                        "UPDATE commitments SET status=? WHERE id=?", (new_status, old["id"]))
                    removed_ids.append({"commitment": old["id"], "fund": old["fund_id"],
                                        "amount": old["amount"], "status": new_status})
                    trace(self.db, line_id, "commitment_without_takeover",
                          {"plan": plan_id, "fund": old["fund_id"],
                           "amount": old["amount"], "status": new_status})
            for fund_id, ap in approvals.items():
                amount = loads(ap["shares_json"]).get(line_id, 0)
                if amount <= 0:
                    continue
                ceiling = self.effective_ceiling(voucher["id"], fund_id, line_id)
                # 证据更正已把该出资方上限降为 0：旧认可不再具资格。
                # 无接替分支已处理其活跃承诺，这里直接跳过。
                if ceiling == 0:
                    continue
                if amount > ceiling:
                    raise DomainError(
                        "above_ceiling",
                        f"接替方 {fund_id} 认可 {amount} 超出其认可上限 {ceiling}", 422)
                if fund_id in current:
                    old = current[fund_id]
                    if old["amount"] == amount:
                        continue
                    # 同一出资方在新版本改了额度：旧承诺作废，按新认可重立。
                    self.db.execute(
                        "UPDATE commitments SET status='superseded' WHERE id=?", (old["id"],))
                    replaced_ids.append(old["id"])
                cap = self.fund_capacity(fund_id, voucher["quarter"])
                if amount > cap["available"]:
                    raise DomainError("capacity_exhausted",
                                      f"{fund_id} 额度不足：需要 {amount}，剩余 {cap['available']}",
                                      409)
                new_rows.append((fund_id, amount, ap["id"]))
            for fund_id, amount, approval_id in new_rows:
                self.db.execute(
                    "INSERT INTO commitments (plan_id, line_id, fund_id, version, amount, "
                    "status, created_at) VALUES (?,?,?,?,?, 'active', ?)",
                    (plan_id, line_id, fund_id, version, amount, now_iso()))
                trace(self.db, line_id, "takeover_commitment",
                      {"plan": plan_id, "fund": fund_id, "amount": amount,
                       "approval": approval_id, "version": version})
            active_total = self.db.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
                "WHERE plan_id=? AND line_id=? AND status='active'",
                (plan_id, line_id)).fetchone()["s"]
            legacy_total = self.db.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
                "WHERE plan_id=? AND line_id=? AND status='legacy'",
                (plan_id, line_id)).fetchone()["s"]
            net_paid = self.net_paid_to_provider(line_id)
            # 机构已经收过款的项目不能重复向机构付款。
            if net_paid > line["fee"]:
                raise DomainError(
                    "over_paid",
                    f"机构净入账 {net_paid} 已超过费用 {line['fee']}，须先追还再办理", 422)
            target_company = line["fee"] - active_total
            ob = self.db.execute(
                "SELECT * FROM company_obligations WHERE plan_id=? AND line_id=?",
                (plan_id, line_id)).fetchone()
            if ob is None:
                raise DomainError("no_obligation", "该分项没有企业自付记录，计划尚未确认", 422)
            current_company = ob["base_amount"] + ob["makeup_amount"]
            makeup = target_company - current_company
            if makeup < 0:
                raise DomainError(
                    "refund_due",
                    f"当前组合超额 {-makeup} 分；遗留承诺 {legacy_total} 须先由对应出资方向机构追还，"
                    "对账明确后才能释放并重办", 422)
            if makeup:
                self.db.execute(
                    "UPDATE company_obligations SET makeup_amount=? WHERE plan_id=? AND line_id=?",
                    (ob["makeup_amount"] + makeup, plan_id, line_id))
                trace(self.db, line_id, "company_makeup",
                      {"plan": plan_id, "line": line_id, "amount": makeup})
            legacy_refund_due = self._legacy_refund_due(line_id)
        return {
            "line": line_id, "version": version, "stage": stage,
            "active_sponsor_total": active_total,
            "company_obligation": current_company + makeup,
            "company_makeup": makeup, "legacy_total": legacy_total,
            "net_paid_to_provider": net_paid, "legacy_refund_due": legacy_refund_due,
            "superseded": replaced_ids,
            "covered": (active_total + current_company + makeup) == line["fee"],
        }

    def _legacy_refund_due(self, line_id):
        """遗留出资方仍须追还的金额：机构净入账超出其新方案最终承担额的部分。"""
        due = []
        for r in self.db.execute(
                "SELECT fund_id, SUM(amount) AS s FROM commitments "
                "WHERE line_id=? AND status='legacy' GROUP BY fund_id", (line_id,)).fetchall():
            net = self.net_paid_to_provider(line_id, r["fund_id"])
            active_now = self.db.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
                "WHERE line_id=? AND fund_id=? AND status='active'",
                (line_id, r["fund_id"])).fetchone()["s"]
            outstanding = max(net - active_now, 0)
            if outstanding > 0:
                due.append({"fund": r["fund_id"], "amount": outstanding})
        return due

    def _reconcile_legacy_at_close(self):
        """关账即对账明确：按已入账净额调减遗留承诺，追清的转为 refunded 释放额度。

        每个遗留出资方的剩余追还 = 机构在该分项上的净入账 − 该出资方新方案最终承担。
        同一条遗留承诺按该剩余额更新；剩余为 0 表示已追清，转为 refunded。
        """
        rows = self.db.execute(
            "SELECT DISTINCT line_id, fund_id FROM commitments WHERE status='legacy'").fetchall()
        adjusted = []
        for r in rows:
            line_id, fund_id = r["line_id"], r["fund_id"]
            net = self.net_paid_to_provider(line_id, fund_id)
            active_now = self.db.execute(
                "SELECT COALESCE(SUM(amount),0) AS s FROM commitments "
                "WHERE line_id=? AND fund_id=? AND status='active'",
                (line_id, fund_id)).fetchone()["s"]
            residual = max(net - active_now, 0)
            for com in self.db.execute(
                    "SELECT * FROM commitments WHERE line_id=? AND fund_id=? AND status='legacy'",
                    (line_id, fund_id)).fetchall():
                if residual == 0:
                    self.db.execute(
                        "UPDATE commitments SET status='refunded' WHERE id=?", (com["id"],))
                    adjusted.append({"commitment": com["id"], "line": line_id,
                                     "fund": fund_id, "status": "refunded"})
                elif residual != com["amount"]:
                    self.db.execute(
                        "UPDATE commitments SET amount=? WHERE id=?", (residual, com["id"]))
                    adjusted.append({"commitment": com["id"], "line": line_id,
                                     "fund": fund_id, "status": "legacy",
                                     "amount": residual})
        return adjusted

    # ------------------------------------------------------------------ 回执与台账
    def _insert_receipt_raw(self, rec):
        if rec["direction"] not in VALID_DIRECTIONS:
            raise DomainError("bad_receipt", f"未知方向 {rec['direction']}", 400)
        if rec["state"] not in VALID_RECEIPT_STATES:
            raise DomainError("bad_receipt", f"未知状态 {rec['state']}", 400)
        if rec["amount"] <= 0:
            raise DomainError("bad_receipt", "amount 须为正整数（绝对金额）", 400)
        parse_time(rec["happened"])
        parse_time(rec["arrived"])
        self.get_line(rec["line"])
        if rec["fund"]:
            if self.db.execute("SELECT 1 FROM funds WHERE id=?", (rec["fund"],)).fetchone() is None:
                raise DomainError("bad_receipt", f"未知出资方 {rec['fund']}", 400)
        if self.db.execute("SELECT 1 FROM receipts WHERE transfer=?", (rec["transfer"],)).fetchone():
            raise DomainError("duplicate_transfer",
                              f"transfer {rec['transfer']} 已存在", 409)
        self.db.execute(
            "INSERT INTO receipts (id, transfer, line_id, fund_id, direction, state, amount, "
            "happened, arrived) VALUES (?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec["transfer"], rec["line"], rec.get("fund"), rec["direction"],
             rec["state"], rec["amount"], rec["happened"], rec["arrived"]))

    def ingest_receipt(self, rec, actor):
        """登记对手方回执并按业务时间入账；到达时间不代替业务时间。

        交换资料载入时只登记原文，本方法负责把尚未入账的回执写入台账，
        从而区分“回执可取得”与“本地已做过相应操作”。
        """
        with self.db:
            existing = self.db.execute(
                "SELECT * FROM receipts WHERE transfer=?", (rec["transfer"],)).fetchone()
            if existing is None:
                self._insert_receipt_raw(rec)
            elif existing["id"] != rec["id"]:
                raise DomainError("duplicate_transfer",
                                  f"transfer {rec['transfer']} 已用于回执 {existing['id']}", 409)
            entry = self._one("SELECT * FROM receipts WHERE transfer=?", (rec["transfer"],))
            already_booked = self.db.execute(
                "SELECT 1 FROM ledger WHERE ref_kind='receipt' AND ref_id=?",
                (entry["id"],)).fetchone()
            if entry["state"] == "received" and already_booked is None:
                self.db.execute(
                    "INSERT INTO ledger (line_id, fund_id, direction, amount, ref_kind, ref_id, "
                    "happened, recorded_at) VALUES (?,?,?,?,?,?,?,?)",
                    (entry["line_id"], entry["fund_id"], entry["direction"], entry["amount"],
                     "receipt", entry["id"], entry["happened"], now_iso()))
                trace(self.db, entry["line_id"], "receipt_booked",
                      {"receipt": entry["id"], "transfer": entry["transfer"],
                       "direction": entry["direction"], "amount": entry["amount"],
                       "state": entry["state"]}, at_time=entry["happened"])
            elif already_booked is None:
                trace(self.db, entry["line_id"], "receipt_seen",
                      {"receipt": entry["id"], "transfer": entry["transfer"],
                       "state": entry["state"]}, at_time=entry["arrived"])
            # 回执若对应本地已发出的付款，回写在途状态。
            dp = self.db.execute("SELECT * FROM dispatches WHERE transfer=?",
                                 (entry["transfer"],)).fetchone()
            if dp is not None and dp["state"] == "pending":
                new_state = "settled" if entry["state"] == "received" else "failed"
                self.db.execute("UPDATE dispatches SET state=? WHERE id=?",
                                (new_state, dp["id"]))
        return {"receipt": entry["id"], "transfer": rec["transfer"], "state": entry["state"],
                "booked": entry["state"] == "received"}

    def net_paid_to_provider(self, line_id, fund_id=None):
        """机构在该分项上的净入账：pay 为正，refund/cancel/chargeback 为负。"""
        sql = ("SELECT COALESCE(SUM(CASE WHEN direction='pay' THEN amount ELSE -amount END),0) AS s "
               "FROM ledger WHERE line_id=?")
        params = [line_id]
        if fund_id is not None:
            sql += " AND fund_id=?"
            params.append(fund_id)
        return self.db.execute(sql, params).fetchone()["s"]

    def dispatch_payment(self, transfer, line_id, fund_id, amount, actor):
        """向机构（或企业）发起付款。重复或超额付款一律拒绝。"""
        if amount <= 0:
            raise DomainError("bad_dispatch", "付款金额须为正整数", 400)
        line = self.get_line(line_id)
        if self.db.execute("SELECT 1 FROM dispatches WHERE transfer=?", (transfer,)).fetchone():
            raise DomainError("duplicate_transfer", f"transfer {transfer} 已使用", 409)
        if self.db.execute("SELECT 1 FROM receipts WHERE transfer=?", (transfer,)).fetchone():
            raise DomainError("duplicate_transfer", f"transfer {transfer} 已用于回执", 409)
        with self.db:
            net = self.net_paid_to_provider(line_id)
            if net + amount > line["fee"]:
                raise DomainError(
                    "over_paid",
                    f"机构累计净入账将达 {net + amount}，超过分项费用 {line['fee']}，拒绝重复付款",
                    422)
            if fund_id:
                fund = self.get_fund_or_404(fund_id)
                voucher_row = self.db.execute(
                    "SELECT v.* FROM plans p JOIN vouchers v ON p.voucher=v.id "
                    "JOIN plan_lines pl ON pl.plan_id=p.id WHERE pl.line_id=? LIMIT 1",
                    (line_id,)).fetchone()
                if voucher_row is None:
                    raise DomainError("line_not_planned", "该分项不属于任何计划", 422)
                cap = self.fund_capacity(fund_id, voucher_row["quarter"])
                if amount > cap["available"]:
                    raise DomainError("capacity_exhausted",
                                      f"{fund_id} 可用额度不足：需要 {amount}，剩余 {cap['available']}",
                                      409)
            self.db.execute(
                "INSERT INTO dispatches (transfer, line_id, fund_id, direction, amount, "
                "requested_at, state) VALUES (?,?,?, 'pay', ?, ?, 'pending')",
                (transfer, line_id, fund_id, amount, now_iso()))
            trace(self.db, line_id, "payment_dispatched",
                  {"transfer": transfer, "fund": fund_id, "amount": amount})
        return {"transfer": transfer, "line": line_id, "fund": fund_id, "amount": amount,
                "state": "pending"}

    def line_reconciliation(self, line_id):
        """机构分项对账：约定费用、各方承担、机构净入账、待追还/待付。"""
        line = self.get_line(line_id)
        commitments = [dict(r) for r in self.db.execute(
            "SELECT fund_id, amount, status, version FROM commitments WHERE line_id=? ORDER BY id",
            (line_id,))]
        ob = self.db.execute("SELECT * FROM company_obligations WHERE line_id=?",
                             (line_id,)).fetchone()
        company = dict(ob) if ob else None
        ledger_rows = [dict(r) for r in self.db.execute(
            "SELECT fund_id, direction, amount, ref_kind, ref_id, happened FROM ledger "
            "WHERE line_id=? ORDER BY happened, id", (line_id,))]
        receipts = [dict(r) for r in self.db.execute(
            "SELECT id, transfer, fund_id, direction, state, amount, happened, arrived, period_id "
            "FROM receipts WHERE line_id=? ORDER BY happened", (line_id,))]
        dispatches = [dict(r) for r in self.db.execute(
            "SELECT transfer, fund_id, amount, state, requested_at FROM dispatches "
            "WHERE line_id=? ORDER BY id", (line_id,))]
        net = self.net_paid_to_provider(line_id)
        per_fund = {}
        for r in self.db.execute(
                "SELECT fund_id, COALESCE(SUM(CASE WHEN direction='pay' THEN amount ELSE -amount END),0) "
                "AS net FROM ledger WHERE line_id=? GROUP BY fund_id", (line_id,)):
            per_fund[r["fund_id"]] = r["net"]
        active_by_fund = {}
        for r in self.db.execute(
                "SELECT fund_id, SUM(amount) AS s FROM commitments "
                "WHERE line_id=? AND status='active' GROUP BY fund_id", (line_id,)):
            active_by_fund[r["fund_id"]] = r["s"]
        payable = {f: max(active_by_fund.get(f, 0) - per_fund.get(f, 0), 0)
                   for f in active_by_fund}
        return {
            "line": line_id, "fee": line["fee"], "stage": self.line_stage(line_id),
            "commitments": commitments,
            "company_obligation": company,
            "net_paid_to_provider": net,
            "balance_due_to_provider": max(line["fee"] - net, 0),
            "over_paid": max(net - line["fee"], 0),
            "payable_by_fund": payable,
            "legacy_refund_due": self._legacy_refund_due(line_id),
            "ledger": ledger_rows, "receipts": receipts, "dispatches": dispatches,
        }

    # ------------------------------------------------------------------ 结算期
    def open_period(self, period_id, quarter):
        if self.db.execute("SELECT 1 FROM periods WHERE id=?", (period_id,)).fetchone():
            raise DomainError("duplicate_period", f"结算期 {period_id} 已存在", 409)
        # 新开的结算期不承接任何台账分录；分录只在关账时按当时未归属的记录归集。
        self.db.execute("INSERT INTO periods (id, quarter, closed) VALUES (?,?,0)",
                        (period_id, quarter))
        self.db.commit()
        return {"period": period_id, "quarter": quarter, "closed": False}

    def close_period(self, period_id, actor):
        """对账关账：同一笔调整只能计入一个结算期；关账后额度才按退款恢复。"""
        period = self._one("SELECT * FROM periods WHERE id=?", (period_id,))
        if period["closed"]:
            raise DomainError("period_closed", f"结算期 {period_id} 已关账", 409)
        with self.db:
            adjusted = self._reconcile_legacy_at_close()
            entries = self.db.execute(
                "SELECT l.* FROM ledger l WHERE NOT EXISTS ("
                "SELECT 1 FROM period_entries pe WHERE pe.ledger_id=l.id)").fetchall()
            counted = []
            for ledger_row in entries:
                self.db.execute(
                    "INSERT INTO period_entries (period_id, ledger_id) VALUES (?,?)",
                    (period_id, ledger_row["id"]))
                if ledger_row["ref_kind"] == "receipt":
                    self.db.execute("UPDATE receipts SET period_id=? WHERE id=?",
                                    (period_id, ledger_row["ref_id"]))
                counted.append(ledger_row["id"])
            summary = self.period_summary(period_id)
            self.db.execute(
                "UPDATE periods SET closed=1, closed_at=?, summary_json=? WHERE id=?",
                (now_iso(), dumps(summary), period_id))
        return {"period": period_id, "closed": True,
                "ledger_entries": counted, "legacy_adjustments": adjusted}

    def period_summary(self, period_id):
        period = self._one("SELECT * FROM periods WHERE id=?", (period_id,))
        rows = self.db.execute(
            "SELECT l.* FROM ledger l JOIN period_entries pe ON pe.ledger_id=l.id "
            "WHERE pe.period_id=? ORDER BY l.happened", (period_id,)).fetchall()
        by_fund = {}
        for r in rows:
            bucket = by_fund.setdefault(r["fund_id"] or "COMPANY",
                                        {"pay": 0, "refund": 0, "cancel": 0, "chargeback": 0})
            bucket[r["direction"]] += r["amount"]
        return {"period": period_id, "quarter": period["quarter"],
                "closed": bool(period["closed"]), "entry_count": len(rows), "by_fund": by_fund}

    # ------------------------------------------------------------------ 争议与追溯
    def open_dispute(self, dispute_id, opened_by, reason, subject_line=None):
        if self.db.execute("SELECT 1 FROM disputes WHERE id=?", (dispute_id,)).fetchone():
            raise DomainError("duplicate_dispute", f"争议 {dispute_id} 已存在", 409)
        with self.db:
            self.db.execute(
                "INSERT INTO disputes (id, opened_by, subject_line, reason, opened_at, status) "
                "VALUES (?,?,?,?,?, 'open')",
                (dispute_id, opened_by, subject_line, reason, now_iso()))
        return {"dispute": dispute_id, "status": "open"}

    def resolve_dispute(self, dispute_id, actor):
        self._one("SELECT * FROM disputes WHERE id=?", (dispute_id,))
        with self.db:
            self.db.execute("UPDATE disputes SET status='resolved' WHERE id=?", (dispute_id,))
        return {"dispute": dispute_id, "status": "resolved"}

    def trace_line(self, line_id):
        """一笔资助怎样走到机构实际收款、企业补缴或向资助方返还的完整链路。"""
        self.get_line(line_id)
        events = [dict(r) for r in self.db.execute(
            "SELECT line_id, event, payload_json, at_time FROM traces "
            "WHERE line_id=? ORDER BY at_time, id", (line_id,))]
        for ev in events:
            ev["payload"] = loads(ev.pop("payload_json"))
        receipts = [dict(r) for r in self.db.execute(
            "SELECT id, transfer, fund_id, direction, state, amount, happened, period_id "
            "FROM receipts WHERE line_id=? ORDER BY happened", (line_id,))]
        periods = sorted({r["period_id"] for r in receipts if r["period_id"]})
        return {"line": line_id, "events": events, "receipts": receipts,
                "settlement_periods": periods,
                "reconciliation": self.line_reconciliation(line_id)}

    # ------------------------------------------------------------------ 企业视图
    def company_dashboard(self, voucher_id):
        """申领企业只能看到自己应付和仍可委托的服务，不含他人额度与财务资料。"""
        voucher = self.get_voucher(voucher_id)
        out = {"voucher": voucher_id, "company": voucher["company"], "use": voucher["use_text"],
               "plans": []}
        for plan in self.db.execute("SELECT * FROM plans WHERE voucher=?", (voucher_id,)):
            lines_view = []
            for line_id in self.plan_line_ids(plan["id"]):
                line = self.get_line(line_id)
                ob = self.db.execute(
                    "SELECT * FROM company_obligations WHERE plan_id=? AND line_id=?",
                    (plan["id"], line_id)).fetchone()
                board = next(b for b in self.work_board(plan["id"])["board"]
                             if b["line"] == line_id)
                lines_view.append({
                    "line": line_id, "fee": line["fee"],
                    "company_base_self_pay": ob["base_amount"] if ob else None,
                    "company_makeup": ob["makeup_amount"] if ob else None,
                    "company_total_due": (ob["base_amount"] + ob["makeup_amount"]) if ob else None,
                    "stage": board["stage"], "can_commission": board["can_start"],
                    "pending_device": board["pending_device"],
                })
            out["plans"].append({"plan": plan["id"], "provider": plan["provider"],
                                 "status": plan["status"],
                                 "confirmed_version": plan["confirmed_version"],
                                 "lines": lines_view})
        return out
