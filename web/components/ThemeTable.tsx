import { pct, rating, signedPts } from "@/lib/format";
import type { ThemesFile } from "@/lib/types";

/** All ≤ 5 themes, ranked as the pipeline ranked them: the top 3 first, the rest by share. */
export function ThemeTable({ data }: { data: ThemesFile }) {
  const rank = new Map(data.top.map((id, i) => [id, i + 1]));
  const themes = [...data.themes].sort(
    (a, b) => (rank.get(a.id) ?? 99) - (rank.get(b.id) ?? 99) || b.share - a.share,
  );
  const max = Math.max(...themes.map((t) => t.share), 0.01);

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Theme</th>
            <th className="bar-cell">Share of reviews</th>
            <th className="num">Share</th>
            <th className="num">1–2★</th>
            <th className="num">Avg ★</th>
            <th className="num">WoW</th>
          </tr>
        </thead>
        <tbody>
          {themes.map((t) => {
            const r = rank.get(t.id);
            const tip = `${t.name}: ${t.count} reviews (${pct(t.share, 1)}), ${pct(t.neg_share)} negative, avg ★${rating(t.avg_rating)}`;
            return (
              <tr key={t.id} className="theme-row" title={tip}>
                <td>
                  <div className="theme-name">
                    <span className={`rank ${r ? "" : "none"}`} aria-label={r ? `rank ${r}` : "not in pulse"}>
                      {r ?? "–"}
                    </span>
                    {t.name}
                  </div>
                  <div className="theme-desc">{t.description}</div>
                </td>
                <td className="bar-cell" aria-hidden="true">
                  <div className="bar">
                    <div className={`bar-fill ${r ? "" : "dim"}`} style={{ width: `${(t.share / max) * 100}%` }} />
                  </div>
                </td>
                <td className="num">{pct(t.share)}</td>
                <td className="num secondary">{pct(t.neg_share)}</td>
                <td className="num secondary">{rating(t.avg_rating)}</td>
                <td className="num muted">{signedPts(t.wow_delta)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="muted" style={{ fontSize: "0.8rem", margin: "10px 4px 0" }}>
        k = {data.k} clusters merged to {data.themes.length} themes · {data.other_count} reviews in the hidden
        “Other” bucket · ranked by 0.4·share + 0.5·neg + 0.1·growth
      </p>
    </div>
  );
}
