import { describe, expect, it } from "vitest";
import { resolveConfig } from "../src/config.ts";
import { ExchangeClient, ExchangeError } from "../src/exchange.ts";

const BASE = Date.parse("2026-11-13T00:00:00.000Z");

function config(overrides: Record<string, unknown> = {}) {
  return resolveConfig({
    gatewayUrl: "http://gw",
    exchangeToken: "gw_exchange",
    ...overrides,
  });
}

function fakeFetch(handler: (calls: number) => { status: number; body: unknown }) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fn = async (url: unknown, init: unknown) => {
    calls.push({ url: String(url), init: init as RequestInit });
    const { status, body } = handler(calls.length);
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  };
  return { fn: fn as unknown as typeof fetch, calls };
}

function issued(credential: string, ttlSeconds = 900) {
  return {
    status: 200,
    body: {
      credential,
      expires_at: new Date(BASE + ttlSeconds * 1000).toISOString(),
      ttl_seconds: ttlSeconds,
    },
  };
}

describe("ExchangeClient", () => {
  it("exchanges once and caches per actor", async () => {
    const { fn, calls } = fakeFetch((n) => issued(`sc_${n}`));
    const client = new ExchangeClient(config(), fn, () => BASE);
    expect(await client.getCredential("u1")).toBe("sc_1");
    expect(await client.getCredential("u1")).toBe("sc_1");
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://gw/api/exchange");
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer gw_exchange");
    expect(headers["X-BaseWard-Actor-Provider"]).toBe("dingtalk");
    expect(headers["X-BaseWard-Actor-Id"]).toBe("u1");
  });

  it("keeps actors isolated", async () => {
    const { fn, calls } = fakeFetch((n) => issued(`sc_${n}`));
    const client = new ExchangeClient(config(), fn, () => BASE);
    expect(await client.getCredential("u1")).toBe("sc_1");
    expect(await client.getCredential("u2")).toBe("sc_2");
    expect(calls).toHaveLength(2);
  });

  it("re-exchanges inside the refresh skew window", async () => {
    const { fn, calls } = fakeFetch((n) => issued(`sc_${n}`));
    let now = BASE;
    const client = new ExchangeClient(config(), fn, () => now);
    await client.getCredential("u1");
    now = BASE + 840_000; // 900s TTL - 60s skew
    expect(await client.getCredential("u1")).toBe("sc_2");
    expect(calls).toHaveLength(2);
  });

  it("single-flights concurrent requests for the same actor", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { fn, calls } = fakeFetch((n) => issued(`sc_${n}`));
    const gated = (async (url: unknown, init: unknown) => {
      await gate;
      return fn(url as string, init as RequestInit);
    }) as unknown as typeof fetch;
    const client = new ExchangeClient(config(), gated, () => BASE);
    const first = client.getCredential("u1");
    const second = client.getCredential("u1");
    release!();
    expect(await first).toBe("sc_1");
    expect(await second).toBe("sc_1");
    expect(calls).toHaveLength(1);
  });

  it("surfaces the gateway error code and does not cache failures", async () => {
    const { fn, calls } = fakeFetch(() => ({
      status: 403,
      body: { error: "actor_required" },
    }));
    const client = new ExchangeClient(config(), fn, () => BASE);
    await expect(client.getCredential("u1")).rejects.toMatchObject({
      name: "ExchangeError",
      status: 403,
      code: "actor_required",
    });
    await expect(client.getCredential("u1")).rejects.toBeInstanceOf(ExchangeError);
    expect(calls).toHaveLength(2);
  });

  it("invalidate forces a new exchange", async () => {
    const { fn, calls } = fakeFetch((n) => issued(`sc_${n}`));
    const client = new ExchangeClient(config(), fn, () => BASE);
    await client.getCredential("u1");
    client.invalidate("u1");
    expect(await client.getCredential("u1")).toBe("sc_2");
    expect(calls).toHaveLength(2);
  });

  it("sends ttl_minutes only when configured", async () => {
    const { fn, calls } = fakeFetch((n) => issued(`sc_${n}`));
    await new ExchangeClient(config(), fn, () => BASE).getCredential("u1");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({});
    await new ExchangeClient(config({ ttlMinutes: 5 }), fn, () => BASE).getCredential("u2");
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ ttl_minutes: 5 });
  });

  it("rejects an unusable payload", async () => {
    const { fn } = fakeFetch(() => ({ status: 200, body: { expires_at: "nope" } }));
    const client = new ExchangeClient(config(), fn, () => BASE);
    await expect(client.getCredential("u1")).rejects.toBeInstanceOf(ExchangeError);
  });
});