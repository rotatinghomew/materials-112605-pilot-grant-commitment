# 中试券联合出资办理后端

线上办理后端，围绕《中试券申报交换》的项目条款实现计划报价、同版本确认、
里程碑看板、证据更正、接替重办、回执入账、结算期与争议追溯。

## 运行

```bash
python3 server.py --db var/voucher.db --fixture fixtures/exchange.json --port 8088
```

只依赖 Python 3.11 标准库。`--fixture` 可选，载入时只登记交换资料原文，
不会自动办理更正或把回执入台账——读到回执不等于本地已做过相应操作。

## 身份与角色

所有业务请求带 `X-Actor: role:subject` 头，角色为：

| 角色 | 可办理 |
| --- | --- |
| `company` | 按券提交计划、查询报价与本企业看板 |
| `provider` | 上报人工/设备里程碑、分项对账、登记回执 |
| `fund` | 登记出资认可、证据更正、发起争议、付款 |
| `center` | 确认计划、办理更正与重办、关结算期、裁决争议 |

`GET /health` 不需要身份。企业只能查看本企业持券的应付与可委托服务，
拿不到其他申请者的额度和财务资料。

## 端点

| 方法与路径 | 说明 |
| --- | --- |
| `POST /v1/exchange/load` | 按交换进件格式整体载入资料 |
| `POST /v1/plans` | 企业按券提交计划（机构可拆多个服务项） |
| `GET  /v1/plans/{id}/quotes/{version}` | 各方可能承担金额与可否确认 |
| `POST /v1/approvals` | 出资方登记某版本的分项认可 |
| `POST /v1/plans/{id}/confirm/{version}` | 同版本足额组合确认并原子占用季度额度 |
| `POST /v1/work` | 人工声明/设备回执分别到达 |
| `GET  /v1/plans/{id}/work-board` | 下一步工序、待查项与前序约束 |
| `POST /v1/changes` `/v1/changes/{id}/apply` | 登记并办理证据更正 |
| `POST /v1/plans/{p}/lines/{l}/reprocess/{v}` | 更正后接替重办 |
| `POST /v1/receipts` | 对手方付款/退款/撤销/冲回（交换进件格式） |
| `POST /v1/dispatches` | 本地向机构或企业发起付款 |
| `GET  /v1/lines/{id}/reconciliation` | 机构分项对账 |
| `GET  /v1/lines/{id}/trace` | 一笔资助的完整资金链路与结算期 |
| `POST /v1/periods` `/v1/periods/{id}/close` | 结算期开立与关账 |
| `POST /v1/disputes` `/v1/disputes/{id}/resolve` | 争议发起与裁决 |
| `GET  /v1/funds/{id}/capacity?quarter=` | 季度额度余量 |
| `GET  /v1/vouchers/{id}/dashboard` | 企业自助视图（隔离） |

## 关键业务规则

### 费用覆盖（按 line 计算）

- 每个出资方的分项认可金额不得超过该 line 的项目上限；证据更正办理后
  以 `new_ceiling` 覆盖目录上限。
- 企业自付 `fee - 各出资方认可之和` 不得低于 `self_min`。
- `exclusive=true` 的分项至多由一个出资方承担资助部分。
- 计划内各方必须认可同一版本，且每个分项都有至少一家出资方、
  资金加企业自付恰好覆盖约定费用，计划才可确认。

### 额度竞争与开工条件

- 确认在一把写锁内完成“复核季度余量→落承诺”，并行办理时先到者占用；
  后来者得到 `409 capacity_exhausted`。
- 半额分项承诺不构成出工条件：未确认或覆盖不足的分项 `can_start=false`。
- 后道工序受 `after` 前序约束；前序未设备验收不可继续。
- 人工 `work_done` 已到而设备回执未到时，分项停在 `pending_device` 待查。

### 证据更正与接替

- 未开工（`not_started`）分项上的受影响承诺直接 `released`，释放额度。
- 已开工/已完成分项上的承诺转 `legacy`：已合规履约的费用仍须找到付款人，
  额度在对账明确（结算期关账）前不释放。
- 接替方只能按其新版认可的额度和范围承担；超额认可在登记或重办时被拒。
- 无人接替的缺口由企业补缴（`company_makeup`）。
- 机构净入账超过分项费用时拒绝新的付款派发，避免重复向机构付款。

### 回执、在途与结算期

- 回执沿用交换进件格式，按 `transfer` 幂等；`received` 才写入台账，
  入账时点取业务时间 `happened`，不是 `arrived`。
- 付款拒绝后不形成台账，可用新 `transfer` 按实际余量重新办理。
- 已发出未收回执的款项处于在途；回执到达后回写 `settled/failed`。
- 关账时每条台账分录只能归入一个结算期；遗留承诺按机构净入账与新方案
  最终承担的差额调减，追清后转 `refunded`，额度此时才释放。
- 争议可针对分项发起，`trace` 展示承诺、更正、回执、补缴、追还与所属结算期。
