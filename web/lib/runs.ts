// Server-side access to the pipeline's runs. Read-only: the UI never writes, publishes or calls a model.
//
// Two sources with the same shape:
// - the backend API (`PULSE_API_URL` + `PULSE_API_TOKEN`), used when deployed (Vercel → Railway, doc/deployment.md)
// - the local data/runs/<week>/ folders (`PULSE_DATA_DIR`, default ../data), for local dev and tests
import { promises as fs } from "node:fs";
import path from "node:path";

import type { PublicQuote, RunDetail, RunFile, RunStatus, RunSummary, ThemesFile } from "./types";

const WEEK_RE = /^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$/;
const KNOWN_STATUSES: RunStatus[] = ["ok", "partial", "failed", "dry_run", "validated"];

/** Raw artifacts of one week, as both sources return them. */
interface RawRun {
  week: string;
  run: RunFile | null;
  themes: ThemesFile | null;
}

interface RawDetail extends RawRun {
  quotes: Array<PublicQuote & { review_id?: string }>;
  pulse_md: string | null;
}

interface RunSource {
  list(): Promise<RawRun[]>;
  detail(week: string): Promise<RawDetail | null>;
}

export class BackendUnavailable extends Error {}

/** ISO week ids only, so a route param can never point outside the runs directory or API. */
export function isValidWeek(week: string): boolean {
  return WEEK_RE.test(week);
}

export function dataDir(): string {
  const configured = process.env.PULSE_DATA_DIR?.trim();
  return path.resolve(process.cwd(), configured || path.join("..", "data"));
}

function apiUrl(): string | null {
  return process.env.PULSE_API_URL?.trim().replace(/\/+$/, "") || null;
}

/** Where runs are read from, for the empty state. Never includes the token. */
export function sourceLabel(): string {
  return apiUrl() ?? path.join(dataDir(), "runs");
}

// ---------- filesystem source ----------

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

const fileSource: RunSource = {
  async list() {
    const runsDir = path.join(dataDir(), "runs");
    let entries: string[];
    try {
      entries = await fs.readdir(runsDir);
    } catch {
      return [];
    }
    const weeks = entries.filter(isValidWeek).sort().reverse();
    return Promise.all(
      weeks.map(async (week) => ({
        week,
        run: await readJson<RunFile>(path.join(runsDir, week, "run.json")),
        themes: await readJson<ThemesFile>(path.join(runsDir, week, "themes.json")),
      })),
    );
  },

  async detail(week) {
    const dir = path.join(dataDir(), "runs", week);
    try {
      if (!(await fs.stat(dir)).isDirectory()) return null;
    } catch {
      return null;
    }
    const [run, themes, quotesFile, pulse_md] = await Promise.all([
      readJson<RunFile>(path.join(dir, "run.json")),
      readJson<ThemesFile>(path.join(dir, "themes.json")),
      readJson<{ quotes: RawDetail["quotes"] }>(path.join(dir, "quotes.json")),
      readText(path.join(dir, "pulse.md")),
    ]);
    return { week, run, themes, quotes: quotesFile?.quotes ?? [], pulse_md };
  },
};

// ---------- backend API source ----------

async function apiGet<T>(base: string, route: string): Promise<T | null> {
  let res: Response;
  try {
    res = await fetch(`${base}${route}`, {
      headers: { Authorization: `Bearer ${process.env.PULSE_API_TOKEN ?? ""}` },
      cache: "no-store", // new runs show up without a redeploy
      signal: AbortSignal.timeout(10_000),
    });
  } catch {
    throw new BackendUnavailable("The pulse backend did not respond.");
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new BackendUnavailable(`The pulse backend answered ${res.status}.`);
  return (await res.json()) as T;
}

function apiSource(base: string): RunSource {
  return {
    async list() {
      return (await apiGet<{ runs: RawRun[] }>(base, "/runs"))?.runs.filter((r) => isValidWeek(r.week)) ?? [];
    },
    detail: (week) => apiGet<RawDetail>(base, `/runs/${week}`),
  };
}

function source(): RunSource {
  const base = apiUrl();
  return base ? apiSource(base) : fileSource;
}

// ---------- shaping for the pages ----------

/** Port of pulse/render.py word_count: Markdown syntax ignored, "★1" and "19%" count as one word each. */
export function wordCount(markdown: string): number {
  const text = markdown.replace(/^\s*(#+|[-*]|\d+\.)\s+/gm, " ").replaceAll("*", " ");
  return text.match(/[\p{L}\p{M}\p{N}_’'-]+/gu)?.length ?? 0;
}

function toStatus(raw: string | undefined): RunStatus {
  return KNOWN_STATUSES.includes(raw as RunStatus) ? (raw as RunStatus) : "unknown";
}

function summarize({ week, run, themes }: RawRun): RunSummary {
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

/** Every week that has a run, newest first. */
export async function listRuns(): Promise<RunSummary[]> {
  return (await source().list()).map(summarize);
}

export async function getRun(week: string): Promise<RunDetail | null> {
  if (!isValidWeek(week)) return null;
  const raw = await source().detail(week);
  if (!raw) return null;
  return {
    ...summarize(raw),
    run: raw.run ?? {},
    themes: raw.themes,
    // Provenance stays on disk for the audit log; only text, rating and theme reach the page.
    quotes: raw.quotes.map(({ text, rating, theme_id }) => ({ text, rating, theme_id })),
    pulseMarkdown: raw.pulse_md,
    wordCount: raw.pulse_md ? wordCount(raw.pulse_md) : null,
  };
}
