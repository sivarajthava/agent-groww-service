import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BackendUnavailable, getRun, listRuns, sourceLabel } from "@/lib/runs";

const BASE = "https://pulse-api.example.up.railway.app";

function respond(status: number, body?: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("backend API source", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubEnv("PULSE_API_URL", `${BASE}/`);
    vi.stubEnv("PULSE_API_TOKEN", "test-token");
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("lists runs with the bearer token and no caching", async () => {
    fetchMock.mockResolvedValue(respond(200, { runs: [
      { week: "2026-W43", run: { status: "ok", counts: { reviews: 10 } }, themes: { avg_rating: 3.2 } },
      { week: "../evil", run: null, themes: null },
    ], active: [] }));
    const runs = await listRuns();
    expect(runs.map((r) => r.week)).toEqual(["2026-W43"]);
    expect(runs[0]).toMatchObject({ status: "ok", reviews: 10, avgRating: 3.2 });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE}/runs`);
    expect(init.headers.Authorization).toBe("Bearer test-token");
    expect(init.cache).toBe("no-store");
  });

  it("loads a week and strips quote provenance even if the API sent it", async () => {
    fetchMock.mockResolvedValue(respond(200, {
      week: "2026-W43", run: { status: "ok" }, themes: null, pulse_md: "# Pulse\nhello world",
      quotes: [{ text: "Slow", rating: 1, theme_id: "t1", review_id: "leaked" }],
    }));
    const run = await getRun("2026-W43");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE}/runs/2026-W43`);
    expect(run?.quotes).toEqual([{ text: "Slow", rating: 1, theme_id: "t1" }]);
    expect(run?.wordCount).toBe(3);
    expect(JSON.stringify(run)).not.toContain("leaked");
  });

  it("maps 404 to not found and never calls the API for a malformed week", async () => {
    fetchMock.mockResolvedValue(respond(404, { detail: "no such week" }));
    expect(await getRun("2026-W01")).toBeNull();
    expect(await getRun("../../etc")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("raises BackendUnavailable on auth errors, 5xx and network failures", async () => {
    fetchMock.mockResolvedValueOnce(respond(401));
    await expect(listRuns()).rejects.toBeInstanceOf(BackendUnavailable);
    fetchMock.mockResolvedValueOnce(respond(502));
    await expect(listRuns()).rejects.toBeInstanceOf(BackendUnavailable);
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    await expect(getRun("2026-W43")).rejects.toBeInstanceOf(BackendUnavailable);
  });

  it("labels the source by URL, never by token", () => {
    expect(sourceLabel()).toBe(BASE);
  });
});
