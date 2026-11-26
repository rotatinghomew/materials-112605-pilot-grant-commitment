# 中试券申报交换

金额均为人民币分。资金覆盖上限按 line 计算，self_min 保留给企业，exclusive 表示该项至多由一个出资方承担资助部分。服务项验收的 milestone 及 principal 由机构确认，资助方更改范围只影响该资助方的资格，已经合规履约的费用仍须找到付款人。外部回执按 transfer 标识归属，入账时点采用 happened。

资料使用 UTF-8 JSON。每个集合各自维护记录 id，跨集合引用使用相应记录 id。事件中的 happened 是业务发生时间，arrived 是对接方可提供该回执的时间。到达时间不能代替业务时间。这些记录来自不同流程阶段，读到一个回执并不代表本地已做过相应操作。

## `funds`

季度出资方及公布额度。字段为 `id`, `quarter`, `capacity`。

## `vouchers`

企业持券、服务用途和授权出资人。字段为 `id`, `company`, `quarter`, `sponsors`, `use`。

## `plans`

委托分项及工序前置条件。字段为 `id`, `voucher`, `provider`, `lines`。

## `lines`

单价、自付下限、是否限制单一资助、可承担上限及前序。字段为 `id`, `fee`, `self_min`, `exclusive`, `limits`, `after`。

## `approvals`

办理中的出资认可，版本相同与否可据现有事实判断。字段为 `id`, `plan`, `version`, `fund`, `lines`, `signed`。

## `work`

服务人员声明和设备回执分别到达。字段为 `id`, `line`, `milestone`, `principal`, `actor_time`, `arrived`。

## `changes`

资助方证据变化或补充承担声明。字段为 `id`, `fund`, `voucher`, `scope`, `new_ceiling`, `occurred`。

## `receipts`

付款方向根据业务单号和相对方核算，amount 为绝对金额。字段为 `id`, `transfer`, `line`, `fund`, `direction`, `state`, `amount`, `happened`, `arrived`。

## 回执查询

本机回放只提供对接方的既有回执。`GET /v1/receipts?at=...` 的 `at` 是 URL 编码后的带偏移时间，返回对象中 `receipts` 是在该时间之前及当时可取得的回执。同一条数据可以在连续查询中出现，列表先后不代表业务先后。`503` 表示当前无法查询回执。
