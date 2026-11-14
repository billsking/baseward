export type BridgeConfig = {
  gatewayUrl: string;
  exchangeToken: string;
  serverName: string;
  mcpPath: string;
  channel: string;
  provider: string;
  ttlMinutes?: number;
  refreshSkewSeconds: number;
};

function str(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

export function resolveConfig(raw: Record<string, unknown> | undefined): BridgeConfig {
  const config = raw ?? {};
  const gatewayUrl = str(config.gatewayUrl, "");
  const exchangeToken = str(config.exchangeToken, "");
  if (!gatewayUrl || !exchangeToken) {
    throw new Error("baseward-bridge: gatewayUrl and exchangeToken are required");
  }
  const ttlMinutes =
    typeof config.ttlMinutes === "number" && Number.isFinite(config.ttlMinutes)
      ? config.ttlMinutes
      : undefined;
  const skew =
    typeof config.refreshSkewSeconds === "number" && Number.isFinite(config.refreshSkewSeconds)
      ? Math.max(0, config.refreshSkewSeconds)
      : 60;
  return {
    gatewayUrl: gatewayUrl.replace(/\/+$/, ""),
    exchangeToken,
    serverName: str(config.serverName, "baseward"),
    mcpPath: str(config.mcpPath, "/mcp/"),
    channel: str(config.channel, "dingtalk-connector"),
    provider: str(config.provider, "dingtalk"),
    ttlMinutes,
    refreshSkewSeconds: skew,
  };
}