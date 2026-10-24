"use client";

export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="card empty">
      <h1>Can’t load runs right now</h1>
      <p>The pulse backend didn’t answer. It may be restarting after a deploy; try again in a minute.</p>
      <button type="button" className="btn" onClick={reset}>Try again</button>
    </div>
  );
}
