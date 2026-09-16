import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .db import now

BASE = "https://www.youxidian.com"


class CrawlError(Exception):
    pass


class Blocked(CrawlError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    server: str
    price: int
    status: str
    observed_at: str

    @property
    def url(self):
        return f"{BASE}/goods/{self.id}.html"


@dataclass
class Page:
    items: list[Item]
    total: int
    pages: int


def document(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    # Scripts, hidden login widgets, seller descriptions must not trigger false CAPTCHA detection.
    for tag in soup.select("script,style,noscript"):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    if re.search(r"/(?:login|captcha|verify)(?:[/.?]|$)", urlparse(url).path, re.I):
        raise Blocked("请求跳转到登录或验证页面，请人工检查")
    title = soup.title.get_text() if soup.title else ""
    if re.search(r"验证码|访问受限|安全验证|Access Denied|Just a moment", title, re.I):
        raise Blocked("网站返回访问验证页面，已停止采集")
    if not soup.select_one(".list_view,.intro_bx,.goods_detail") and re.search(
        r"访问过于频繁|请求过于频繁|请完成.*验证|请输入验证码|访问被拒绝", text
    ):
        raise Blocked("检测到限流或验证码，已停止采集")
    return soup


def parse_list(html: str, status: str, url="") -> Page:
    soup = document(html, url)
    total_node = soup.select_one(".paginator_lite .pages")
    match = re.search(r"共\s*(\d+)\s*个商品", total_node.get_text() if total_node else "")
    if not match or not soup.select_one("#label_cat"):
        raise CrawlError("列表结构异常：缺少商品总数或账号筛选，拒绝当作空列表")
    if "账号" not in soup.select_one("#label_cat").get_text() or "弹弹堂" not in soup.get_text():
        raise CrawlError("采集范围异常：不是弹弹堂账号列表")
    radio = soup.select_one("#sj" if status == "listed" else "#gsq")
    if not radio or not radio.has_attr("checked"):
        raise CrawlError("来源状态筛选不匹配，可能发生跳转")
    total = int(match[1])
    items = []
    for card in soup.select(".list_view > .list_item"):
        anchor = card.select_one("h3.desc a")
        price_node = card.select_one(".price")
        infos = card.select(".intro .info")
        if not anchor or not price_node or len(infos) < 2:
            raise CrawlError("商品卡片字段缺失")
        product_id = re.fullmatch(r"/goods/(\d+)\.html", anchor.get("href", ""))
        # Publicity cards embed the future listing time in <p> children of the price node.
        price_text = " ".join(price_node.find_all(string=True, recursive=False)).strip()
        money = re.fullmatch(r"[¥￥]\s*([\d,]+(?:\.\d{1,2})?)", price_text)
        title = anchor.get_text(" ", strip=True)
        region = infos[1].get_text(" ", strip=True)
        if not product_id or not money or not title or infos[0].get_text(strip=True) != "账号":
            raise CrawlError("商品 ID、价格或类型解析失败")
        if not region.startswith("弹弹堂 /"):
            raise CrawlError("商品所属游戏不匹配")
        items.append(
            Item(
                product_id[1],
                title,
                region.split("/", 1)[1].strip(),
                int(Decimal(money[1].replace(",", "")) * 100),
                status,
                now(),
            )
        )
    if total and not items:
        raise CrawlError("页面报告有商品但未解析到卡片")
    if not total and items:
        raise CrawlError("商品总数与卡片不一致")
    pages_match = re.search(r"共\s*(\d+)\s*页", soup.get_text(" ", strip=True))
    pages = int(pages_match[1]) if pages_match else 1
    if not 1 <= pages <= 500:
        raise CrawlError("分页数量异常")
    if len({item.id for item in items}) != len(items):
        raise CrawlError("同一页出现重复商品")
    return Page(items, total, pages)


def parse_detail(html: str, url: str) -> tuple[str | None, str]:
    soup = document(html, url)
    # Only explicit site status messages are evidence; free-form seller content is not.
    for node in soup.select(".goods_status,.goods-status,.status_tips,.error_msg,.msgbox"):
        text = node.get_text(" ", strip=True)
        if re.search(r"商品已(?:经)?售出|商品已成交|交易成功", text):
            return "sold", text[:200]
        if re.search(r"商品已(?:经)?下架|该商品已删除", text):
            return "delisted", text[:200]
    return None, "详情页未提供可确认的下架或成交标记"
