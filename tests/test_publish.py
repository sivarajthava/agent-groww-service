from pathlib import Path

import pytest
import yaml
from fakes.fake_mcp import fake_session_factory, make_fake_server

from groww_pulse.config import McpConfig
from groww_pulse.publish.docs import doc_title, publish_doc
from groww_pulse.publish.gmail import email_subject, upsert_draft
from groww_pulse.publish.mcp_client import McpCallError, McpGateway

WEEK = "2026-W43"
TO = "me+groww-pulse@example.com"
MD = "# Groww — Weekly Review Pulse\n\n1. Withdrawal Delays — 30% — money stuck\n"
HTML = "<h1>Groww — Weekly Review Pulse</h1><ol><li>Withdrawal Delays</li></ol>"
# The repo's real capability map, so the publishers are tested against what a live run would use.
MCP = McpConfig(**yaml.safe_load(Path(__file__).parents[1].joinpath("config.yaml").read_text(encoding="utf-8"))["mcp"])


async def no_sleep(_: float) -> None:
    pass


def docs_gw(server) -> McpGateway:
    return McpGateway(MCP.docs, "docs", session_factory=fake_session_factory(server), sleep=no_sleep)


def gmail_gw(server) -> McpGateway:
    return McpGateway(MCP.gmail, "gmail", session_factory=fake_session_factory(server), sleep=no_sleep)


async def publish(server, markdown: str = MD, html: str = HTML) -> tuple[str, str, str]:
    async with docs_gw(server) as gw:
        doc_id, doc_url = await publish_doc(gw, WEEK, markdown)
    async with gmail_gw(server) as gw:
        draft_id = await upsert_draft(gw, WEEK, TO, markdown, html, doc_url)
    return doc_id, doc_url, draft_id


def test_titles_carry_the_iso_week():
    assert doc_title(WEEK) == "Groww Weekly Pulse — 2026-W43"
    assert email_subject(WEEK) == "[Groww Pulse] 2026-W43 — weekly review pulse"


async def test_first_run_creates_doc_and_draft():
    server = make_fake_server()
    doc_id, doc_url, draft_id = await publish(server)
    assert server.docs == {doc_id: {"title": doc_title(WEEK), "content": MD, "url": doc_url}}
    assert doc_url == "https://docs.example.com/d/doc1"
    assert list(server.drafts) == [draft_id]
    assert server.drafts[draft_id]["to"] == TO
    assert server.drafts[draft_id]["subject"] == email_subject(WEEK)
    assert server.sent == []


async def test_rerun_same_week_updates_not_duplicates():
    server = make_fake_server()
    first = await publish(server, "old pulse", "<p>old pulse</p>")
    second = await publish(server)
    assert first == second
    assert len(server.docs) == 1 and len(server.drafts) == 1
    assert server.docs[first[0]]["content"] == MD
    assert server.drafts[first[2]]["body"].endswith(MD)
    assert "old pulse" not in server.drafts[first[2]]["htmlBody"]


async def test_draft_contains_doc_link_and_full_pulse():
    server = make_fake_server()
    _, doc_url, draft_id = await publish(server)
    draft = server.drafts[draft_id]
    assert draft["body"] == f"Doc: {doc_url}\n\n{MD}"
    assert draft["htmlBody"].startswith(f'<p>Doc: <a href="{doc_url}">{doc_url}</a></p>')
    assert draft["htmlBody"].endswith(HTML)


async def test_draft_without_doc_url_inlines_pulse():
    server = make_fake_server()
    async with gmail_gw(server) as gw:
        draft_id = await upsert_draft(gw, WEEK, TO, MD, HTML, None)
    assert server.drafts[draft_id]["body"] == MD
    assert server.drafts[draft_id]["htmlBody"] == HTML


async def test_lost_create_response_does_not_duplicate():
    server = make_fake_server()
    server.lose_response_of = {"create_document", "create_draft"}
    doc_id, doc_url, draft_id = await publish(server)
    assert (doc_id, doc_url) == ("doc1", "https://docs.example.com/d/doc1")
    assert len(server.docs) == 1 and server.docs["doc1"]["content"] == MD
    assert draft_id == "draft1" and len(server.drafts) == 1


async def test_failed_create_that_did_not_happen_reraises():
    server = make_fake_server()
    server.fail_next["create_document"] = RuntimeError("quota exceeded")
    async with docs_gw(server) as gw:
        with pytest.raises(McpCallError, match="quota exceeded"):
            await publish_doc(gw, WEEK, MD)
        assert [c["action"] for c in gw.calls] == ["find_doc", "create_doc", "find_doc"]  # never re-created
    assert server.docs == {}


async def test_similar_title_from_other_week_not_reused():
    server = make_fake_server()
    server.fuzzy_search = True
    server.docs["old"] = {"title": "Groww Weekly Pulse — 2026-W42", "content": "last week", "url": "https://x/old"}
    server.drafts["oldd"] = {"to": TO, "subject": email_subject("2026-W42"), "body": "last week", "htmlBody": ""}
    doc_id, _, draft_id = await publish(server)
    assert doc_id != "old" and draft_id != "oldd"
    assert server.docs["old"]["content"] == "last week" and server.drafts["oldd"]["body"] == "last week"
    assert len(server.docs) == 2 and len(server.drafts) == 2


async def test_find_is_retried_as_a_read():
    server = make_fake_server()
    server.fail_next["search_documents"] = RuntimeError("flaky")
    server.fail_next["list_drafts"] = RuntimeError("flaky")
    await publish(server)
    assert len(server.docs) == 1 and len(server.drafts) == 1
