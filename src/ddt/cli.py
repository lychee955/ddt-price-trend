import argparse
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import uvicorn

from .db import connect, data_dir, enqueue, init_db
from .worker import worker


def main():
    parser = argparse.ArgumentParser(description="弹弹堂账号价格跟踪")
    subs = parser.add_subparsers(dest="command", required=True)
    serve = subs.add_parser("serve", help="启动页面和采集进程")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    subs.add_parser("worker", help="单独启动采集进程")
    subs.add_parser("crawl", help="执行一次完整采集")
    subs.add_parser("backup", help="在线备份 SQLite")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    if args.command == "worker":
        worker()
    elif args.command == "crawl":
        try:
            run = enqueue("cli")
        except ValueError as exc:
            parser.exit(1, str(exc) + "\n")
        worker(once=True)
        while True:
            with connect() as conn:
                row = dict(conn.execute("SELECT * FROM crawl_runs WHERE id=?", (run["id"],)).fetchone())
            if row["status"] not in ("queued", "running"):
                print(row)
                sys.exit(0 if row["status"] == "success" else 1)
            time.sleep(2)
    elif args.command == "backup":
        target = data_dir() / "backups" / f"tracker-{datetime.now():%Y%m%d-%H%M%S-%f}.db"
        target.parent.mkdir(exist_ok=True)
        with connect() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        print(target)
    elif args.command == "serve":
        if args.host not in ("127.0.0.1", "localhost", "::1") and len(os.getenv("DDT_API_TOKEN", "")) < 24:
            parser.error("对外监听需设置至少 24 字符的 DDT_API_TOKEN，并使用 HTTPS 反向代理")
        if not (Path(__file__).parent / "static" / "index.html").is_file():
            parser.error("请先在 frontend 目录执行 npm ci 和 npm run build")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        child = subprocess.Popen([sys.executable, "-m", "ddt", "worker"], creationflags=flags)
        try:
            uvicorn.run("ddt.api:app", host=args.host, port=args.port)
        finally:
            child.terminate()
            child.wait(timeout=15)
