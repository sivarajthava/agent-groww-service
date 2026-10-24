// Server-side access to data/runs/<week>/. Read-only: the UI never writes, publishes or calls a model.
import { promises as fs } from "node:fs";
import path from "node:path";

import type { PublicQuote, RunDetail, RunFile, RunStatus, RunSummary, ThemesFile } from "./types";

const WEEK_RE = /^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$/;
const KNOWN_STATUSES: RunStatus[] = ["ok", "partial", "failed", "dry_run", "validated"];

/** ISO week ids only, so a route param can never point outside the runs directory. */
export function isValidWeek(week: string): boolean {
  return WEEK_RE.test(week);
}

export function dataDir(): string {
  const configured = process.env.PULSE_DATA_DIR?.trim();
  return path.resolve(process.cwd(), configured || path.join("..", "data"));
}

function runsDir(): string {
  return path.join(dataDir(), "runs");
}

async function readJson<T>(file: string): Promise<T | null> {
  try {
    return JSON.parse(await fs.readFile(file, "utf-8")) as T;
  } catch {
    return null; // missing or half-written file: the stage hasn't run (or is running)
  }
}

async function readText(file: string): Promise<string | null> {
  try {
    return await fs.readFile(file, "utf-8");
  } catch {
    return null;
  }
}

/** Port of pulse/render.py word_count: Markdown syntax ignored, "★1" and "19%" count as one word each. */
export function wordCount(markdown: string): number {
  const text = markdown.replace(/^\s*(#+|[-*]|\d+\.)\s+/gm, " ").replaceAll("*", " ");
  return text.match(/[\p{L}\p{M}\p{N}_’'-]+/gu)?.length ?? 0;
}

function toStatus(raw: string | undefined): RunStatus {
  return KNOWN_STATUSES.includes(raw as RunStatus) ? (raw as RunStatus) : "unknown";
}

function summarize(week: string, run: RunFile | null, themes: ThemesFile | null): RunSummary {
  const r = run ?? {};
  return {
    week,
    status: toStatus(r.status),
    window: r.window ?? themes?.window ?? null,
    reviews: r.counts?.reviews ?? themes?.total_reviews ?? null,
    avgRating: themes?.avg_rating ?? null,
    validated: r.validation?.ok === true,
    docUrl: r.doc_url ?? null,
    draftId: r.draft_id ?? null,
    flags: r.flags ?? themes?.flags ?? [],
  };
}

/** Every week that has a run folder, newest first. */
export async function listRuns(): Promise<RunSummary[]> {
  let entries: string[];
  try {
    entries = await fs.readdir(runsDir());
  } catch {
    return [];
  }
  const weeks = entries.filter(isValidWeek).sort().reverse();
  return Promise.all(
    weeks.map(async (week) => {
      const dir = path.join(runsDir(), week);
      const [run, themes] = await Promise.all([
        readJson<RunFile>(path.join(dir, "run.json")),
        readJson<ThemesFile>(path.join(dir, "themes.json")),
      ]);
      return summarize(week, run, themes);
    }),
  );
}

export async function getRun(week: string): Promise<RunDetail | null> {
  if (!isValidWeek(week)) return null;
  const dir = path.join(runsDir(), week);
  try {
    if (!(await fs.stat(dir)).isDirectory()) return null;
  } catch {
    return null;
  }
  const [run, themes, quotesFile, pulseMarkdown] = await Promise.all([
    readJson<RunFile>(path.join(dir, "run.json")),
    readJson<ThemesFile>(path.join(dir, "themes.json")),
    readJson<{ quotes: Array<PublicQuote & { review_id?: string }> }>(path.join(dir, "quotes.json")),
    readText(path.join(dir, "pulse.md")),
  ]);
  // Provenance stays on disk for the audit log; only text, rating and theme leave the server.
  const quotes = (quotesFile?.quotes ?? []).map(({ text, rating, theme_id }) => ({ text, rating, theme_id }));
  return {
    ...summarize(week, run, themes),
    run: run ?? {},
    themes,
    quotes,
    pulseMarkdown,
    wordCount: pulseMarkdown ? wordCount(pulseMarkdown) : null,
  };
}
