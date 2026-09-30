import hmac
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from starlette.middleware.gzip import GZipMiddleware

from .db import connect, enqueue, init_db, settings
from .worker import after

PRODUCTS_SQL = (Path(__file__).parent / "sql" / "products.sql").read_text(encoding="utf-8")


@asynccontextmanager
async def lifespan(app):
    init_db()
    yield


app = FastAPI(title="弹弹堂号价跟踪", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.middleware("http")
async def protect(request: Request, call_next):
    if not os.getenv("DDT_API_TOKEN") and request.url.hostname not in (
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
    ):
        return JSONResponse({"detail": "未配置访问令牌时仅允许本机域名访问"}, status_code=403)
    if request.url.path.startswith("/api/"):
        token = os.getenv("DDT_API_TOKEN", "")
        if token and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            return JSONResponse({"detail": "请输入正确的访问令牌"}, status_code=401)
        # Non-browser API clients are supported; browser writes require same origin and a custom header.
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不允许跨站操作"}, status_code=403)
            if request.headers.get("x-ddt-client") != "dashboard":
                return JSONResponse({"detail": "缺少请求校验头"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class SettingsModel(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=60, ge=15, le=10080)
    delay_min: float = Field(default=3, ge=2, le=60)
    delay_max: float = Field(default=8, ge=2, le=120)
    retries: int = Field(default=3, ge=0, le=3)
    cooldown_minutes: int = Field(default=60, ge=15, le=1440)
    drop_threshold: float = Field(default=0.3, ge=0.1, le=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.delay_max < self.delay_min:
            raise ValueError("最大请求间隔不能小于最小请求间隔")
        return self


def run_dict(row):
    if not row:
        return None
    value = dict(row)
    value["summary"] = json.loads(value["summary"])
    return value


@app.get("/api/overview")
def overview():
    with connect() as conn:
        latest = conn.execute(
            "SELECT * FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        active = conn.execute(
            "SELECT * FROM crawl_runs WHERE status IN ('queued','running') LIMIT 1"
        ).fetchone()
        runtime = dict(conn.execute("SELECT * FROM runtime WHERE id=1").fetchone())
        threshold = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(timespec="seconds")
        runtime["worker_online"] = bool(runtime["heartbeat"] and runtime["heartbeat"] > threshold)
        counts = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM products GROUP BY status")}
        return dict(latest=run_dict(latest), active=run_dict(active), runtime=runtime, counts=counts)


@app.get("/api/products")
def products(
    q: str = "",
    server: str = "",
    status: str = "",
    change: str = "",
    min_price: int | None = Query(None, ge=0),
    max_price: int | None = Query(None, ge=0),
    sort: Literal[
        "latest", "price_asc", "price_desc", "drop", "drop_percent",
        "delta_asc", "delta_desc", "total_delta_asc", "total_delta_desc",
    ] = "latest",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    conditions, params = ["1=1"], []
    if q:
        conditions.append("(p.title LIKE ? OR p.id LIKE ?)")
        params.extend([f"%{q}%"] * 2)
    for key, val in (("server", server), ("status", status)):
        if val:
            conditions.append(f"p.{key}=?")
            params.append(val)
    if min_price is not None:
        conditions.append("p.price>=?")
        params.append(min_price)
    if max_price is not None:
        conditions.append("p.price<=?")
        params.append(max_price)
    with connect() as conn:
        last = conn.execute(
            "SELECT id FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        rid = last[0] if last else None
        if change:
            conditions.append(
                "EXISTS(SELECT 1 FROM change_events e WHERE e.product_id=p.id AND e.run_id=? AND e.kind=?)"
            )
            params.extend([rid, change])
        where = " AND ".join(conditions)
        total = conn.execute(f"SELECT COUNT(*) FROM products p WHERE {where}", params).fetchone()[0]
        order = {
            "latest": "p.first_seen DESC,p.id DESC",
            "price_asc": "p.price ASC,p.id",
            "price_desc": "p.price DESC,p.id",
            "drop": "COALESCE(delta,0) ASC,p.id",
            "drop_percent": "COALESCE(percent,0) ASC,p.id",
            "delta_asc": "COALESCE(delta,0) ASC,p.id",
            "delta_desc": "COALESCE(delta,0) DESC,p.id",
            "total_delta_asc": "total_delta ASC NULLS LAST,p.id",
            "total_delta_desc": "total_delta DESC NULLS LAST,p.id",
        }[sort]
        rows = conn.execute(
            PRODUCTS_SQL.format(where=where, order=order),
            [rid, *params, page_size, (page - 1) * page_size],
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["changes"] = [
                r[0]
                for r in conn.execute(
                    "SELECT kind FROM change_events WHERE run_id=? AND product_id=?", (rid, row["id"])
                )
            ]
            previous = conn.execute(
                """SELECT price FROM product_snapshots
                WHERE product_id=? AND run_id<? AND present=1 ORDER BY run_id DESC LIMIT 1""",
                (row["id"], rid),
            ).fetchone()
            value["previous_price"] = previous[0] if previous else None
            first_price = value.pop("first_price")
            value["total_percent"] = (
                round(value["total_delta"] / first_price * 100, 2) if first_price else None
            )
            result.append(value)
        servers = [r[0] for r in conn.execute("SELECT DISTINCT server FROM products ORDER BY server")]
        return dict(items=result, total=total, servers=servers, run_id=rid)


@app.get("/api/products/{pid}")
def product(pid: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            raise HTTPException(404, "商品不存在")
        return dict(row)


@app.get("/api/products/{pid}/history")
def history(pid: str):
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM products WHERE id=?", (pid,)).fetchone():
            raise HTTPException(404, "商品不存在")
        snapshots = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM product_snapshots WHERE product_id=? ORDER BY run_id", (pid,)
            )
        ]
        events = [
            dict(r)
            for r in conn.execute("SELECT * FROM change_events WHERE product_id=? ORDER BY id DESC", (pid,))
        ]
        failed = [
            dict(r)
            for r in conn.execute(
                """SELECT id,started_at,finished_at,status FROM crawl_runs
            WHERE status IN ('failed','blocked','interrupted') AND created_at >=
            (SELECT first_seen FROM products WHERE id=?) ORDER BY id""",
                (pid,),
            )
        ]
        return dict(snapshots=snapshots, events=events, gaps=failed)


@app.get("/api/changes")
def changes(run_id: int | None = None, page: int = Query(1, ge=1)):
    with connect() as conn:
        if run_id is None:
            last = conn.execute(
                "SELECT id FROM crawl_runs WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            run_id = last[0] if last else None
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM change_events WHERE run_id=? ORDER BY id DESC LIMIT 100 OFFSET ?",
                (run_id, (page - 1) * 100),
            )
        ]


@app.post("/api/crawls")
def crawl():
    try:
        return run_dict(enqueue())
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/crawls")
def crawls(page: int = Query(1, ge=1)):
    with connect() as conn:
        return dict(
            items=[
                run_dict(r)
                for r in conn.execute(
                    "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 20 OFFSET ?", ((page - 1) * 20,)
                )
            ],
            total=conn.execute("SELECT COUNT(*) FROM crawl_runs").fetchone()[0],
        )


@app.get("/api/crawls/{rid}")
def crawl_detail(rid: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM crawl_runs WHERE id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        return dict(
            run=run_dict(row),
            pages=[
                dict(r) for r in conn.execute("SELECT * FROM crawl_pages WHERE run_id=? ORDER BY id", (rid,))
            ],
        )


@app.get("/api/settings")
def get_settings():
    return settings()


@app.patch("/api/settings")
def update_settings(value: SettingsModel):
    with connect() as conn:
        previous = json.loads(conn.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        conn.execute("UPDATE settings SET value=? WHERE id=1", (value.model_dump_json(),))
        if value.enabled != previous["enabled"] or value.interval_minutes != previous["interval_minutes"]:
            conn.execute(
                "UPDATE runtime SET next_run_at=? WHERE id=1",
                (after(value.interval_minutes * 60) if value.enabled else None,),
            )
    return value


static = Path(__file__).parent / "static"
if static.exists():
    app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
