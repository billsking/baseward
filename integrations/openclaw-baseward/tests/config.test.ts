import { describe, expect, it } from "vitest";
import { resolveConfig } from "../src/config.ts";

describe("resolveConfig", () => {
  it("requires gatewayUrl and exchangeToken", () => {
    expect(() => resolveConfig(undefined)).toThrow(/required/);
    expect(() => resolveConfig({ gatewayUrl: "http://x" })).toThrow(/required/);
  });

  it("applies defaults and trims the trailing slash", () => {
    const config = resolveConfig({
      gatewayUrl: "http://127.0.0.1:8080/",
      exchangeToken: "gw_x",
    });
    expect(config.gatewayUrl).toBe("http://127.0.0.1:8080");
    expect(config.serverName).toBe("baseward");
    expect(config.mcpPath).toBe("/mcp/");
    expect(config.channel).toBe("dingtalk-connector");
    expect(config.provider).toBe("dingtalk");
    expect(config.refreshSkewSeconds).toBe(60);
    expect(config.ttlMinutes).toBeUndefined();
  });

  it("honours overrides and clamps a negative skew", () => {
    const config = resolveConfig({
      gatewayUrl: "http://x",
      exchangeToken: "gw_x",
      serverName: "db",
      channel: "custom",
      provider: "wecom",
      ttlMinutes: 5,
      refreshSkewSeconds: -10,
    });
    expect(config.serverName).toBe("db");
    expect(config.channel).toBe("custom");
    expect(config.provider).toBe("wecom");
    expect(config.ttlMinutes).toBe(5);
    expect(config.refreshSkewSeconds).toBe(0);
  });
});