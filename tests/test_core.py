import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ddt.api import app
from ddt.compare import publish
from ddt.crawler import Crawler, retry_seconds
from ddt.db import DEFAULTS, connect, enqueue, now
from ddt.parser import Blocked, CrawlError, Item, parse_detail, parse_list
from ddt.worker import after, recover, run_next, schedule

FIXTURES = Path(__file__).parent / "fixtures"


def item(pid="1", price=10000, status="listed"):
    return Item(pid, "账号" + pid, "测试区", price, status, now())


def batch(items, **kwargs):
    run = enqueue()
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='running' WHERE id=?", (run["id"],))
    publish(run["id"], items, **kwargs)
    return run["id"]


def test_real_list_and_publicity():
    listed = parse_list((FIXTURES / "live-list.html").read_text(encoding="utf-8"), "listed")
    assert len(listed.items) == 20 and listed.pages == 7 and listed.total == 122
    assert listed.items[0].id == "2783950" and listed.items[0].price == 46600
    public = parse_list((FIXTURES / "live-publicity.html").read_text(encoding="utf-8"), "publicity")
    assert public.total == len(public.items) == 13


def test_first_baseline_prices_new_return_and_status():
    first = batch([item(), item("2"), item("3")])
    second = batch([item(price=8000), item("2", 12000, "publicity"), item("4")], drop_threshold=1)
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM change_events WHERE run_id=?", (first,)).fetchone()[0] == 0
        events = {
            (r[0], r[1]): (r[2], r[3])
            for r in conn.execute(
                "SELECT product_id,kind,delta,percent FROM change_events WHERE run_id=?", (second,)
            )
        }
        assert events[("1", "decreased")] == (-2000, -20)
        assert events[("2", "increased")] == (2000, 20)
        assert ("2", "status_changed") in events and ("4", "new") in events
        assert conn.execute("SELECT status FROM products WHERE id='3'").fetchone()[0] == "suspected_missing"
    batch([item(price=8000), item("2", 12000, "publicity"), item("4")])
    with connect() as conn:
        assert conn.execute("SELECT status FROM products WHERE id='3'").fetchone()[0] == "missing"
        assert (
            conn.execute(
                "SELECT price FROM product_snapshots WHERE product_id='3' ORDER BY run_id DESC"
            ).fetchone()[0]
            is None
        )
    last = batch([item(price=8000), item("2", 12000, "publicity"), item("3", 7000), item("4")])
    with connect() as conn:
        kinds = {
            r[0]
            for r in conn.execute("SELECT kind FROM change_events WHERE run_id=? AND product_id='3'", (last,))
        }
        assert kinds == {"returned", "decreased"}
        assert (
            conn.execute("SELECT COUNT(*) FROM product_snapshots WHERE run_id=?", (last,)).fetchone()[0] == 4
        )


def test_collapse_is_atomic_and_preserves_baseline():
    good = batch([item(str(i)) for i in range(10)])
    with pytest.raises(CrawlError, match="异常下降"):
        batch([item("0", 5000)])
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 10
        assert conn.execute("SELECT price FROM products WHERE id='0'").fetchone()[0] == 10000
        assert conn.execute("SELECT id FROM crawl_runs WHERE status='success'").fetchone()[0] == good


def test_enqueue_deduplicates_and_respects_cooldown():
    a, b = enqueue(), enqueue("scheduled")
    assert a["id"] == b["id"]
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='failed'")
        conn.execute("UPDATE runtime SET cooldown_until=? WHERE id=1", (after(3600),))
    with pytest.raises(ValueError, match="冷却"):
        enqueue()


def test_failed_batch_does_not_become_comparison_base():
    first = batch([item()])
    failed = enqueue()
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='failed' WHERE id=?", (failed["id"],))
    third = batch([item(price=9000)])
    with connect() as conn:
        assert conn.execute("SELECT base_run_id FROM crawl_runs WHERE id=?", (third,)).fetchone()[0] == first
        assert conn.execute("SELECT delta FROM change_events WHERE run_id=?", (third,)).fetchone()[0] == -1000


@pytest.mark.parametrize("html", ["<html>维护中</html>", "<title>安全验证</title>", "<p>请输入验证码</p>"])
def test_error_pages_not_empty_lists(html):
    with pytest.raises(CrawlError):
        parse_list(html, "listed")


def test_bad_price_and_wrong_scope_fail_closed():
    html = (FIXTURES / "live-list.html").read_text(encoding="utf-8")
    with pytest.raises(CrawlError):
        parse_list(html.replace("&yen; 466.00", "价格待定"), "listed")
    with pytest.raises(CrawlError, match="状态筛选"):
        parse_list(html, "publicity")


def test_on_sale_detail_not_misread_as_delisted():
    html = (FIXTURES / "live-detail.html").read_text(encoding="utf-8")
    assert parse_detail(html, "https://www.youxidian.com/goods/2783950.html")[0] is None
    assert parse_detail('<div class="goods_status">该商品已经下架</div>', "")[0] == "delisted"
    assert parse_detail('<div class="seller">商品已售出</div>', "")[0] is None


def test_rate_limit_stops_requests_and_preserves_retry_after():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "7200"})

    crawler = Crawler(
        DEFAULTS,
        enqueue()["id"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(Blocked) as caught:
        crawler.get("https://www.youxidian.com/goods/search.html")
    assert caught.value.retry_after == 7200 and len(calls) == 1
    crawler.close()
    assert retry_seconds("bad") == 0


def test_server_error_retry_bound():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    crawler = Crawler(
        DEFAULTS,
        enqueue()["id"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(CrawlError, match="重试耗尽"):
        crawler.get("https://www.youxidian.com/goods/search.html")
    assert len(calls) == 4
    crawler.close()


def test_worker_block_persists_cooldown(monkeypatch):
    def blocked(self):
        raise Blocked("访问受限", 7200)

    monkeypatch.setattr(Crawler, "collect", blocked)
    rid = enqueue()["id"]
    assert run_next()
    with connect() as conn:
        assert conn.execute("SELECT status FROM crawl_runs WHERE id=?", (rid,)).fetchone()[0] == "blocked"
        assert conn.execute("SELECT cooldown_until FROM runtime").fetchone()[0] > after(7100)
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0


def test_recover_only_running_and_schedule(monkeypatch):
    rid = enqueue()["id"]
    with connect() as conn:
        conn.execute("UPDATE crawl_runs SET status='running' WHERE id=?", (rid,))
    recover()
    with connect() as conn:
        assert conn.execute("SELECT status FROM crawl_runs").fetchone()[0] == "interrupted"
        conf = {**DEFAULTS, "enabled": True}
        conn.execute("UPDATE settings SET value=?", (json.dumps(conf),))
        conn.execute("UPDATE runtime SET next_run_at=?", (after(-60),))
    schedule()
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crawl_runs WHERE status='queued'").fetchone()[0] == 1
        assert conn.execute("SELECT next_run_at FROM runtime").fetchone()[0] > now()


def test_api_filters_settings_auth_and_csrf(monkeypatch):
    batch([item("1", 5000), item("2", 20000)])
    batch([item("1", 4500), item("2", 20000)])
    with TestClient(app) as client:
        result = client.get("/api/products?change=decreased&max_price=10000").json()
        assert result["total"] == 1 and result["items"][0]["previous_price"] == 5000
        assert client.get("/api/products/1/history").json()["snapshots"][1]["price"] == 4500
        assert client.get("/api/products/999/history").status_code == 404
        assert client.post("/api/crawls").status_code == 403
        headers = {"X-DDT-Client": "dashboard"}
        assert (
            client.post("/api/crawls", headers={**headers, "Origin": "https://evil.example"}).status_code
            == 403
        )
        assert (
            client.patch(
                "/api/settings", headers=headers, json={**DEFAULTS, "delay_min": 10, "delay_max": 3}
            ).status_code
            == 422
        )
        assert (
            client.patch("/api/settings", headers=headers, json={**DEFAULTS, "enabled": True}).status_code
            == 200
        )
        assert client.get("/api/overview").json()["runtime"]["next_run_at"]
        monkeypatch.setenv("DDT_API_TOKEN", "test-token")
        assert client.get("/api/products").status_code == 401
        assert client.get("/api/products", headers={"Authorization": "Bearer test-token"}).status_code == 200


def synthetic_page(ids, total, pages=1, status="listed"):
    cards = "".join(
        f"""<li class="list_item"><h3 class="desc"><a href="/goods/{pid}.html">账号 {pid}</a></h3>
        <div class="intro"><span class="info">账号</span><span class="info">弹弹堂 / 测试区</span></div>
        <li class="price">¥ 100.00</li></li>"""
        for pid in ids
    )
    return f'''<title>弹弹堂商品搜索</title><span id="label_cat">账号</span>
        <input id="{"sj" if status == "listed" else "gsq"}" checked>
        <div class="paginator_lite"><span class="pages">共 {total}个商品</span></div>
        <ul class="list_view">{cards}</ul><span>共{pages} 页</span>'''


@pytest.mark.parametrize("failure", ["duplicate", "missing", "total_change", "verification"])
def test_collection_refuses_incomplete_or_moving_pages(failure):
    def handler(request):
        page = request.url.params.get("page")
        if page == "1":
            html = synthetic_page([1], total=2, pages=2)
        else:
            html = {
                "duplicate": synthetic_page([1], 2, 2),
                "missing": synthetic_page([], 2, 2),
                "total_change": synthetic_page([2], 3, 2),
                "verification": "<title>安全验证</title>",
            }[failure]
        return httpx.Response(200, text=html, headers={"Content-Type": "text/html"})

    crawler = Crawler(
        DEFAULTS,
        enqueue()["id"],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(CrawlError):
        crawler.collect()
    crawler.close()


def test_empty_publicity_is_valid_but_scope_is_not_lost():
    parsed = parse_list(synthetic_page([], 0, status="publicity"), "publicity")
    assert parsed.items == [] and parsed.total == 0


def test_confirmed_delisting_keeps_price_history():
    batch([item()])
    batch([], evidence={"1": ("delisted", "明确下架提示")}, drop_threshold=1)
    with connect() as conn:
        row = conn.execute("SELECT * FROM products").fetchone()
        assert row["status"] == "delisted" and row["price"] == 10000
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 2


def test_third_failure_trips_circuit_without_mutations(monkeypatch):
    def fail(self):
        raise CrawlError("列表结构变化")

    monkeypatch.setattr(Crawler, "collect", fail)
    for _ in range(3):
        enqueue()
        run_next()
    with connect() as conn:
        assert conn.execute("SELECT failures FROM runtime").fetchone()[0] == 3
        assert conn.execute("SELECT cooldown_until FROM runtime").fetchone()[0] > now()
        assert conn.execute("SELECT COUNT(*) FROM product_snapshots").fetchone()[0] == 0


def test_concurrent_requests_share_one_task():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _: enqueue()["id"], range(16)))
    assert len(set(ids)) == 1
