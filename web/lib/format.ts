export function pct(x: number | null | undefined, digits = 0): string {
  return x == null ? "–" : `${(x * 100).toFixed(digits)}%`;
}

export function signedPts(x: number | null | undefined): string {
  if (x == null) return "–";
  const pts = x * 100;
  return `${pts > 0 ? "+" : ""}${pts.toFixed(1)} pts`;
}

export function rating(x: number | null | undefined): string {
  return x == null ? "–" : x.toFixed(1);
}

export function windowLabel(w: { start: string; end: string } | null | undefined): string {
  return w ? `${w.start} – ${w.end}` : "–";
}
