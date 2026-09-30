import argparse
import logging
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
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
    export = subs.add_parser("export", help="导出公开只读看板 JSON")
    export.add_argument("--output", type=Path, required=True, help="静态数据输出目录")
    pack = subs.add_parser("state-pack", help="打包并校验完整数据库状态")
    pack.add_argument("--output", type=Path, required=True)
    restore = subs.add_parser("state-restore", help="恢复校验通过的状态到空数据目录")
    restore.add_argument("--source", type=Path, required=True)
    for name, help_text, argument in (
        ("state-encrypt", "加密完整数据库备份", "--output"),
        ("state-decrypt", "解密并恢复完整备份到空数据目录", "--source"),
    ):
        command = subs.add_parser(name, help=help_text)
        command.add_argument(argument, type=Path, required=True)
        command.add_argument("--key-file", type=Path, help="本地密钥文件；省略时使用 DDT_BACKUP_KEY")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "state-decrypt":
        from .encrypted_state import read_encrypted

        read_encrypted(args.source, key_file=args.key_file, restore=True)
        print("加密状态已校验并恢复")
        return
    if args.command == "state-restore":
        from .state import restore_state

        restore_state(args.source)
        print("状态已恢复")
        return
    init_db()
    if args.command == "export":
        from .export import export_site

        print(export_site(args.output)["version"])
    elif args.command == "state-encrypt":
        from .encrypted_state import pack_encrypted

        pack_encrypted(args.output, key_file=args.key_file)
        print("完整状态已加密")
    elif args.command == "state-pack":
        from .state import pack_state

        print(pack_state(args.output)["sha256"])
    elif args.command == "worker":
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
        with connect() as source, closing(sqlite3.connect(target)) as dest:
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
