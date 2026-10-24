import { promises as fs } from "node:fs";
import path from "node:path";
import { beforeAll, describe, expect, it } from "vitest";

import { getRun, isValidWeek, listRuns, wordCount } from "@/lib/runs";

beforeAll(() => {
  process.env.PULSE_DATA_DIR = path.join(__dirname, "fixtures", "data");
});

describe("isValidWeek", () => {
  it.each(["2026-W01", "2026-W43", "2026-W53"])("accepts %s", (w) => expect(isValidWeek(w)).toBe(true));
  it.each(["2026-W00", "2026-W54", "2026-43", "../etc", "2026-W43/../../x", "", "2026-w43"])(
    "rejects %s",
    (w) => expect(isValidWeek(w)).toBe(false),
  );
});

describe("listRuns", () => {
  it("lists week folders newest first and skips other folders", async () => {
    const runs = await listRuns();
    expect(runs.map((r) => r.week)).toEqual(["2026-W43", "2026-W42"]);
  });

  it("summarizes status, counts and publish results", async () => {
    const [w43, w42] = await listRuns();
    expect(w43).toMatchObject({ status: "ok", reviews: 2144, avgRating: 3.04, validated: true,
      docUrl: "https://docs.google.com/document/d/FIXTURE_DOC/edit", draftId: "r-fixture-draft" });
    expect(w42).toMatchObject({ status: "failed", validated: false, avgRating: null, docUrl: null,
      flags: ["low_volume"] });
  });

  it("returns an empty list when there is no runs directory", async () => {
    const prev = process.env.PULSE_DATA_DIR;
    process.env.PULSE_DATA_DIR = path.join(__dirname, "fixtures", "missing");
    try {
      expect(await listRuns()).toEqual([]);
    } finally {
      process.env.PULSE_DATA_DIR = prev;
    }
  });
});

describe("getRun", () => {
  it("loads every artifact for a complete week", async () => {
    const run = await getRun("2026-W43");
    expect(run?.themes?.themes).toHaveLength(5);
    expect(run?.themes?.top).toEqual(["t1", "t2", "t3"]);
    expect(run?.quotes).toHaveLength(3);
    expect(run?.pulseMarkdown).toContain("Weekly Review Pulse");
    expect(run?.run.mcp_calls).toHaveLength(4);
  });

  it("never exposes quote provenance", async () => {
    const run = await getRun("2026-W43");
    for (const q of run!.quotes) expect(Object.keys(q).sort()).toEqual(["rating", "text", "theme_id"]);
    expect(JSON.stringify(run)).not.toContain("secret");
  });

  it("tolerates a week that stopped before themes and pulse", async () => {
    const run = await getRun("2026-W42");
    expect(run).toMatchObject({ themes: null, quotes: [], pulseMarkdown: null, wordCount: null, status: "failed" });
    expect(run?.run.validation?.failures).toEqual(["word_count: 274 > 250"]);
  });

  it("returns null for unknown or malformed weeks", async () => {
    expect(await getRun("2026-W01")).toBeNull();
    expect(await getRun("../../src")).toBeNull();
    expect(await getRun("not-a-week")).toBeNull();
  });
});

describe("wordCount", () => {
  it("matches the Python rule: markers ignored, ★1 and 19% are single words", () => {
    expect(wordCount("## Top Themes\n\n1. Support — 19% — slow\n- \"Bad app\" (★1)")).toBe(8);
  });

  it("keeps the golden pulse within the 250-word budget", async () => {
    const md = await fs.readFile(path.join(__dirname, "fixtures/data/runs/2026-W43/pulse.md"), "utf-8");
    expect(wordCount(md)).toBeLessThanOrEqual(250);
  });
});
