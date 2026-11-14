import { afterEach, describe, expect, it, vi } from "vitest";
import register from "../src/index.ts";
import type { McpConnectionResolver, PluginApi } from "../src/types.ts";

const BASE = Date.parse("2026-11-13T00:00:00.000Z");

function harness(pluginConfig: Record<string, unknown>, status = 200) {
  const resolvers: McpConnectionResolver[] = [];
  const warnings: string[] = [];
  const api: PluginApi = {
    pluginConfig,
    logger: { warn: (message) => warnings.push(message) },
    registerMcpServerConnectionResolver: (resolver) => resolvers.push(resolver),
  };
  const fetchMock = vi.fn(async (_url: unknown, _init: unknown) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({
      credential: "sc_issued",
      expires_at: new Date(BASE + 900_000).toISOString(),
      ttl_seconds: 900,
    }),
    text: async () => JSON.stringify({ error: "actor_required" }),
  }));
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(Date, "now").mockReturnValue(BASE);
  register(api);
  return { resolver: resolvers[0], warnings, fetchMock };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("register", () => {
  it("requires config", () => {
    expect(() => register({ registerMcpServerConnectionResolver: () => {} } as PluginApi)).toThrow(
      /required/
    );
  });

  it("registers the resolver under the configured server name", () => {
    const { resolver } = harness({
      gatewayUrl: "http://gw",
      exchangeToken: "gw_x",
      serverName: "db",
    });
    expect(resolver.serverName).toBe("db");
  });

  it("resolves a connection bound to the trusted requester", async () => {
    const { resolver, fetchMock } = harness({ gatewayUrl: "http://gw", exchangeToken: "gw_x" });
    const connection = await resolver.resolve({
      requesterSenderId: "manager1234",
      messageChannel: "dingtalk-connector",
    });
    expect(connection).toEqual({
      url: "http://gw/mcp/",
      headers: { Authorization: "Bearer sc_issued" },
    });
    const headers = (fetchMock.mock.calls[0][1] as RequestInit).headers as Record<string, string>;
    expect(headers["X-BaseWard-Actor-Id"]).toBe("manager1234");
  });

  it("fails closed for another channel", async () => {
    const { resolver, fetchMock } = harness({ gatewayUrl: "http://gw", exchangeToken: "gw_x" });
    expect(
      await resolver.resolve({ requesterSenderId: "u1", messageChannel: "telegram" })
    ).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fails closed without a trusted sender id", async () => {
    const { resolver, fetchMock } = harness({ gatewayUrl: "http://gw", exchangeToken: "gw_x" });
    expect(
      await resolver.resolve({ requesterSenderId: "  ", messageChannel: "dingtalk-connector" })
    ).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("omits the server and warns when exchange fails", async () => {
    const { resolver, warnings } = harness(
      { gatewayUrl: "http://gw", exchangeToken: "gw_x" },
      403
    );
    expect(
      await resolver.resolve({
        requesterSenderId: "u1",
        messageChannel: "dingtalk-connector",
      })
    ).toBeNull();
    expect(warnings.join()).toMatch(/exchange failed/);
  });
});