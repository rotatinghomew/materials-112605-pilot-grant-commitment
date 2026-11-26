"""启动中试券办理服务。

用法：python3 server.py --db var/voucher.db --port 8088
可选 --fixture 在启动时载入本机联调的合成交换资料（只登记，不自动办理）。
"""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import argparse
import json

from voucher.api import make_handler, Service
from voucher.engine import Engine
from voucher.store import connect


def main():
    parser = argparse.ArgumentParser(description="中试券联合出资办理后端")
    parser.add_argument("--db", default="var/voucher.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--fixture", type=Path, help="启动时载入的交换资料 JSON")
    args = parser.parse_args()

    if args.db != ":memory:":
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = connect(args.db)
    if args.fixture is not None:
        bundle = json.loads(args.fixture.read_text(encoding="utf-8"))
        result = Engine(db).load_bundle(bundle)
        print(f"已载入交换记录 {result['count']} 条", flush=True)

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(Service(db)))
    print(f"http://{args.host}:{httpd.server_port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
