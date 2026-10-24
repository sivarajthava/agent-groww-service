"""End-to-end edge cases E1–E33 (implementation plan, Task 8.5).

Each case runs the whole pipeline (`run_pipeline`, dry run unless the case is about publishing) with fakes, or the
component that owns the behaviour when the case is about one boundary (config, CSV, agent tools). Test names
carry the case id from the plan's table.
"""

import json
import logging
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import groq
import httpx
import pytest
from pydantic import ValidationError

from groww_pulse.agent.loop import run_agent
from groww_pulse.agent.tools import AgentTools
from groww_pulse.config import McpServer, ToolSpec, load_settings
from groww_pulse.evals.fake_mcp import fake_session_factory, make_fake_server
from groww_pulse.evals.harness import _respond, load_jsonl, offline_llm
from groww_pulse.ingest.play_store import PlayStoreCsvSource, PlayStorePublicSource
from groww_pulse.llm import FakeLLM, GroqClient, RequestTooLarge
from groww_pulse.models import RawReview
from groww_pulse.privacy.scrubber import HINGLISH_WORDS
from groww_pulse.publish.docs import doc_title
from groww_pulse.publish.gmail import email_subject
from groww_pulse.publish.mcp_client import McpGateway
from groww_pulse.runner import RunContext, RunDeps, run_lock, run_pipeline
from groww_pulse.storage.db import Database
from groww_pulse.storage.raw import save_snapshot
from groww_pulse.themes.embed import HashEmbedder
from groww_pulse.themes.quotes import is_code_mixed
from groww_pulse.weeks import current_iso_week, week_window

FIXTURES = Path(__file__).parent / "fixtures"
WEEK, TODAY = "2026-W43", date(2026, 10, 26)
ENGLISH = [r for r in load_jsonl("reviews_labeled.jsonl")
           if r["lang"] == "en" and r["gold_theme"] != "other" and not r["id"].startswith("dup")]


class Source:
    name = "play_store"

    def __init__(self, items=None, error=None):
        self.items, self.error, self.fetches = items or [], error, 0

    def fetch(self, since, until):
        self.fetches += 1
        if self.error:
            raise self.error
        return list(self.items)


def raw(text, rating=3, rid=None, at=None, user=None):
    return RawReview(rid or f"gp:{abs(hash((text, rating, str(at)))) % 10**12}", rating, None, text,
                     at or datetime(2026, 10, 20, 9), "15.2.0", user)


def dataset(n=None, rating=None, rows=ENGLISH):
    rows = rows[:n] if n else rows
    return [raw(r["text"], rating or r["rating"], f"gp:{r['id']}", datetime.fromisoformat(r["date"] + "T09:00"))
            for r in rows]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_ID_SALT", "edge-salt")
    return load_settings().model_copy(update={"data_dir": tmp_path})


def run(settings, items=None, *, source=None, llm=None, dry_run=True, **kw):
    kw.setdefault("today", TODAY)
    return run_pipeline(settings, WEEK, dry_run=dry_run, source=source or Source(items), llm=llm or offline_llm(),
                        embedder=kw.pop("embedder", HashEmbedder(dim=256)), sleep=lambda _: None, **kw)


def out(settings, name):
    return settings.data_dir / "runs" / WEEK / name


def run_json(settings):
    return json.loads(out(settings, "run.json").read_text(encoding="utf-8"))


def quotes(settings):
    return json.loads(out(settings, "quotes.json").read_text(encoding="utf-8"))["quotes"]


def stored_texts(settings):
    with Database(settings.data_dir / "reviews.sqlite") as db:
        return [r.text for r in db.reviews_in_window(*week_window(WEEK, 12))]


def gateways(settings, server, docs_cfg=None, timeout_s=30):
    f = fake_session_factory(server)
    return (McpGateway(docs_cfg or settings.mcp.docs, "docs", timeout_s, session_factory=None if docs_cfg else f),
            McpGateway(settings.mcp.gmail, "gmail", timeout_s, session_factory=f))


# --- Volume -----------------------------------------------------------------------------------------------
def test_E01_no_reviews_and_no_snapshot(settings):
    result = run(settings, [])
    assert result.status == "failed" and result.error.startswith("no_reviews")
    assert result.pulse_path is None


def test_E02_one_review_fails_with_clear_quote_message(settings):
    result = run(settings, dataset(1))
    assert result.status == "failed"
    assert "found 1 of 3 quotes" in result.error and "themes:" in result.error


def test_E03_eight_reviews_widen_window_and_flag(settings):
    result = run(settings, dataset(8))
    assert result.status == "dry_run", result.error
    assert {"window_widened", "low_volume", "few_themes"} <= set(result.flags)
    assert len(quotes(settings)) == 3


@pytest.mark.slow  # ~2.5 min: langdetect is ~9 ms per review; the eval's edge gate still runs it
def test_E04_twenty_thousand_reviews(settings):
    rng = random.Random(4)
    words = " ".join(r["text"].lower().rstrip(".") for r in ENGLISH).split()
    items = [raw(" ".join(rng.choice(words) for _ in range(12)) + f" case {i}", rng.randint(1, 5), f"gp:big{i}",
                 datetime(2026, 8, 17) + timedelta(hours=i % 1600)) for i in range(20_000)]
    seen = []

    def spy(system, user, schema):
        seen.append(user)
        return _respond(system, user, schema)

    start = time.perf_counter()
    result = run(settings, items, llm=FakeLLM([spy] * 20))
    assert result.status == "dry_run", result.error
    assert time.perf_counter() - start < 600
    label_prompt = next(u for u in seen if "<clusters>" in u and "<reviews>" in u)
    for block in label_prompt.split("<reviews>")[1:]:
        assert block.split("</reviews>")[0].count("<review id=") <= 15  # prompt-size guard
    assert run_json(settings)["timings"]["themes"] > 0  # runtime logged


# --- Sentiment ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("case, rating, ok", [("E05", 5, lambda q: q >= 4), ("E06", 1, lambda q: q <= 2)])
def test_E05_E06_one_sided_sentiment(settings, case, rating, ok):
    result = run(settings, dataset(rating=rating))
    assert result.status == "dry_run", result.error
    assert all(ok(q["rating"]) for q in quotes(settings))
    themes = json.loads(out(settings, "themes.json").read_text(encoding="utf-8"))["themes"]
    assert all(t["neg_share"] == (0.0 if rating == 5 else 1.0) for t in themes)


# --- Language -----------------------------------------------------------------------------------------------
def test_E07_all_hindi_fails_with_no_english_reviews(settings):
    hindi = [r for r in load_jsonl("reviews_labeled.jsonl") if r["lang"] == "hi"]
    result = run(settings, dataset(rows=hindi))
    assert result.status == "failed" and result.error.startswith("no_english_reviews")
    counts = run_json(settings)["stages"]["normalize"]  # counts recorded: every kept review is Hindi
    assert counts["raw"] == len(hindi) and counts["kept"] == counts["lang_hi"] >= 1


def test_E08_hinglish_is_tagged_and_never_quoted(settings):
    hinglish = [r for r in load_jsonl("reviews_labeled.jsonl") if r["lang"] == "hi-Latn"]
    mixed = [raw(f"app bahut slow hai yaar {t['text'].lower()}", 1, f"gp:mix{i}") for i, t in enumerate(ENGLISH[:40])]
    result = run(settings, dataset() + dataset(rows=hinglish) + mixed)
    assert result.status == "dry_run", result.error
    assert not any(is_code_mixed(q["text"]) or set(q["text"].lower().split()) & {"hai", "bahut"}
                   for q in quotes(settings))


# --- Text -------------------------------------------------------------------------------------------------
def test_E09_emoji_punctuation_zero_width_and_rtl_are_noise(settings):
    junk = ["👍👍👍 🔥🔥", "!!!!!!!! ??? ...", "​​​‍", "‮" + "تطبيق جيد جدا" + "‬", "⭐⭐⭐⭐⭐"]
    result = run(settings, dataset() + [raw(t, 5, f"gp:junk{i}") for i, t in enumerate(junk)])
    assert result.status == "dry_run", result.error
    stored = stored_texts(settings)
    visible = [v for t in junk if (v := t.strip("​‍‮‬ "))]
    assert len(visible) == 4 and not any(v in s for v in visible for s in stored)
    assert run_json(settings)["stages"]["normalize"]["raw"] == len(ENGLISH) + len(junk)  # counted


def test_E10_huge_review_is_trimmed_for_quotes(settings):
    huge = ("The withdrawal is pending and support does not reply. " * 100)[:5000]
    items = [raw(huge, 1, f"gp:huge{i}") for i in range(1)] + dataset()
    result = run(settings, items)
    assert result.status == "dry_run", result.error
    assert all(len(q["text"].rstrip("…").split()) <= 40 for q in quotes(settings))


def test_E11_markup_in_reviews_never_reaches_the_pulse(settings):
    tricky = ["**bold claim** the app crashes every time I open the portfolio after the update",
              "<script>alert(1)</script> the app freezes on the login screen after the update today",
              "the app is very slow, see [link](http://x.example) for my screen recording of the bug"]
    items = [raw(t, 1, f"gp:md{i}") for i, t in enumerate(tricky * 5)] + dataset(40)
    result = run(settings, items)
    assert result.status == "dry_run", result.error
    md = result.pulse_path.read_text(encoding="utf-8")
    assert "<script>" not in md and "http" not in md and "**" not in md and "](" not in md
    from groww_pulse.pulse.render import render_html

    assert "<script>" not in render_html("- \"<script>x</script>\" (★1)")


def test_E12_review_of_only_pii_becomes_noise(settings):
    only_pii = "9876543210 rahul.verma@gmail.com ABCDE1234F 2345 6789 0123 rv12@okaxis"
    result = run(settings, dataset() + [raw(only_pii, 1, "gp:pii")])
    assert result.status == "dry_run", result.error
    assert not any("[phone]" in s and "[email]" in s for s in stored_texts(settings))
    assert all("[" not in q["text"] or "rahul" not in q["text"] for q in quotes(settings))


def test_E13_quote_with_redaction_token_passes_fidelity(settings):
    named = [raw(f"Hi Rahul, {r['text'][0].lower()}{r['text'][1:]}", r["rating"], f"gp:n{r['id']}",
                 datetime.fromisoformat(r["date"] + "T09:00")) for r in ENGLISH]
    result = run(settings, named)
    assert result.status == "dry_run", result.error
    md = result.pulse_path.read_text(encoding="utf-8")
    assert "[name]" in md and "Rahul" not in md


# --- Dedup ------------------------------------------------------------------------------------------------
def test_E14_duplicates_and_spam_collapse(settings):
    page_dupe = dataset(30)
    spam = [raw("Earn ten thousand daily with this one simple trading trick, join the group now", 5, f"gp:spam{i}")
            for i in range(50)]
    result = run(settings, page_dupe + page_dupe[:10] + spam)
    assert result.status in ("dry_run", "failed")
    stored = stored_texts(settings)
    assert len(stored) == len(set(stored)) and sum("simple trading trick" in s for s in stored) == 1
    assert run_json(settings)["stages"]["normalize"]["kept"] == 31


# --- Dates ------------------------------------------------------------------------------------------------
def test_E15_dates_use_ist_and_inclusive_bounds(settings):
    # Sunday 2026-10-25 23:59 IST = 18:29 UTC (in W43); Monday 00:30 IST = Sunday 19:00 UTC (W44 in IST).
    edge = [raw("Sunday night: the withdrawal is pending and support has no update at all", 1, "gp:sun",
                datetime(2026, 10, 25, 18, 29)),
            raw("Monday morning: the withdrawal is pending and support has no update at all", 1, "gp:mon",
                datetime(2026, 10, 25, 19, 0)),
            raw("First day of the window: the app crashes every time I open the portfolio", 1, "gp:first",
                datetime(2026, 8, 2, 19, 0))]  # = 2026-08-03 00:30 IST, the first day of the 12-week window
    run(settings, dataset() + edge)
    with Database(settings.data_dir / "reviews.sqlite") as db:
        rows = {r.text.split(":")[0]: r.date for r in db.reviews_in_window(*week_window(WEEK, 12))}
    assert rows["Sunday night"] == date(2026, 10, 25) and "Monday morning" not in rows
    assert rows["First day of the window"] == date(2026, 8, 3)


def test_E16_year_boundary_and_week_53():
    assert week_window("2026-W53", 1) == (date(2026, 12, 28), date(2027, 1, 3))
    assert week_window("2027-W01", 1) == (date(2027, 1, 4), date(2027, 1, 10))
    assert current_iso_week(date(2027, 1, 1)) == "2026-W53"
    with pytest.raises(ValueError):
        week_window("2025-W53", 1)  # 2025 has 52 ISO weeks
    assert doc_title("2026-W53") == "Groww Weekly Pulse — 2026-W53"
    assert email_subject("2027-W01") == "[Groww Pulse] 2027-W01 — weekly review pulse"


# --- History ----------------------------------------------------------------------------------------------
def test_E17_no_previous_week(settings):
    sparse = [r for r in dataset() if r.at.date() < date(2026, 10, 12)]  # nothing in W42/W43
    result = run(settings, sparse)
    assert result.status == "dry_run", result.error
    with Database(settings.data_dir / "reviews.sqlite") as db:
        assert all(t.wow_delta is None for t in db.load_themes(WEEK))


def test_E18_previous_names_offered_for_reuse(settings):
    run_pipeline(settings, "2026-W42", dry_run=True, source=Source(dataset()), llm=offline_llm(),
                 embedder=HashEmbedder(dim=256), today=TODAY, sleep=lambda _: None)
    seen = []

    def spy(system, user, schema):
        seen.append(user)
        return _respond(system, user, schema)

    run(settings, dataset(), llm=FakeLLM([spy] * 20))
    label = next(u for u in seen if "<reviews>" in u)
    assert "Previous week's theme names: [" in label and "App Performance & Bugs" in label.split("\n")[0]


# --- LLM --------------------------------------------------------------------------------------------------
def test_E19_every_cluster_same_name_gives_reduced_pulse(settings):
    def same(system, user, schema):
        out_ = _respond(system, user, schema)
        if "<data>" not in user:
            for e in next(iter(out_.values())):
                e["name"], e["description"] = "General Feedback", "Mixed feedback."
        return out_

    result = run(settings, dataset(), llm=FakeLLM([same] * 20))
    assert result.status == "dry_run", result.error
    assert "few_themes" in result.flags and len(quotes(settings)) == 3


def _rate_limited():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return groq.RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


class _AlwaysFails:
    def __init__(self, error):
        self.error, self.calls = error, 0
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self.calls += 1
        raise self.error()


def test_E20_groq_down_on_every_call(settings):
    client = GroqClient("primary", "fallback", sleep=lambda _: None)
    client._client = _AlwaysFails(_rate_limited)
    result = run(settings, dataset(), llm=client)
    assert result.status == "failed" and result.error.startswith("themes: llm_unavailable")
    assert client._client.calls == 4  # 3 tries on the primary model, then the fallback once


def test_E21_request_too_large_shrinks_samples(settings):
    sizes = []

    def limited(system, user, schema):
        if "<reviews>" in user:
            per_cluster = max(b.split("</reviews>")[0].count("<review id=") for b in user.split("<reviews>")[1:])
            sizes.append(per_cluster)
            if per_cluster > 4:
                raise RequestTooLarge("413: request too large")
        return _respond(system, user, schema)

    result = run(settings, dataset(), llm=FakeLLM([limited] * 20))
    assert result.status == "dry_run", result.error
    assert sizes[:3] == [15, 8, 4]


def test_E21_groq_413_maps_to_request_too_large():
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    client = GroqClient("m", sleep=lambda _: None)
    client._client = _AlwaysFails(lambda: groq.APIStatusError("too large", response=httpx.Response(413, request=req),
                                                              body=None))
    with pytest.raises(RequestTooLarge):
        client.complete_json("s", "u", {"type": "object"})
    assert client._client.calls == 1  # not retried as transient


def test_E22_injection_reviews_leave_no_trace(settings):
    injection = load_jsonl("injection.jsonl")
    result = run(settings, dataset() + dataset(rows=injection))
    assert result.status == "dry_run", result.error
    md = result.pulse_path.read_text(encoding="utf-8").lower()
    for marker in ("http", "www.", "scam", "ignore", "<script>", "hacked", "ceo", "five action"):
        assert marker not in md, marker


# --- Source -----------------------------------------------------------------------------------------------
def test_E23_scraper_fails_on_page_two(settings):
    old = dataset(60)
    save_snapshot(settings.data_dir, "play_store", date(2026, 10, 20), old)
    page1 = [{"reviewId": f"gp:p1-{i}", "score": 1, "content": r["text"], "at": datetime(2026, 10, 22, 12),
              "reviewCreatedVersion": "15.3.0", "userName": "A Google user"} for i, r in enumerate(ENGLISH[60:90])]
    calls = []

    class Token:
        token = "next"

    def fetch_page(app_id, **kw):
        calls.append(kw["continuation_token"])
        if kw["continuation_token"] is None:
            return page1, Token()
        raise ConnectionError("page 2 timed out")

    source = PlayStorePublicSource("com.nextbillion.groww", "en", "in", fetch_page=fetch_page, sleep=lambda _: None)
    result = run(settings, source=source)
    assert result.status == "dry_run", result.error
    assert "partial_fetch" in result.flags
    assert len(calls) == 4  # page 1, then page 2 tried 3 times
    assert run_json(settings)["stages"]["ingest"]["fetched"] == 30
    assert run_json(settings)["stages"]["ingest"]["snapshot"] == 90  # page 1 merged with the older snapshot


def test_E24_csv_variants(settings):
    since, until = week_window(WEEK, 12)
    bom = PlayStoreCsvSource(FIXTURES / "play_bom_extra.csv").fetch(since, until)
    assert [r.source_review_id for r in bom] == ["gp:csv-1", "gp:csv-2"] and bom[1].title == "Nice"
    plain = PlayStoreCsvSource(FIXTURES / "play_no_optional.csv").fetch(since, until)
    assert plain[0].title is None and plain[0].app_version is None
    with pytest.raises(ValueError, match="Star Rating"):
        PlayStoreCsvSource(FIXTURES / "play_missing_rating.csv").fetch(since, until)
    result = run(settings, source=PlayStoreCsvSource(FIXTURES / "play_missing_rating.csv"))
    assert result.status == "failed" and "Star Rating" in result.error


# --- MCP --------------------------------------------------------------------------------------------------
def test_E25_non_json_text_result_fails_clearly(settings):
    def create_document(title: str, content: str):
        return "created!"

    server = make_fake_server({"create_document": create_document})
    result = run(settings, dataset(), dry_run=False, gateways=gateways(settings, server))
    assert result.status == "partial" and "docs_failed" in result.flags
    errors = run_json(settings)["publish_errors"]
    assert any("no document id" in e and "result map" in e for e in errors)
    assert len(server.drafts) == 1


def test_E26_two_drafts_with_the_same_subject(settings, caplog):
    server = make_fake_server()
    subject = email_subject(WEEK)
    server.drafts["d-old"] = {"to": "x", "subject": subject, "body": "old", "htmlBody": "", "date": "2026-10-01"}
    server.drafts["d-new"] = {"to": "x", "subject": subject, "body": "new", "htmlBody": "", "date": "2026-10-20"}
    with caplog.at_level(logging.WARNING):
        result = run(settings, dataset(), dry_run=False, gateways=gateways(settings, server))
    assert result.status == "ok" and result.draft_id == "d-new"
    assert len(server.drafts) == 2 and server.drafts["d-old"]["body"] == "old"
    assert "d-old" in caplog.text and "d-new" in caplog.text


def test_E27_doc_deleted_between_runs(settings):
    server = make_fake_server()
    first = run(settings, dataset(), dry_run=False, gateways=gateways(settings, server))
    server.docs.clear()
    second = run(settings, dataset(), dry_run=False, gateways=gateways(settings, server))
    assert second.status == "ok" and second.doc_url != first.doc_url
    assert run_json(settings)["doc_url"] == second.doc_url and len(server.docs) == 1


def test_E28_stdio_server_crashes_mid_run(settings):
    docs = settings.mcp.docs.model_copy(update={"transport": "stdio", "command": sys.executable,
                                                "args": [str(FIXTURES / "crash_server.py")]})
    server = make_fake_server()
    start = time.perf_counter()
    result = run(settings, dataset(), dry_run=False, gateways=gateways(settings, server, docs_cfg=docs, timeout_s=10))
    assert result.status == "partial" and "docs_failed" in result.flags
    assert len(server.drafts) == 1 and time.perf_counter() - start < 120


def test_E29_send_in_allowlist_rejected_at_startup():
    tools = {"create_draft": ToolSpec(name="create_draft"), "send_message": ToolSpec(name="send_message")}
    with pytest.raises(ValidationError, match="send"):
        McpServer(transport="stdio", command="x", tools=tools, allowlist=["create_draft", "send_message"])


# --- Agent ------------------------------------------------------------------------------------------------
def _tools(settings, server):
    ctx = RunContext(settings, WEEK, RunDeps(source=Source(dataset()), llm=offline_llm(),
                                             embedder=HashEmbedder(dim=256), today=TODAY, sleep=lambda _: None))
    f = fake_session_factory(server)
    return ctx, AgentTools(ctx, lambda name: McpGateway(getattr(settings.mcp, name), name, session_factory=f))


async def test_E30_agent_cannot_publish_through_run_stage_or_another_week(settings):
    server = make_fake_server()
    _, tools = _tools(settings, server)
    assert (await tools.dispatch("run_stage", {"week": WEEK, "stage": "publish"}))["ok"] is False
    assert (await tools.dispatch("publish_doc", {"week": "2026-W42"}))["ok"] is False
    assert server.docs == {} and server.drafts == {}


async def test_E31_agent_claiming_success_without_publishing_is_not_ok(settings):
    server = make_fake_server()
    ctx, tools = _tools(settings, server)
    result = await run_agent(ctx, FakeLLM([{"role": "assistant", "content": "Published! All done."}]), tools)
    assert result.status == "failed" and server.docs == {}


# --- Ops / config -----------------------------------------------------------------------------------------
def test_E32_second_run_for_the_same_week_is_locked(settings):
    with run_lock(settings, WEEK):
        result = run(settings, dataset())
    assert result.status == "failed" and result.error.startswith("locked")
    assert run(settings, dataset()).status == "dry_run"  # the lock is released afterwards


def test_E33_missing_secrets_fail_before_any_fetch(settings, tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    source = Source(dataset())
    result = run_pipeline(settings, WEEK, dry_run=True, source=source, embedder=HashEmbedder(dim=256), today=TODAY)
    assert result.status == "failed" and "GROQ_API_KEY" in result.error and source.fetches == 0

    monkeypatch.delenv("PULSE_ID_SALT")
    monkeypatch.chdir(tmp_path)  # keep the developer's .env out of it
    with pytest.raises(ValidationError, match="PULSE_ID_SALT"):
        load_settings(Path(__file__).parents[2] / "config.yaml")
