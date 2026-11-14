"""End-to-end smoke test for export handoff against a deployed gateway, printing pass/fail per check.

Usage (the token is read from the environment only, never the command line):

    set /x GATEWAY_TEST_TOKEN=gw_xxx          (Windows cmd)
    $env:GATEWAY_TEST_TOKEN = "gw_xxx"        (PowerShell)

    python scripts/smoke_export_handoff.py --datasource demo \
        --sql "SELECT id, phone FROM users ORDER BY id"

Checks: health endpoint, tool list, execute_sql truncation handoff, explicit issue plus a real
connection with the issued credential fetching everything, and rejection of write statements and
an empty reason. Any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

WRITE_SQL = "UPDATE gateway_smoke_probe SET id = id WHERE 1 = 0"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _payload(result) -> dict:
    for item in result.content:
        if getattr(item, "type", None) == "text":
            text = item.text.strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                start, end = text.find("{"), text.rfind("}")
                if start != -1 and end > start:
                    return json.loads(text[start : end + 1])
            return {"_raw": text}
    raise AssertionError(f"工具没有返回文本内容：{result}")


class Smoke:
    def __init__(self, args: argparse.Namespace, token: str) -> None:
        self.args = args
        self.token = token
        self.results: list[Check] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append(Check(name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{f' — {detail}' if detail else ''}")
        return ok

    async def run(self) -> None:
        await self._healthz()
        async with streamablehttp_client(
            f"{self.args.base_url}/mcp",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
            sse_read_timeout=30,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                await self._tools(session)
                export = await self._explicit_issue(session)
                if export is not None:
                    await self._stream_export(export)
                await self._truncation_handoff(session)
                await self._rejections(session)

    async def _healthz(self) -> None:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.args.base_url}/healthz")
        body = response.json()
        export = body.get("mysql_export", {})
        self.record(
            "健康检查",
            response.status_code == 200,
            f"status={body.get('status')} export={export}",
        )
        self.record(
            "导出通道在监听",
            bool(export.get("enabled")) and bool(export.get("listening")),
            f"并发上限={export.get('max_concurrent')}",
        )

    async def _tools(self, session: ClientSession) -> None:
        tools = [tool.name for tool in (await session.list_tools()).tools]
        self.record(
            "工具清单含 issue_export_credential",
            "issue_export_credential" in tools,
            "、".join(tools),
        )

    async def _call(self, session: ClientSession, name: str, arguments: dict) -> tuple[bool, dict]:
        result = await session.call_tool(name, arguments)
        return bool(getattr(result, "isError", False)), _payload(result)

    async def _explicit_issue(self, session: ClientSession) -> dict | None:
        is_error, payload = await self._call(
            session,
            "issue_export_credential",
            {
                "datasource": self.args.datasource,
                "sql": self.args.sql,
                "reason": self.args.reason,
            },
        )
        if not self.record(
            "显式签发返回凭证",
            not is_error and bool(payload.get("user")),
            payload.get("error", {}).get("message", "") if is_error else f"账号={payload.get('user')}",
        ):
            return None
        self.record(
            "载荷可运行",
            all(payload.get(key) for key in ("host", "port", "password", "sample_code"))
            and "SSCursor" in payload["sample_code"],
            f"{payload.get('host')}:{payload.get('port')} db={payload.get('database')} "
            f"绑定IP={payload.get('source_ip')} 过期={payload.get('expires_at')}",
        )
        self.record(
            "密码不进样本代码",
            payload["password"] not in payload["sample_code"],
        )
        return payload

    async def _stream_export(self, payload: dict) -> None:
        import pymysql

        host = self.args.export_host or payload["host"]
        port = self.args.export_port or payload["port"]

        def fetch() -> tuple[list[str], int]:
            connection = pymysql.connect(
                host=host,
                port=int(port),
                user=payload["user"],
                password=payload["password"],
                database=payload.get("database") or None,
                charset="utf8mb4",
                ssl_disabled=True,
                connect_timeout=10,
                cursorclass=pymysql.cursors.SSCursor,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(payload["sql"])
                    columns = [item[0] for item in cursor.description]
                    total = 0
                    while True:
                        batch = cursor.fetchmany(5000)
                        if not batch:
                            break
                        total += len(batch)
                    return columns, total
            finally:
                connection.close()

        try:
            columns, total = await asyncio.to_thread(fetch)
        except Exception as exc:  # noqa: BLE001 - the smoke script must report connection-level errors truthfully
            self.record(
                "用凭证连导出通道取全量",
                False,
                f"{type(exc).__name__}: {exc}",
            )
            return
        self.record(
            "用凭证连导出通道取全量",
            total > 0,
            f"{total} 行 / {len(columns)} 列（导出地址 {host}:{port}，"
            f"来源 IP 须在白名单内且等于凭证绑定的 {payload.get('source_ip')}）",
        )

    async def _truncation_handoff(self, session: ClientSession) -> None:
        is_error, payload = await self._call(
            session, "execute_sql", {"datasource": self.args.datasource, "sql": self.args.sql}
        )
        if is_error:
            self.record("execute_sql 截断交接", False, payload.get("error", {}).get("message", ""))
            return
        truncated = bool(payload.get("truncated"))
        if not truncated:
            self.record(
                "execute_sql 截断交接",
                True,
                f"结果 {payload.get('row_count')} 行未超上限，跳过（换更大的 SQL 或调低 MCP_RESULT_MAX_ROWS 才会触发）",
            )
            return
        export = payload.get("export")
        self.record(
            "execute_sql 截断交接",
            export is not None and bool(export.get("user")),
            f"reason={payload.get('truncated_reason')} hint={payload.get('hint')}",
        )

    async def _rejections(self, session: ClientSession) -> None:
        is_error, payload = await self._call(
            session,
            "issue_export_credential",
            {"datasource": self.args.datasource, "sql": WRITE_SQL, "reason": "冒烟：写语句应被拒"},
        )
        self.record(
            "写语句签发被拒",
            is_error and payload.get("error", {}).get("code") in {"WRITE_ON_READONLY", "NO_WHERE"},
            f"code={payload.get('error', {}).get('code')} msg={payload.get('error', {}).get('message')}",
        )

        is_error, payload = await self._call(
            session,
            "issue_export_credential",
            {"datasource": self.args.datasource, "sql": self.args.sql, "reason": "  "},
        )
        self.record(
            "空 reason 被拒",
            is_error and payload.get("error", {}).get("code") == "INVALID_ARGUMENT",
            f"code={payload.get('error', {}).get('code')}",
        )

    def finish(self) -> int:
        failed = [item for item in self.results if not item.ok]
        print(f"\n合计 {len(self.results)} 项，失败 {len(failed)} 项")
        for item in failed:
            print(f"  - {item.name}：{item.detail}")
        return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出交接冒烟测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--datasource", required=True, help="数据源名称或唯一业务描述片段")
    parser.add_argument("--sql", required=True, help="本次要导出的只读语句")
    parser.add_argument("--reason", default="冒烟测试：验证导出交接")
    parser.add_argument("--export-host", default="", help="覆盖响应里的导出地址（本机测试常用 127.0.0.1）")
    parser.add_argument("--export-port", type=int, default=0, help="覆盖响应里的导出端口")
    parser.add_argument(
        "--token-file",
        default="",
        help="从文件读取 Token 明文（比命令行和环境变量都更适合代跑，文件请用后删除）",
    )
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    token = os.environ.get("GATEWAY_TEST_TOKEN", "").strip()
    if not token and args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        print("先设置环境变量 GATEWAY_TEST_TOKEN，或用 --token-file 指向 Token 明文文件（管理端创建的 Token）。")
        return 2
    smoke = Smoke(args, token)
    await smoke.run()
    return smoke.finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
