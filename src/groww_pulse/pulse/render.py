"""Render the pulse to Markdown (problemStatement.md template) and to simple HTML for the email."""

import html
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from groww_pulse.models import PulseDraft, Quote, Theme
from groww_pulse.pulse.generate import week_monday

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent),
    autoescape=False,  # Markdown output; values are inserted as data, never evaluated
    keep_trailing_newline=True,
    undefined=StrictUndefined,
)


def render_markdown(
    week: str,
    window: tuple[date, date],
    total_reviews: int,
    avg_rating: float,
    top: list[Theme],
    quotes: list[Quote],
    draft: PulseDraft,
) -> str:
    return _env.get_template("template.md.j2").render(
        week_of=week_monday(week).isoformat(),
        start=window[0].isoformat(),
        end=window[1].isoformat(),
        total_reviews=total_reviews,
        avg_rating=f"{avg_rating:.1f}",
        themes=[{"name": t.name, "share": round(t.share * 100), "summary": s}
                for t, s in zip(top, draft.theme_summaries)],
        quotes=quotes,
        actions=draft.actions,
    )


def render_html(markdown: str) -> str:
    """Headings, ordered/unordered lists and paragraphs; every text run is HTML-escaped."""
    out: list[str] = []
    open_list = None
    for line in markdown.splitlines():
        m = re.match(r"(#{1,2}) (.*)|(\d+)\. (.*)|- (.*)", line)
        kind = None if not m else "h" if m.group(1) else "ol" if m.group(3) else "ul"
        if open_list and kind != open_list:
            out.append(f"</{open_list}>")
            open_list = None
        if not line.strip():
            continue
        if kind == "h":
            level = len(m.group(1))
            out.append(f"<h{level}>{html.escape(m.group(2))}</h{level}>")
        elif kind in ("ol", "ul"):
            if open_list != kind:
                out.append(f"<{kind}>")
                open_list = kind
            out.append(f"<li>{html.escape(m.group(4) if kind == 'ol' else m.group(5))}</li>")
        else:
            out.append(f"<p>{html.escape(line)}</p>")
    if open_list:
        out.append(f"</{open_list}>")
    return "".join(out)
