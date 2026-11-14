#!/usr/bin/env python3
"""Reference adapter: call BaseWard MCP tools while asserting the real end user.

Example only. P1 actor identity is self-asserted — see README for the trust boundary.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def mcp_client_factory(headers=None, timeout=None, auth=None) -> httpx.AsyncClient:
    """Ignore env proxies: a bare [::1] in NO_PROXY breaks httpx's env proxy parsing."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30, read=300),
        auth=auth,
        trust_env=False,
    )


def actor_headers(provider: str, actor_id: str, name: str | None) -> dict[str, str]:
    """Build the X-BaseWard-Actor-* contract headers; the nickname is base64url UTF-8."""
    headers = {
        "X-BaseWard-Actor-Provider": provider,
        "X-BaseWard-Actor-Id": actor_id,
    }
    if name:
        headers["X-BaseWard-Actor-Name"] = (
            base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")
        )
    return headers


def _payload(result) -> dict:
    for item in result.content:
        if getattr(item, "type", None) == "text":
            try:
                return json.loads(item.text)
            except json.JSONDecodeError:
                return {"raw": item.text}
    return {}


async def run(args) -> int:
    token = args.token or os.environ.get("GATEWAY_TOKEN")
    if not token:
        print("error: --token or GATEWAY_TOKEN is required", file=sys.stderr)
        return 2
    headers = {
        "Authorization": f"Bearer {token}",
        **actor_headers(args.provider, args.actor_id, args.actor_name),
    }
    async with streamablehttp_client(
        args.base_url, headers=headers, timeout=30, httpx_client_factory=mcp_client_factory
    ) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            print(json.dumps(_payload(await session.call_tool("list_datasources", {})),
                             ensure_ascii=False, indent=2))
            if args.datasource:
                result = await session.call_tool(
                    "execute_sql", {"datasource": args.datasource, "sql": args.sql}
                )
                print(json.dumps(_payload(result), ensure_ascii=False, indent=2))
                if getattr(result, "isError", False):
                    return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="BaseWard MCP actor-identity reference adapter (example)"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/mcp/")
    parser.add_argument("--token", default=None, help="gw_ token; defaults to GATEWAY_TOKEN")
    parser.add_argument("--provider", default="dingtalk")
    parser.add_argument("--actor-id", required=True, help="stable end-user id, e.g. DingTalk senderStaffId")
    parser.add_argument("--actor-name", default=None, help="display nickname (any language)")
    parser.add_argument("--datasource", default=None, help="also run execute_sql against this datasource")
    parser.add_argument("--sql", default="SELECT 1")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001 - the example surfaces any failure readably
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())