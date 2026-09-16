import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import httpx

from .db import connect, now
from .parser import BASE, Blocked, CrawlError, parse_detail, parse_list


def retry_seconds(value):
    try:
        return max(0, int(value))
    except (ValueError, TypeError):
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 0


class Crawler:
    def __init__(self, config, run_id, client=None, sleep=time.sleep):
        self.config, self.run_id, self.sleep = config, run_id, sleep
        self.client = client or httpx.Client(
            timeout=25,
            follow_redirects=False,
            headers={
                "User-Agent": "DDTPriceTracker/0.1 (personal price monitoring)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        self.requested = False

    def log(self, url, status, count=None, error=None):
        with connect() as conn:
            conn.execute(
                "INSERT INTO crawl_pages(run_id,url,status,count,error,created_at) VALUES (?,?,?,?,?,?)",
                (self.run_id, url, status, count, error, now()),
            )
            if status == "parsed":
                conn.execute("UPDATE crawl_runs SET pages_done=pages_done+1 WHERE id=?", (self.run_id,))

    def get(self, url):
        for attempt in range(self.config["retries"] + 1):
            if self.requested:
                self.sleep(random.uniform(self.config["delay_min"], self.config["delay_max"]))
            self.requested = True
            try:
                res = self.client.get(url)
                if res.status_code == 429:
                    raise Blocked(
                        "网站限流（429），已暂停全部采集请求", retry_seconds(res.headers.get("Retry-After"))
                    )
                if res.status_code in (401, 403) or res.is_redirect:
                    raise Blocked(f"网站限制访问或要求跳转（{res.status_code}），请人工检查")
                if res.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"网站服务异常 {res.status_code}", request=res.request, response=res
                    )
                if res.status_code != 200:
                    raise CrawlError(f"页面返回 HTTP {res.status_code}，不能确认商品状态")
                if "text/html" not in res.headers.get("Content-Type", ""):
                    raise CrawlError("网站未返回 HTML 页面")
                return res.text
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                self.log(url, "retry", error=str(exc)[:250])
                if attempt >= self.config["retries"]:
                    raise CrawlError(f"请求重试耗尽：{type(exc).__name__}") from exc
                self.sleep(min(60, 2 ** (attempt + 2) + random.random() * 3))
        raise CrawlError("请求未完成")

    def collect(self):
        result = {}
        for source, status in (("3", "listed"), ("2", "publicity")):
            seen = {}
            total, pages = None, 1
            page = 1
            first_ids = None
            while page <= pages:
                url = f"{BASE}/goods/search.html?" + urlencode(
                    dict(game_id=80, server_id=0, cat_id=1, status=source, page=page)
                )
                try:
                    parsed = parse_list(self.get(url), status, url)
                    if total is None:
                        total, pages = parsed.total, parsed.pages
                        first_ids = {i.id for i in parsed.items}
                    if parsed.total != total or parsed.pages != pages:
                        raise CrawlError("扫描期间商品总数或页数改变，下轮重试，未发布本批次")
                    for item in parsed.items:
                        if item.id in seen:
                            raise CrawlError("跨页商品重复，列表可能移动，未发布本批次")
                        seen[item.id] = item
                    self.log(url, "parsed", len(parsed.items))
                    with connect() as conn:
                        conn.execute(
                            "UPDATE crawl_runs SET product_count=? WHERE id=?",
                            (len(result) + len(seen), self.run_id),
                        )
                except CrawlError as exc:
                    self.log(url, "failed", error=str(exc))
                    raise
                page += 1
            if len(seen) != total:
                raise CrawlError(f"完整性校验失败：应有 {total} 个，实际 {len(seen)} 个")
            if pages > 1:
                url = f"{BASE}/goods/search.html?" + urlencode(
                    dict(game_id=80, server_id=0, cat_id=1, status=source, page=1)
                )
                check = parse_list(self.get(url), status, url)
                self.log(url, "rechecked", len(check.items))
                if check.total != total or check.pages != pages or {i.id for i in check.items} != first_ids:
                    raise CrawlError("首页复核发现列表变化，等待下一批次")
            if set(result) & set(seen):
                raise CrawlError("商品跨来源状态重复，可能正在转上架，下轮重试")
            result.update(seen)
            with connect() as conn:
                conn.execute("UPDATE crawl_runs SET product_count=? WHERE id=?", (len(result), self.run_id))
        return list(result.values())

    def missing_evidence(self, items):
        present = {i.id for i in items}
        with connect() as conn:
            previous = list(conn.execute("SELECT id,url FROM products WHERE missing_count<2"))
        evidence = {}
        for row in previous:
            if row["id"] in present:
                continue
            try:
                html = self.get(row["url"])
                evidence[row["id"]] = parse_detail(html, row["url"])
                self.log(row["url"], "detail", error=evidence[row["id"]][1])
            except Blocked:
                raise
            except CrawlError as exc:
                evidence[row["id"]] = (None, str(exc))
                self.log(row["url"], "detail_unknown", error=str(exc))
        return evidence

    def close(self):
        self.client.close()
