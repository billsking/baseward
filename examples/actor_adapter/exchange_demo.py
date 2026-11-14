#!/usr/bin/env python3
"""Reference adapter: exchange a short-lived credential, then query with it.

Example only. The actor is still self-asserted at exchange time — see README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from demo import _payload, actor_headers, mcp_client_factory


async def exchange(
    base_url: str, token: str, provider: str, actor_id: str, name: str | None, ttl: int | None
) -> dict:
    # /api/exchange lives at the gateway root, while base_url points at the MCP mount.
    parts = urlsplit(base_url)
    endpoint = f"{parts.scheme}://{parts.netloc}/api/exchange"
    body = {} if ttl is None else {"ttl_minutes": ttl}
    # trust_env=False: some NO_PROXY values (e.g. a bare [::1]) break httpx's env proxy parsing.
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}", **actor_headers(provider, actor_id, name)},
            json=body,
        )
    if response.status_code != 200:
        raise RuntimeError(f"exchange failed: {response.status_code} {response.text}")
    return response.json()


async def run(args) -> int:
    token = args.exchange_token or os.environ.get("GATEWAY_EXCHANGE_TOKEN")
    if not token:
        print("error: --exchange-token or GATEWAY_EXCHANGE_TOKEN is required", file=sys.stderr)
        return 2
    issued = await exchange(
        args.base_url, token, args.provider, args.actor_id, args.actor_name, args.ttl_minutes
    )
    print(json.dumps({"issued": {k: v for k, v in issued.items() if k != "credential"}},
                     ensure_ascii=False, indent=2))
    # The short credential carries the actor; identity headers are no longer required.
    headers = {"Authorization": f"Bearer {issued['credential']}"}
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
        description="BaseWard short-credential exchange reference adapter (example)"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080/mcp/")
    parser.add_argument("--exchange-token", default=None,
                        help="gw_ exchange-kind token; defaults to GATEWAY_EXCHANGE_TOKEN")
    parser.add_argument("--provider", default="dingtalk")
    parser.add_argument("--actor-id", required=True, help="stable end-user id, e.g. DingTalk senderStaffId")
    parser.add_argument("--actor-name", default=None, help="display nickname (any language)")
    parser.add_argument("--ttl-minutes", type=int, default=None, help="optional, capped by the gateway")
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