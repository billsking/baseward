import { describe, expect, it } from "vitest";
import { actorHeaders } from "../src/headers.ts";

describe("actorHeaders", () => {
  it("encodes a Chinese nickname as unpadded base64url", () => {
    const headers = actorHeaders("dingtalk", "manager1234", "李四");
    expect(headers["X-BaseWard-Actor-Provider"]).toBe("dingtalk");
    expect(headers["X-BaseWard-Actor-Id"]).toBe("manager1234");
    expect(headers["X-BaseWard-Actor-Name"]).toBe("5p2O5Zub");
  });

  it("omits the name header when no nickname is given", () => {
    expect(actorHeaders("dingtalk", "u1")).toEqual({
      "X-BaseWard-Actor-Provider": "dingtalk",
      "X-BaseWard-Actor-Id": "u1",
    });
    expect(actorHeaders("dingtalk", "u1", null)["X-BaseWard-Actor-Name"]).toBeUndefined();
  });

  it("uses URL-safe alphabet for bytes that would otherwise need + or /", () => {
    const name = "subjects?query=1";
    const encoded = actorHeaders("dingtalk", "u1", name)["X-BaseWard-Actor-Name"];
    expect(encoded).not.toContain("+");
    expect(encoded).not.toContain("/");
    expect(encoded).not.toContain("=");
    expect(Buffer.from(encoded, "base64url").toString("utf8")).toBe(name);
  });
});