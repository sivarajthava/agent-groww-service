import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";

import { CopyButton } from "@/components/CopyButton";
import { PulseView } from "@/components/PulseView";
import { CheckBadge, StatusBadge } from "@/components/StatusBadge";
import { ThemeTable } from "@/components/ThemeTable";
import { rating, windowLabel } from "@/lib/format";
import { getRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

const WORD_BUDGET = 250;
const STAGE_ORDER = ["ingest", "normalize", "themes", "pulse", "publish"];

type Params = Promise<{ week: string }>;

export async function generateMetadata({ params }: { params: Params }): Promise<Metadata> {
  const { week } = await params;
  return { title: `Pulse ${week} · Groww Weekly Pulse` };
}

export default async function RunPage({ params }: { params: Params }) {
  const { week } = await params;
  const detail = await getRun(week);
  if (!detail) notFound();

  const { run, themes, quotes, pulseMarkdown, wordCount } = detail;
  const themeName = new Map(themes?.themes.map((t) => [t.id, t.name]) ?? []);
  const stages = STAGE_ORDER.filter((s) => run.timings?.[s] != null || run.stages?.[s]);
  const mcpCalls = run.mcp_calls ?? [];
  const trace = run.agent_trace ?? [];

  return (
    <>
      <div className="page-head">
        <div>
          <Link href="/" className="crumb">← All runs</Link>
          <h1>
            {week} <StatusBadge status={detail.status} />
          </h1>
          <p>
            Window {windowLabel(detail.window)} · {detail.reviews?.toLocaleString("en-IN") ?? "–"} reviews · avg ★
            {rating(detail.avgRating)}
          </p>
        </div>
        <div className="actions">
          {detail.docUrl && (
            <a className="btn btn-primary" href={detail.docUrl} target="_blank" rel="noreferrer">Open Google Doc ↗</a>
          )}
          {pulseMarkdown && <CopyButton text={pulseMarkdown} />}
        </div>
      </div>

      {run.error && <div className="callout" style={{ marginBottom: 16 }}><strong>Run stopped:</strong> {run.error}</div>}
      {!!run.publish_errors?.length && (
        <div className="callout warn" style={{ marginBottom: 16 }}>
          <strong>Publish problems:</strong> {run.publish_errors.join(" · ")}
        </div>
      )}

      <div className="grid grid-2">
        <section className="card">
          <h2>
            Pulse
            {wordCount != null && (
              <span className={`badge ${wordCount <= WORD_BUDGET ? "badge-good" : "badge-critical"}`}>
                {wordCount}/{WORD_BUDGET} words
              </span>
            )}
          </h2>
          {pulseMarkdown ? (
            <PulseView markdown={pulseMarkdown} />
          ) : (
            <p className="muted">No pulse.md for this week. The run stopped before the pulse stage finished.</p>
          )}
        </section>

        <div className="stack">
          <section className="card">
            <h2>Delivery <span className="card-sub">via MCP · draft only</span></h2>
            <dl className="kv">
              <dt>Validation</dt>
              <dd>{run.validation ? <CheckBadge ok={run.validation.ok} /> : <span className="muted">not run</span>}</dd>
              <dt>Google Doc</dt>
              <dd>
                {detail.docUrl ? <a href={detail.docUrl} target="_blank" rel="noreferrer">{detail.docUrl}</a> : <span className="muted">not published</span>}
              </dd>
              <dt>Gmail draft</dt>
              <dd>{detail.draftId ? <code>{detail.draftId}</code> : <span className="muted">not created</span>}</dd>
              <dt>Flags</dt>
              <dd>
                <div className="chips">
                  {detail.flags.length ? detail.flags.map((f) => <span key={f} className="chip">{f}</span>) : <span className="muted">none</span>}
                </div>
              </dd>
            </dl>
            {!!run.validation?.failures.length && (
              <ul style={{ margin: "12px 0 0", paddingLeft: 18, color: "var(--critical)" }}>
                {run.validation.failures.map((f) => <li key={f}><code>{f}</code></li>)}
              </ul>
            )}
          </section>

          <section className="card">
            <h2>What users said <span className="card-sub">verbatim · anonymized</span></h2>
            {quotes.length ? (
              <div className="stack" style={{ gap: 10 }}>
                {quotes.map((q, i) => (
                  <blockquote key={i} className="quote">
                    <p>“{q.text}”</p>
                    <footer>
                      <span className="stars" aria-label={`${q.rating} out of 5 stars`}>★{q.rating}</span>
                      {themeName.get(q.theme_id) && <> · {themeName.get(q.theme_id)}</>}
                    </footer>
                  </blockquote>
                ))}
              </div>
            ) : (
              <p className="muted">No quotes selected yet.</p>
            )}
          </section>
        </div>
      </div>

      {themes && (
        <section className="card" style={{ marginTop: 16 }}>
          <h2>Themes <span className="card-sub">{themes.themes.length} of max 5 · top 3 go in the pulse</span></h2>
          <ThemeTable data={themes} />
        </section>
      )}

      <div className="grid grid-2" style={{ marginTop: 16 }}>
        <section className="card">
          <h2>Pipeline stages</h2>
          {stages.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Stage</th><th>Counts</th><th className="num">Seconds</th></tr>
                </thead>
                <tbody>
                  {stages.map((s) => (
                    <tr key={s}>
                      <td><strong>{s}</strong></td>
                      <td className="secondary">
                        {Object.entries(run.stages?.[s] ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "–"}
                      </td>
                      <td className="num">{run.timings?.[s]?.toFixed(1) ?? "–"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">No stage timings recorded.</p>
          )}
        </section>

        <section className="card">
          <h2>Audit</h2>
          <dl className="kv">
            <dt>Model</dt>
            <dd>{run.model ? <code>{run.model}</code> : <span className="muted">–</span>}</dd>
            <dt>Reasoning effort</dt>
            <dd>{run.reasoning_effort ?? "–"}</dd>
            {Object.entries(run.prompts ?? {}).map(([name, hash]) => (
              <div key={name} style={{ display: "contents" }}>
                <dt>Prompt {name}</dt>
                <dd><code>{hash.slice(0, 12)}</code></dd>
              </div>
            ))}
            {run.agent_status && (
              <>
                <dt>Agent</dt>
                <dd>{run.agent_status}{run.agent_reason ? ` (${run.agent_reason})` : ""}</dd>
              </>
            )}
          </dl>
        </section>
      </div>

      {mcpCalls.length > 0 && (
        <section className="card" style={{ marginTop: 16 }}>
          <h2>MCP calls <span className="card-sub">argument keys only, never values</span></h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Server</th><th>Action</th><th>Tool</th><th>Arg keys</th><th className="num">ms</th><th>Result</th></tr>
              </thead>
              <tbody>
                {mcpCalls.map((c, i) => (
                  <tr key={i}>
                    <td>{c.server}</td>
                    <td><code>{c.action}</code></td>
                    <td className="secondary"><code>{c.tool ?? "–"}</code></td>
                    <td><div className="chips">{c.arg_keys.map((k) => <span key={k} className="chip">{k}</span>)}</div></td>
                    <td className="num">{c.ms}</td>
                    <td><CheckBadge ok={c.ok} okLabel="ok" failLabel={c.error ?? "error"} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {trace.length > 0 && (
        <section className="card" style={{ marginTop: 16 }}>
          <h2>Agent trace <span className="card-sub">pulse agent</span></h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th className="num">Step</th><th>Tool</th><th>Args</th><th className="num">ms</th><th>Result</th></tr>
              </thead>
              <tbody>
                {trace.map((t) => (
                  <tr key={t.step}>
                    <td className="num">{t.step}</td>
                    <td><code>{t.tool}</code></td>
                    <td className="secondary"><code>{JSON.stringify(t.args)}</code></td>
                    <td className="num">{t.ms}</td>
                    <td><CheckBadge ok={t.ok} okLabel="ok" failLabel="refused/error" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}
