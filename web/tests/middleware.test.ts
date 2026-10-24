import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { middleware } from "@/middleware";

const req = (auth?: string) =>
  new NextRequest("https://pulse.example.com/runs/2026-W43", { headers: auth ? { authorization: auth } : {} });
const basic = (s: string) => `Basic ${btoa(s)}`;

describe("basic auth middleware", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("lets everything through when WEB_BASIC_AUTH is unset", () => {
    vi.stubEnv("WEB_BASIC_AUTH", "");
    expect(middleware(req()).status).toBe(200);
  });

  it("requires the configured credentials", () => {
    vi.stubEnv("WEB_BASIC_AUTH", "team:s3cret");
    const denied = middleware(req());
    expect(denied.status).toBe(401);
    expect(denied.headers.get("WWW-Authenticate")).toContain("Basic");
    expect(middleware(req(basic("team:wrong"))).status).toBe(401);
    expect(middleware(req(basic("team:s3cret!"))).status).toBe(401);
    expect(middleware(req("Basic !!!not-base64")).status).toBe(401);
    expect(middleware(req(basic("team:s3cret"))).status).toBe(200);
  });
});
