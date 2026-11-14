/**
 * Minimal structural types for the OpenClaw plugin surface this bridge uses.
 * Verified against OpenClaw 2026.8.1 and 2026.11.3 (`registerMcpServerConnectionResolver`).
 */

export type McpResolveContext = {
  requesterSenderId: string;
  agentAccountId?: string;
  messageChannel?: string;
};

export type McpResolvedConnection = {
  url: string;
  headers?: Record<string, string>;
};

export type McpConnectionResolver = {
  serverName: string;
  resolve: (
    ctx: McpResolveContext
  ) => McpResolvedConnection | null | Promise<McpResolvedConnection | null>;
};

export type PluginLogger = {
  info?: (message: string) => void;
  warn?: (message: string) => void;
  error?: (message: string) => void;
};

export type PluginApi = {
  pluginConfig?: Record<string, unknown>;
  logger?: PluginLogger;
  registerMcpServerConnectionResolver: (resolver: McpConnectionResolver) => void;
};