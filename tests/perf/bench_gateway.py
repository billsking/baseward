"""AC12 performance comparison: P95 of SELECT 1 through the gateway vs direct MySQL.

Usage (with both the gateway and the test MySQL reachable):

    python tests/perf/bench_gateway.py --base-url http://127.0.0.1:8080 \
        --token gw_xxx --datasource testds \
        --direct-host 127.0.0.1 --direct-port 3306 \
        --direct-user reader --direct-password '******' --direct-database testdb

Defaults to 10 concurrent workers and 200 requests; results go to docs/perf-report.md.
The script never prints tokens, database passwords, or password-bearing connection strings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

DEFAULT_REQUESTS = 200
DEFAULT_CONCURRENCY = 10
THRESHOLD_MS = 100.0
ERROR_SAMPLE_LIMIT = 5


@dataclass
class Measurements:
    label: str
    latencies: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(ratio * len(ordered)) - 1)
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None, "avg": None}
    return {
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
        "avg": sum(values) / len(values),
    }


def _split_counts(total: int, workers: int) -> list[int]:
    base, remainder = divmod(total, workers)
    return [base + (1 if index < remainder else 0) for index in range(workers)]


def _sanitize(message: str, secrets: list[str]) -> str:
    text = message.replace("\n", " ")[:300]
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


async def _mcp_worker(
    endpoint: str,
    token: str,
    datasource: str,
    count: int,
    timeout: float,
    measurements: Measurements,
    secrets: list[str],
) -> None:
    if count <= 0:
        return
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with streamablehttp_client(
            endpoint,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=timeout,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                warmup = await session.call_tool(
                    "execute_sql",
                    {"datasource": datasource, "sql": "SELECT 1"},
                )
                if getattr(warmup, "isError", False):
                    raise RuntimeError("预热调用失败")
                for _ in range(count):
                    started = time.perf_counter_ns()
                    result = await session.call_tool(
                        "execute_sql",
                        {"datasource": datasource, "sql": "SELECT 1"},
                    )
                    elapsed = (time.perf_counter_ns() - started) / 1_000_000
                    if getattr(result, "isError", False):
                        raise RuntimeError("工具返回错误")
                    measurements.latencies.append(elapsed)
    except Exception as exc:  # noqa: BLE001 - the benchmark records every failure
        measurements.errors.append(_sanitize(f"{type(exc).__name__}: {exc}", secrets))


async def _direct_worker(
    engine,
    count: int,
    measurements: Measurements,
    secrets: list[str],
) -> None:
    if count <= 0:
        return
    try:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
            for _ in range(count):
                started = time.perf_counter_ns()
                await conn.exec_driver_sql("SELECT 1")
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                measurements.latencies.append(elapsed)
    except Exception as exc:  # noqa: BLE001
        measurements.errors.append(_sanitize(f"{type(exc).__name__}: {exc}", secrets))


async def run(args: argparse.Namespace) -> tuple[Measurements, Measurements]:
    counts = _split_counts(args.requests, args.concurrency)
    gateway = Measurements("gateway")
    direct = Measurements("direct")
    secrets = [args.token, args.direct_password]

    endpoint = args.base_url.rstrip("/") + "/mcp"
    mcp_tasks = [
        asyncio.create_task(
            _mcp_worker(
                endpoint,
                args.token,
                args.datasource,
                count,
                args.timeout,
                gateway,
                secrets,
            )
        )
        for count in counts
    ]

    url = URL.create(
        "mysql+aiomysql",
        username=args.direct_user,
        password=args.direct_password,
        host=args.direct_host,
        port=args.direct_port,
        database=args.direct_database,
        query={"charset": "utf8mb4"},
    )
    engine = create_async_engine(
        url,
        pool_size=max(args.concurrency, 1),
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )
    direct_tasks = [
        asyncio.create_task(_direct_worker(engine, count, direct, secrets))
        for count in counts
    ]
    try:
        await asyncio.gather(*mcp_tasks)
        await asyncio.gather(*direct_tasks)
    finally:
        await engine.dispose()
    return gateway, direct


def _format(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def write_report(
    args: argparse.Namespace,
    gateway: Measurements,
    direct: Measurements,
    path: Path,
) -> tuple[bool, float | None]:
    gateway_stats = _summary(gateway.latencies)
    direct_stats = _summary(direct.latencies)
    overhead = None
    if gateway_stats["p95"] is not None and direct_stats["p95"] is not None:
        overhead = gateway_stats["p95"] - direct_stats["p95"]
    passed = (
        overhead is not None
        and not gateway.errors
        and not direct.errors
        and len(gateway.latencies) == args.requests
        and len(direct.latencies) == args.requests
        and overhead <= THRESHOLD_MS
    )

    lines = [
        "# BaseWard 性能报告（AC12）",
        "",
        f"- 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- 网关地址：{args.base_url.rstrip('/')}",
        f"- 数据源：{args.datasource}",
        f"- 直连数据库：{args.direct_host}:{args.direct_port}/{args.direct_database}",
        f"- 并发：{args.concurrency}；每轮总请求：{args.requests} 次 `SELECT 1`",
        f"- 判定阈值：网关 P95 − 直连 P95 ≤ {THRESHOLD_MS:.0f} ms",
        "",
        "## 结果",
        "",
        "| 指标 | 经网关 (ms) | 直连 (ms) |",
        "| --- | ---: | ---: |",
    ]
    for key, label in (
        ("avg", "平均值"),
        ("p50", "P50"),
        ("p95", "P95"),
        ("p99", "P99"),
        ("max", "最大值"),
    ):
        lines.append(f"| {label} | {_format(gateway_stats[key])} | {_format(direct_stats[key])} |")
    lines.append("")
    lines.append(f"- P95 额外开销：{_format(overhead)} ms")
    lines.append(f"- 有效样本：网关 {len(gateway.latencies)} 条，直连 {len(direct.latencies)} 条")
    lines.append(f"- 结论：{'通过' if passed else '不通过'}")

    if gateway.errors or direct.errors:
        lines.extend(["", "## 错误", ""])
        for label, measurements in (("网关", gateway), ("直连", direct)):
            for message in measurements.errors[:ERROR_SAMPLE_LIMIT]:
                lines.append(f"- {label}：{message}")
        total_errors = len(gateway.errors) + len(direct.errors)
        if total_errors > ERROR_SAMPLE_LIMIT * 2:
            lines.append(f"- 其余 {total_errors - ERROR_SAMPLE_LIMIT * 2} 条错误省略")

    lines.extend(
        [
            "",
            "## 说明",
            "",
            "- 每个 worker 独立建立 MCP 会话并预热一次，预热不计入统计。",
            "- 网关延迟包含认证、策略解析、审计写入与网络往返；直连为同一 MySQL 的单条查询。",
            "- 环境、网络与数据库负载会显著影响绝对值，AC12 只约束 P95 差值。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return passed, overhead


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BaseWard AC12 P95 性能对比")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BENCH_BASE_URL", "http://127.0.0.1:8080"),
        help="网关根地址，默认 http://127.0.0.1:8080",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BENCH_TOKEN"),
        help="MCP Bearer Token（也可用环境变量 BENCH_TOKEN）",
    )
    parser.add_argument("--datasource", required=True, help="网关内的数据源名称")
    parser.add_argument("--direct-host", required=True, help="直连 MySQL 主机")
    parser.add_argument("--direct-port", type=int, default=3306, help="直连 MySQL 端口")
    parser.add_argument("--direct-user", required=True, help="直连 MySQL 账号")
    parser.add_argument(
        "--direct-password",
        default=os.environ.get("BENCH_DIRECT_PASSWORD"),
        help="直连 MySQL 密码（也可用环境变量 BENCH_DIRECT_PASSWORD）",
    )
    parser.add_argument("--direct-database", required=True, help="直连 MySQL 数据库")
    parser.add_argument(
        "--requests", type=int, default=DEFAULT_REQUESTS, help="总请求数，默认 200"
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="并发数，默认 10"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="单次调用超时（秒）")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/perf-report.md"),
        help="报告输出路径，默认 docs/perf-report.md",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="额外输出 JSON 结果（供 bench_export.py 等脚本解析）",
    )
    args = parser.parse_args(argv)
    if args.requests <= 0 or args.concurrency <= 0:
        parser.error("--requests 与 --concurrency 必须为正整数")
    if not args.token:
        parser.error("缺少 --token 或环境变量 BENCH_TOKEN")
    if not args.direct_password:
        parser.error("缺少 --direct-password 或环境变量 BENCH_DIRECT_PASSWORD")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    gateway, direct = asyncio.run(run(args))
    passed, overhead = write_report(args, gateway, direct, args.output)

    gateway_stats = _summary(gateway.latencies)
    direct_stats = _summary(direct.latencies)
    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "gateway": gateway_stats,
                    "direct": direct_stats,
                    "overhead": overhead,
                    "samples": len(gateway.latencies) + len(direct.latencies),
                    "errors": gateway.errors + direct.errors,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    print(
        "网关 P95="
        f"{_format(gateway_stats['p95'])} ms，直连 P95={_format(direct_stats['p95'])} ms，"
        f"额外开销={_format(overhead)} ms，阈值={THRESHOLD_MS:.0f} ms，"
        f"结论={'通过' if passed else '不通过'}"
    )
    if gateway.errors or direct.errors:
        print(f"错误：网关 {len(gateway.errors)} 条，直连 {len(direct.errors)} 条")
    print(f"报告已写入 {args.output}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
