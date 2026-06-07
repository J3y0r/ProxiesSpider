from __future__ import annotations

import argparse
import concurrent.futures as futures
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - 运行时依赖提示
    requests = None

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT_DIR = PROJECT_DIR / "output"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
DEFAULT_TEST_URL = "https://httpbin.org/ip"
DEFAULT_TIMEOUT = 8.0
DEFAULT_CONCURRENCY = 50
SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks5"}


@dataclass(frozen=True)
class ProxyItem:
    raw: str
    scheme: str
    host: str
    port: int

    @property
    def proxy_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def output_key(self) -> str:
        return self.scheme


@dataclass(frozen=True)
class CheckResult:
    proxy: ProxyItem
    ok: bool
    elapsed: float
    reason: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="并发测试代理可用性，并按协议输出可用代理。")
    parser.add_argument(
        "--input",
        nargs="*",
        type=Path,
        default=None,
        help=f"输入代理文件；默认读取 {DEFAULT_INPUT_DIR / 'http.txt'}、{DEFAULT_INPUT_DIR / 'socks4.txt'}、{DEFAULT_INPUT_DIR / 'socks5.txt'}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"可用代理输出目录。默认：{DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument("--test-url", default=DEFAULT_TEST_URL, help=f"连通性检测地址。默认：{DEFAULT_TEST_URL}")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"单个代理请求超时秒数。默认：{DEFAULT_TIMEOUT}")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"并发数。默认：{DEFAULT_CONCURRENCY}",
    )
    return parser.parse_args()


def load_proxy_items(input_files: list[Path] | None) -> list[ProxyItem]:
    paths = input_files or [DEFAULT_INPUT_DIR / "http.txt", DEFAULT_INPUT_DIR / "socks4.txt", DEFAULT_INPUT_DIR / "socks5.txt"]
    items: list[ProxyItem] = []
    seen: set[str] = set()

    for path in paths:
        if not path.exists():
            print(f"[跳过] 输入文件不存在：{path}", flush=True)
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            item = parse_proxy_line(raw)
            if not item:
                print(f"[跳过] 非法代理格式：{raw}", flush=True)
                continue
            key = item.proxy_url
            if key in seen:
                continue
            seen.add(key)
            items.append(item)

    return items


def parse_proxy_line(raw: str) -> ProxyItem | None:
    parsed = urlparse(raw)
    if parsed.scheme not in SUPPORTED_SCHEMES:
        return None
    if not parsed.hostname or parsed.port is None:
        return None
    return ProxyItem(raw=raw, scheme=parsed.scheme, host=parsed.hostname, port=parsed.port)


def check_proxy(proxy: ProxyItem, test_url: str, timeout: float) -> CheckResult:
    started = time.perf_counter()
    proxies = {"http": proxy.proxy_url, "https": proxy.proxy_url}
    if proxy.scheme.startswith("socks"):
        proxies = {"http": proxy.proxy_url, "https": proxy.proxy_url}

    try:
        resp = requests.get(test_url, proxies=proxies, timeout=timeout)
        resp.raise_for_status()
        elapsed = time.perf_counter() - started
        return CheckResult(proxy=proxy, ok=True, elapsed=elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - started
        return CheckResult(proxy=proxy, ok=False, elapsed=elapsed, reason=short_error(exc))


def short_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 180 else text[:177] + "..."


def write_lines(path: Path, lines: Iterable[str]) -> None:
    items = list(lines)
    path.write_text("\n".join(items) + ("\n" if items else ""), encoding="utf-8")


def write_results(results: list[CheckResult], output_dir: Path) -> None:
    output_dir.mkdir(exist_ok=True)
    buckets: dict[str, list[str]] = {scheme: [] for scheme in SUPPORTED_SCHEMES}
    for result in results:
        if result.ok:
            buckets[result.proxy.output_key].append(result.proxy.proxy_url)

    for scheme, lines in buckets.items():
        write_lines(output_dir / f"available_{scheme}.txt", lines)


def main() -> None:
    args = parse_args()

    if requests is None:
        print("缺少依赖：requests[socks]", flush=True)
        print("请先安装：python -m pip install \"requests[socks]\"", flush=True)
        return

    proxies = load_proxy_items(args.input)
    if not proxies:
        print("未读取到可测试代理。", flush=True)
        return

    print(f"共读取 {len(proxies)} 个代理，开始并发测试...", flush=True)
    results: list[CheckResult] = []
    started_all = time.perf_counter()

    with futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        future_map = {executor.submit(check_proxy, proxy, args.test_url, args.timeout): proxy for proxy in proxies}
        for index, future in enumerate(futures.as_completed(future_map), start=1):
            result = future.result()
            results.append(result)
            status = "可用" if result.ok else "不可用"
            detail = f" - {result.reason}" if result.reason else ""
            print(
                f"[{index}/{len(proxies)}] {status} {result.proxy.proxy_url} 耗时 {result.elapsed:.2f}s{detail}",
                flush=True,
            )

    elapsed_all = time.perf_counter() - started_all
    ok_results = [r for r in results if r.ok]
    fail_count = len(results) - len(ok_results)
    write_results(results, args.output_dir)

    print("" , flush=True)
    print(f"总计: {len(results)}", flush=True)
    print(f"可用: {len(ok_results)}", flush=True)
    print(f"不可用: {fail_count}", flush=True)
    print(f"成功率: {len(ok_results) / len(results) * 100:.2f}%", flush=True)
    print(f"总耗时: {elapsed_all:.2f}s", flush=True)
    print(f"输出目录: {args.output_dir.resolve()}", flush=True)


if __name__ == "__main__":
    main()
