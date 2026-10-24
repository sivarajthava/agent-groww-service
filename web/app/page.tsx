import Link from "next/link";

import { StatusBadge } from "@/components/StatusBadge";
import { rating, windowLabel } from "@/lib/format";
import { dataDir, listRuns } from "@/lib/runs";

export const dynamic = "force-dynamic"; // new runs appear without a rebuild

export default async function Home() {
  const runs = await listRuns();

  if (runs.length === 0) {
    return (
      <div className="card empty">
        <h1>No runs yet</h1>
        <p>
          Nothing found in <code>{dataDir()}/runs</code>. Produce a pulse with the pipeline, then refresh:
        </p>
        <pre>pulse run --dry-run</pre>
      </div>
    );
  }

  const latest = runs[0];
  const published = runs.filter((r) => r.status === "ok").length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Weekly runs</h1>
          <p>One pulse per ISO week: top 3 themes, 3 verbatim quotes, 3 action ideas.</p>
        </div>
        <Link href={`/runs/${latest.week}`} className="btn btn-primary">
          Open latest pulse →
        </Link>
      </div>

      <section className="tiles" aria-label="Latest run">
        <div className="tile">
          <div className="tile-label">Latest week</div>
          <div className="tile-value">{latest.week}</div>
          <div className="tile-note"><StatusBadge status={latest.status} /></div>
        </div>
        <div className="tile">
          <div className="tile-label">Reviews analyzed</div>
          <div className="tile-value">{latest.reviews?.toLocaleString("en-IN") ?? "–"}</div>
          <div className="tile-note">{windowLabel(latest.window)}</div>
        </div>
        <div className="tile">
          <div className="tile-label">Avg rating</div>
          <div className="tile-value">★{rating(latest.avgRating)}</div>
          <div className="tile-note">across the review window</div>
        </div>
        <div className="tile">
          <div className="tile-label">Published</div>
          <div className="tile-value">{published}/{runs.length}</div>
          <div className="tile-note">weeks with Doc + draft</div>
        </div>
      </section>

      <section className="card">
        <h2>All runs <span className="card-sub">{runs.length} weeks</span></h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Week</th>
                <th>Status</th>
                <th>Window</th>
                <th className="num">Reviews</th>
                <th className="num">Avg ★</th>
                <th>Flags</th>
                <th>Doc</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.week} className="link-row">
                  <td><Link href={`/runs/${r.week}`}><strong>{r.week}</strong></Link></td>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="secondary">{windowLabel(r.window)}</td>
                  <td className="num">{r.reviews?.toLocaleString("en-IN") ?? "–"}</td>
                  <td className="num">{rating(r.avgRating)}</td>
                  <td>
                    <div className="chips">
                      {r.flags.length ? r.flags.map((f) => <span key={f} className="chip">{f}</span>) : <span className="muted">–</span>}
                    </div>
                  </td>
                  <td>
                    {r.docUrl ? (
                      <a href={r.docUrl} target="_blank" rel="noreferrer">Google Doc ↗</a>
                    ) : (
                      <span className="muted">–</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
