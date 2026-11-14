import { resolveConfig } from "./config.ts";
import { ExchangeClient } from "./exchange.ts";
import type { McpResolveContext, McpResolvedConnection, PluginApi } from "./types.ts";

export { ExchangeClient, ExchangeError } from "./exchange.ts";
export { actorHeaders } from "./headers.ts";
export { resolveConfig } from "./config.ts";

export default function register(api: PluginApi): void {
  const config = resolveConfig(api.pluginConfig);
  const client = new ExchangeClient(config);
  const url = `${config.gatewayUrl}${config.mcpPath}`;

  api.registerMcpServerConnectionResolver({
    serverName: config.serverName,
    resolve: async (ctx: McpResolveContext): Promise<McpResolvedConnection | null> => {
      // Fail closed: only the configured channel, and only a host-trusted sender id.
      if (ctx.messageChannel !== config.channel) return null;
      const actorId = (ctx.requesterSenderId ?? "").trim();
      if (!actorId) return null;
      try {
        const credential = await client.getCredential(actorId);
        // The resolver's headers replace the static ones, so Authorization must be included.
        // The short credential already carries the actor; no identity headers are needed.
        return { url, headers: { Authorization: `Bearer ${credential}` } };
      } catch (error) {
        api.logger?.warn?.(
          `baseward-bridge: exchange failed for actor ${actorId}: ${String(error)}`
        );
        return null;
      }
    },
  });
}