import argparse
import html as html_lib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

FREEVPNNODE_BASE_URL = "https://cn.freevpnnode.com/free-proxy/"
PROXYHUB_CN_URL = "https://proxyhub.me/en/cn-free-proxy-list.html"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
PAGE_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PROTOCOLS = ("http", "socks4", "socks5")
HIDDEN_EMPTY_VALUES = {"", "no need", "****", "hidden text"}


@dataclass(frozen=True)
class ProxyRecord:
    ip: str
    port: str
    username: str
    password: str
    protocol: str

    @property
    def has_auth(self) -> bool:
        return bool(self.username and self.password)

    @property
    def raw_line(self) -> str:
        if self.has_auth:
            return f"{self.ip}:{self.port}@{self.username}:{self.password}"
        return f"{self.ip}:{self.port}"

    @property
    def default_line(self) -> str:
        if self.has_auth:
            return f"{self.protocol}://{self.username}:{self.password}@{self.ip}:{self.port}"
        return f"{self.protocol}://{self.ip}:{self.port}"


class BaseScraper:
    source_name = "unknown"

    def __init__(self, delay: float):
        self.delay = delay

    def collect_all_records(self, max_pages: int | None) -> list[ProxyRecord]:
        raise NotImplementedError


class FreeVpnNodeScraper(BaseScraper):
    source_name = "freevpnnode"

    def collect_all_records(self, max_pages: int | None) -> list[ProxyRecord]:
        all_records: list[ProxyRecord] = []
        seen = set()
        page = 1
        fetched_pages = 0

        while page:
            if max_pages is not None and fetched_pages >= max_pages:
                print(f"达到页数限制：{max_pages} 页", flush=True)
                break

            print(f"[{self.source_name}] 正在抓取第 {page} 页...", flush=True)
            html = fetch_html(self.get_page_url(page))
            rows = self.extract_rows(html)
            if not rows:
                print(f"[{self.source_name}] 第 {page} 页未解析到代理，停止。", flush=True)
                break

            new_count = append_new_records(all_records, seen, rows)
            fetched_pages += 1
            print(f"[{self.source_name}] 第 {page} 页解析 {len(rows)} 条，新增 {new_count} 条，累计 {len(all_records)} 条。", flush=True)

            next_page = self.discover_next_page(html, page)
            if not next_page or next_page <= page:
                print(f"[{self.source_name}] 未发现下一页，抓取完成。", flush=True)
                break

            page = next_page
            sleep_if_needed(self.delay)

        return all_records

    @staticmethod
    def get_page_url(page: int) -> str:
        if page <= 1:
            return FREEVPNNODE_BASE_URL
        return f"{FREEVPNNODE_BASE_URL}?page={page}"

    @staticmethod
    def extract_rows(html: str) -> list[ProxyRecord]:
        tbody_match = re.search(r"<tbody>(.*?)</tbody>", html, re.S | re.I)
        if not tbody_match:
            return []

        tbody = tbody_match.group(1)
        rows = re.findall(r"<tr>(.*?)</tr>", tbody, re.S | re.I)
        records: list[ProxyRecord] = []

        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
            if len(cells) < 6:
                continue

            ip = strip_tags(cells[0])
            port = strip_tags(cells[1])
            username = parse_hidden_value(cells[2])
            password = parse_hidden_value(cells[3])
            protocol = strip_tags(cells[5]).lower()

            if ip and port and protocol in PROTOCOLS:
                records.append(ProxyRecord(ip=ip, port=port, username=username, password=password, protocol=protocol))

        return records

    @staticmethod
    def discover_next_page(html: str, current_page: int) -> int | None:
        pages = []
        for href in re.findall(r'href="([^"]+)"', html, re.I):
            if "page=" not in href:
                continue
            page = extract_page_number_from_url(href)
            if page and page > current_page:
                pages.append(page)
        return min(pages) if pages else None


class ProxyHubCnScraper(BaseScraper):
    source_name = "proxyhub-cn"

    def __init__(self, delay: float):
        super().__init__(delay)
        self.opener = build_opener(HTTPCookieProcessor())

    def collect_all_records(self, max_pages: int | None) -> list[ProxyRecord]:
        all_records: list[ProxyRecord] = []
        seen = set()
        page = 1
        fetched_pages = 0
        last_page = None

        while page:
            if max_pages is not None and fetched_pages >= max_pages:
                print(f"达到页数限制：{max_pages} 页", flush=True)
                break

            print(f"[{self.source_name}] 正在抓取第 {page} 页...", flush=True)
            html = self.fetch_page(page)
            rows = self.extract_rows(html)
            if not rows:
                print(f"[{self.source_name}] 第 {page} 页未解析到代理，停止。", flush=True)
                break

            new_count = append_new_records(all_records, seen, rows)
            fetched_pages += 1
            print(f"[{self.source_name}] 第 {page} 页解析 {len(rows)} 条，新增 {new_count} 条，累计 {len(all_records)} 条。", flush=True)

            if last_page is None:
                last_page = self.extract_last_page(html)
                if last_page:
                    print(f"[{self.source_name}] 检测到总页数：{last_page}", flush=True)

            if last_page is not None and page >= last_page:
                print(f"[{self.source_name}] 已到最后一页，抓取完成。", flush=True)
                break

            next_page = page + 1
            if max_pages is not None and fetched_pages >= max_pages:
                break

            page = next_page
            sleep_if_needed(self.delay)

        return all_records

    def fetch_page(self, page: int) -> str:
        headers = {"User-Agent": USER_AGENT}
        if page > 1:
            headers["Cookie"] = f"page={page}"
        req = Request(PROXYHUB_CN_URL, headers=headers)
        with self.opener.open(req, timeout=PAGE_TIMEOUT) as resp:
            raw = resp.read()
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def extract_rows(html: str) -> list[ProxyRecord]:
        tbody_match = re.search(r"<tbody>(.*?)</tbody>", html, re.S | re.I)
        if not tbody_match:
            return []

        tbody = tbody_match.group(1)
        rows = re.findall(r"<tr>(.*?)</tr>", tbody, re.S | re.I)
        records: list[ProxyRecord] = []

        for row in rows:
            ip = first_match(r'<span class="ip-text"[^>]*title="([^"]+)"', row)
            if not ip:
                ip = first_match(r'<span class="ip-text"[^>]*>([^<]+)</span>', row)

            port = first_match(r'<span class="port-text">([^<]+)</span>', row)
            protocol = first_match(r'<a[^>]+title="([^"]+)"', row)
            if not protocol:
                protocol = first_match(r'<td[^>]*data-label="Protocols"[^>]*>.*?<span>([^<]+)</span>', row)

            protocol = protocol.lower().strip() if protocol else ""
            if ip and port and protocol in PROTOCOLS:
                records.append(ProxyRecord(ip=ip.strip(), port=port.strip(), username="", password="", protocol=protocol))

        return records

    @staticmethod
    def extract_last_page(html: str) -> int | None:
        pages = [int(value) for value in re.findall(r'<span class="page-link"\s+page="(\d+)"', html)]
        return max(pages) if pages else None


def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=PAGE_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def strip_tags(text: str) -> str:
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_hidden_value(cell_html: str) -> str:
    for attr in ("data-text", "data-value", "data-content"):
        m = re.search(rf'{attr}="([^"]*)"', cell_html, re.I)
        if not m:
            continue
        value = normalize_hidden_value(m.group(1))
        if value:
            return value

    return normalize_hidden_value(strip_tags(cell_html))


def normalize_hidden_value(value: str) -> str:
    value = html_lib.unescape(value).strip()
    if value.lower() in HIDDEN_EMPTY_VALUES:
        return ""
    return value


def extract_page_number_from_url(url: str) -> int | None:
    qs = parse_qs(urlparse(url).query)
    values = qs.get("page")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.S | re.I)
    return html_lib.unescape(match.group(1)).strip() if match else ""


def append_new_records(all_records: list[ProxyRecord], seen: set[tuple[str, str, str, str, str]], rows: list[ProxyRecord]) -> int:
    new_count = 0
    for record in rows:
        key = (record.protocol, record.ip, record.port, record.username, record.password)
        if key in seen:
            continue
        seen.add(key)
        all_records.append(record)
        new_count += 1
    return new_count


def sleep_if_needed(delay: float) -> None:
    if delay > 0:
        time.sleep(delay)


def write_outputs(records: Iterable[ProxyRecord], output_dir: Path) -> None:
    output_dir.mkdir(exist_ok=True)
    buckets: dict[str, list[ProxyRecord]] = {protocol: [] for protocol in PROTOCOLS}
    for record in records:
        buckets[record.protocol].append(record)

    for protocol, items in buckets.items():
        default_lines = [record.default_line for record in items]
        raw_lines = [record.raw_line for record in items]
        write_lines(output_dir / f"{protocol}.txt", default_lines)
        write_lines(output_dir / f"{protocol}_raw.txt", raw_lines)


def write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def build_scraper(source: str, delay: float) -> BaseScraper:
    if source == "freevpnnode":
        return FreeVpnNodeScraper(delay=delay)
    if source == "proxyhub-cn":
        return ProxyHubCnScraper(delay=delay)
    raise ValueError(f"不支持的数据源: {source}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取免费代理并按协议分流输出。")
    parser.add_argument(
        "--source",
        choices=("freevpnnode", "proxyhub-cn"),
        default="freevpnnode",
        help="选择抓取数据源。freevpnnode=混合代理页，proxyhub-cn=ProxyHub 中国代理页。默认：freevpnnode",
    )
    parser.add_argument("--max-pages", type=int, default=10, help="最多抓取页数；传 0 表示抓取到最后一页。默认：10")
    parser.add_argument("--delay", type=float, default=0.25, help="翻页请求间隔秒数。默认：0.25")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"输出目录。默认：{DEFAULT_OUTPUT_DIR}")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_pages = None if args.max_pages == 0 else args.max_pages
    scraper = build_scraper(source=args.source, delay=args.delay)
    records = scraper.collect_all_records(max_pages=max_pages)
    write_outputs(records, args.output_dir)

    auth_count = sum(1 for r in records if r.has_auth)
    print(f"数据源: {args.source}", flush=True)
    print(f"已抓取 {len(records)} 条代理，其中 {auth_count} 条包含页面可见认证信息。", flush=True)
    print(f"输出目录: {args.output_dir.resolve()}", flush=True)


if __name__ == "__main__":
    main()
