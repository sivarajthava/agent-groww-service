from pathlib import Path

from groww_pulse.pulse.render import render_html, render_markdown
from pulse_fixtures import DRAFT, QUOTES, RENDER_ARGS

GOLDEN = Path(__file__).parent / "fixtures" / "golden_pulse.md"


def test_render_matches_golden():
    assert render_markdown(**RENDER_ARGS, draft=DRAFT) == GOLDEN.read_text(encoding="utf-8")


def test_quotes_inserted_verbatim():
    md = render_markdown(**RENDER_ARGS, draft=DRAFT)
    for q in QUOTES:
        assert f'- "{q.text}" (★{q.rating})' in md


def test_template_markup_in_inputs_is_not_executed():
    from dataclasses import replace

    from pulse_fixtures import QUOTES as Q

    quotes = [replace(Q[0], text="Fees are {{ 7*7 }} rupees higher than they should be today"), *Q[1:]]
    md = render_markdown(**{**RENDER_ARGS, "quotes": quotes}, draft=DRAFT)
    assert "{{ 7*7 }}" in md and "49" not in md


def test_render_html_structure_and_escaping():
    html = render_html("# Title & more\n\nWindow: a < b\n\n## Top\n\n1. One\n2. Two\n\n- \"q\" (★1)\n")
    assert "<h1>Title &amp; more</h1>" in html
    assert "<p>Window: a &lt; b</p>" in html
    assert "<h2>Top</h2>" in html
    assert "<ol><li>One</li><li>Two</li></ol>" in html
    assert "<ul><li>&quot;q&quot; (★1)</li></ul>" in html


def test_render_html_of_golden_keeps_all_quotes():
    html = render_html(GOLDEN.read_text(encoding="utf-8"))
    assert html.count("<li>") == 9
