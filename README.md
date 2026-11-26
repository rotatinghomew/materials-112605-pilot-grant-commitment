# 中试券联合出资与履约补缴

区域中试中心和出资方以企业持券计划、分项签认及银行回执办理对账。

《中试券申报交换》说明在 [交换约定](docs/exchange.md)，办理后端的端点与规则在 [后端说明](docs/backend.md)。`fixtures/exchange.json` 是本机联调使用的合成记录。项目当前可读取这些交换文件，回放对接方回执，并验证资料之间的引用。

运行环境为 Python 3.11.2，只依赖标准库。执行 `python3 -m unittest discover -s tests -v` 检查交换资料与办理规则。

- `python3 partner.py`：对接方回执回放，回环地址 8087 端口，`GET /health` 返回服务状态；模拟查询失败时启动传入 `--unavailable`。
- `python3 server.py --fixture fixtures/exchange.json`：线上办理后端，默认 8088 端口，请求通过 `X-Actor: role:subject` 表明身份。后端覆盖计划报价与同版本确认、季度额度竞争、里程碑待查看板、证据更正与接替重办、回执入账与防重复付款、结算期关账、争议追溯及企业视图隔离。
