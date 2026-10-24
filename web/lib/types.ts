// Shapes of the files the Python pipeline writes to data/runs/<week>/ (architecture.md §5).
// Only fields the UI shows are typed; everything is optional because a run can stop at any stage.

export type RunStatus = "ok" | "partial" | "failed" | "dry_run" | "validated" | "unknown";

export interface ThemeStats {
  id: string;
  name: string;
  description: string;
  count: number;
  share: number;
  avg_rating: number;
  neg_share: number;
  wow_delta: number | null;
}

export interface ThemesFile {
  week: string;
  window: { start: string; end: string };
  total_reviews: number;
  avg_rating: number;
  flags: string[];
  k: number;
  other_count: number;
  top: string[];
  themes: ThemeStats[];
}

/** A quote as shown in the UI: provenance (review_id) is dropped at the data layer. */
export interface PublicQuote {
  text: string;
  rating: number;
  theme_id: string;
}

export interface McpCall {
  server: string;
  action: string;
  tool: string | null;
  arg_keys: string[];
  ms: number;
  ok: boolean;
  error: string | null;
}

export interface AgentStep {
  step: number;
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  ms: number;
}

export interface RunFile {
  week?: string;
  window?: { start: string; end: string };
  status?: string;
  error?: string | null;
  flags?: string[];
  counts?: { reviews?: number; themes?: number; quotes?: number };
  model?: string | null;
  reasoning_effort?: string;
  prompts?: Record<string, string>;
  validation?: { ok: boolean; failures: string[] };
  timings?: Record<string, number>;
  stages?: Record<string, Record<string, number>>;
  doc_url?: string | null;
  draft_id?: string | null;
  mcp_calls?: McpCall[];
  publish_errors?: string[];
  agent_status?: string;
  agent_reason?: string | null;
  agent_trace?: AgentStep[];
}

export interface RunSummary {
  week: string;
  status: RunStatus;
  window: { start: string; end: string } | null;
  reviews: number | null;
  avgRating: number | null;
  validated: boolean;
  docUrl: string | null;
  draftId: string | null;
  flags: string[];
}

export interface RunDetail extends RunSummary {
  run: RunFile;
  themes: ThemesFile | null;
  quotes: PublicQuote[];
  pulseMarkdown: string | null;
  wordCount: number | null;
}
