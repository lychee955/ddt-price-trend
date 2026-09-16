import logging
import time
from datetime import datetime, timedelta, timezone

import portalocker
from apscheduler.schedulers.background import BackgroundScheduler

from .compare import publish
from .crawler import Crawler
from .db import connect, data_dir, enqueue, init_db, now, settings
from .parser import Blocked, CrawlError

log = logging.getLogger(__name__)


def after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def heartbeat():
    with connect() as conn:
        conn.execute("UPDATE runtime SET heartbeat=? WHERE id=1", (now(),))


def recover():
    with connect() as conn:
        conn.execute(
            """UPDATE crawl_runs SET status='interrupted',finished_at=?,error=?
                     WHERE status='running'""",
            (now(), "采集进程上次中断，保留原成功基准"),
        )


def schedule():
    conf = settings()
    with connect() as conn:
        runtime = dict(conn.execute("SELECT * FROM runtime WHERE id=1").fetchone())
        if not conf["enabled"]:
            conn.execute("UPDATE runtime SET next_run_at=NULL WHERE id=1")
            return
        if not runtime["next_run_at"]:
            conn.execute(
                "UPDATE runtime SET next_run_at=? WHERE id=1", (after(conf["interval_minutes"] * 60),)
            )
            return
    if runtime["next_run_at"] <= now():
        try:
            enqueue("scheduled")
        except ValueError:
            pass
        with connect() as conn:
            next_at = after(conf["interval_minutes"] * 60)
            if runtime["cooldown_until"] and runtime["cooldown_until"] > next_at:
                next_at = runtime["cooldown_until"]
            conn.execute("UPDATE runtime SET next_run_at=? WHERE id=1", (next_at,))


def run_next():
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        runtime = conn.execute("SELECT * FROM runtime WHERE id=1").fetchone()
        if runtime["cooldown_until"] and runtime["cooldown_until"] > now():
            return False
        run = conn.execute("SELECT id FROM crawl_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not run:
            return False
        rid = run[0]
        conn.execute("UPDATE crawl_runs SET status='running',started_at=? WHERE id=?", (now(), rid))
    conf = settings()
    crawler = Crawler(conf, rid)
    try:
        items = crawler.collect()
        # Check collapse before spending requests on potentially hundreds of missing detail pages.
        with connect() as conn:
            last = conn.execute(
                "SELECT product_count FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if last and last[0] and len(items) < last[0] * (1 - conf["drop_threshold"]):
            raise CrawlError("商品数量异常骤减，未发布批次，未请求消失商品详情")
        evidence = crawler.missing_evidence(items)
        publish(rid, items, evidence, conf["drop_threshold"])
        log.info("Batch %s completed: %s products", rid, len(items))
    except Exception as exc:
        log.exception("Batch %s failed", rid)
        with connect() as conn:
            failures = conn.execute("SELECT failures FROM runtime WHERE id=1").fetchone()[0] + 1
            blocked = isinstance(exc, Blocked)
            cool = (
                after(max(conf["cooldown_minutes"] * 60, getattr(exc, "retry_after", 0)))
                if blocked or failures >= 3
                else None
            )
            message = (
                str(exc)[:500]
                if isinstance(exc, CrawlError)
                else f"内部错误：{type(exc).__name__}，请检查运行日志"
            )
            conn.execute(
                "UPDATE crawl_runs SET status=?,finished_at=?,error=? WHERE id=?",
                ("blocked" if blocked else "failed", now(), message, rid),
            )
            conn.execute(
                "UPDATE runtime SET failures=?,last_error=?,cooldown_until=? WHERE id=1",
                (failures, message, cool),
            )
    finally:
        crawler.close()
    return True


def worker(once=False):
    init_db()
    # OS lock is released by process death; stale DB heartbeats alone never steal live ownership.
    try:
        with portalocker.Lock(str(data_dir() / "worker.lock"), timeout=0):
            recover()
            heartbeat()
            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(heartbeat, "interval", seconds=10, max_instances=1, coalesce=True)
            scheduler.start()
            try:
                if once:
                    run_next()
                    return
                while True:
                    schedule()
                    run_next()
                    time.sleep(1)
            finally:
                scheduler.shutdown(wait=True)
                with connect() as conn:
                    conn.execute("UPDATE runtime SET heartbeat=NULL WHERE id=1")
    except portalocker.exceptions.LockException:
        if once:
            return
        raise RuntimeError("已有采集进程运行，不能重复启动") from None
