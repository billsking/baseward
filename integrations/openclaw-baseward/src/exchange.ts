import type { BridgeConfig } from "./config.ts";
import { actorHeaders } from "./headers.ts";

export type ExchangeResponse = {
  credential: string;
  expires_at: string;
  ttl_seconds: number;
};

type FetchLike = typeof fetch;

type CacheEntry = { credential: string; expiresAtMs: number };

export class ExchangeError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string
  ) {
    super(message);
    this.name = "ExchangeError";
  }
}

/**
 * Per-actor short-credential cache with single-flight exchange.
 * The actor is asserted by the host-trusted requester id, never by the model.
 */
export class ExchangeClient {
  private readonly cache = new Map<string, CacheEntry>();
  private readonly inflight = new Map<string, Promise<string>>();

  constructor(
    private readonly config: BridgeConfig,
    private readonly fetchImpl: FetchLike = fetch,
    private readonly now: () => number = Date.now
  ) {}

  async getCredential(actorId: string, actorName?: string | null): Promise<string> {
    const cached = this.cache.get(actorId);
    const skewMs = this.config.refreshSkewSeconds * 1000;
    if (cached && cached.expiresAtMs - skewMs > this.now()) {
      return cached.credential;
    }
    const pending = this.inflight.get(actorId);
    if (pending) return pending;
    const task = this.exchange(actorId, actorName).finally(() => {
      this.inflight.delete(actorId);
    });
    this.inflight.set(actorId, task);
    return task;
  }

  invalidate(actorId: string): void {
    this.cache.delete(actorId);
  }

  private async exchange(actorId: string, actorName?: string | null): Promise<string> {
    const body =
      this.config.ttlMinutes === undefined ? {} : { ttl_minutes: this.config.ttlMinutes };
    const response = await this.fetchImpl(`${this.config.gatewayUrl}/api/exchange`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.config.exchangeToken}`,
        "Content-Type": "application/json",
        ...actorHeaders(this.config.provider, actorId, actorName),
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      let code: string | undefined;
      try {
        code = (JSON.parse(text) as { error?: string }).error;
      } catch {
        code = undefined;
      }
      throw new ExchangeError(
        `BaseWard exchange failed: HTTP ${response.status}${code ? ` ${code}` : ""}`,
        response.status,
        code
      );
    }
    const payload = (await response.json()) as ExchangeResponse;
    const expiresAtMs = Date.parse(payload.expires_at);
    if (!payload.credential || !Number.isFinite(expiresAtMs)) {
      throw new ExchangeError("BaseWard exchange returned an unusable credential", response.status);
    }
    this.cache.set(actorId, { credential: payload.credential, expiresAtMs });
    return payload.credential;
  }
}