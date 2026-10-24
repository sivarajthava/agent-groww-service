import Link from "next/link";

export default function NotFound() {
  return (
    <div className="card empty">
      <h1>No run for that week</h1>
      <p>Weeks look like <code>2026-W43</code>. Only weeks with a folder under <code>data/runs/</code> exist.</p>
      <Link href="/" className="btn">← All runs</Link>
    </div>
  );
}
