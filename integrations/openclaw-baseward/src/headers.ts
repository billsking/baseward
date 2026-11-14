/** Build the X-BaseWard-Actor-* contract headers; the nickname is base64url UTF-8. */
export function actorHeaders(
  provider: string,
  actorId: string,
  name?: string | null
): Record<string, string> {
  const headers: Record<string, string> = {
    "X-BaseWard-Actor-Provider": provider,
    "X-BaseWard-Actor-Id": actorId,
  };
  if (name) {
    headers["X-BaseWard-Actor-Name"] = Buffer.from(name, "utf8")
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }
  return headers;
}