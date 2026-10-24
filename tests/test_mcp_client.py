import json
from contextlib import asynccontextmanager

import anyio
import pytest
from fakes.fake_mcp import fake_session_factory, make_fake_server

from groww_pulse.config import McpServer, ToolSpec
from groww_pulse.publish.mcp_client import (
    CapabilityMissing,
    ConfigError,
    McpCallError,
    McpGateway,
    McpTimeout,
    ToolNotAllowed,
)

TOOLS = {
    "find_doc": ToolSpec(name="search_documents", args={"title": "query"},
                         result={"id": "documentId", "url": "url"}),
    "create_doc": ToolSpec(name="create_document", args={"title": "title", "body": "content"},
                           result={"id": "documentId", "url": "url"}),
    "update_doc": ToolSpec(name="update_document", args={"doc_id": "documentId", "body": "content"}),
    "list_drafts": ToolSpec(name="list_drafts", args={"subject": "query"}, result={"id": "draftId"}),
    "create_draft": ToolSpec(name="create_draft",
                             args={"to": "to", "subject": "subject", "body": "body", "html": "htmlBody"},
                             result={"id": "draftId"}),
    "send_message": ToolSpec(name="send_message", args={"to": "to", "subject": "subject", "body": "body"}),
}
ALLOWED = ["find_doc", "create_doc", "update_doc", "list_drafts", "create_draft"]


def server_cfg(tools: dict[str, ToolSpec] | None = None, allowlist: list[str] | None = None) -> McpServer:
    tools = tools or TOOLS
    return McpServer(transport="stdio", command="unused", tools=tools,
                     allowlist=allowlist if allowlist is not None else [a for a in ALLOWED if a in tools])


def gateway(server, cfg: McpServer | None = None, **kw) -> McpGateway:
    return McpGateway(cfg or server_cfg(), "fake", session_factory=fake_session_factory(server), **kw)


async def no_sleep(seconds: float) -> None:
    no_sleep.calls.append(seconds)


async def test_connects_and_discovers_tools():
    server = make_fake_server()
    async with gateway(server) as gw:
        assert set(gw.available_tools) == {"search_documents", "create_document", "update_document",
                                           "list_drafts", "create_draft", "update_draft", "send_message"}


async def test_call_maps_logical_to_real_tool_and_renames_args():
    server = make_fake_server()
    async with gateway(server) as gw:
        out = await gw.call("create_doc", {"title": "Pulse W43", "body": "hello world"})
    assert list(server.docs.values()) == [
        {"title": "Pulse W43", "content": "hello world", "url": "https://docs.example.com/d/doc1"}]
    assert out["documentId"] == out["id"] == "doc1"
    assert out["url"] == "https://docs.example.com/d/doc1"


async def test_send_blocked_by_allowlist():
    server = make_fake_server()
    async with gateway(server) as gw:
        with pytest.raises(ToolNotAllowed, match="send_message"):
            await gw.call("send_message", {"to": "a@example.com", "subject": "s", "body": "b"})
        with pytest.raises(ToolNotAllowed):
            await gw.call("delete_everything", {})
    assert server.sent == []


async def test_missing_capability_fails_fast():
    server = make_fake_server()
    tools = {**TOOLS, "create_doc": ToolSpec(name="createDoc", args={"title": "title", "body": "content"})}
    with pytest.raises(CapabilityMissing, match="create_document") as e:
        async with gateway(server, server_cfg(tools)):
            pytest.fail("entered despite a missing capability")
    assert e.value.action == "create_doc" and e.value.wanted == "createDoc"
    assert server.docs == {}


async def test_required_param_not_mapped_fails_fast():
    server = make_fake_server()
    tools = {**TOOLS, "update_doc": ToolSpec(name="update_document", args={"body": "content"})}
    with pytest.raises(CapabilityMissing, match="documentId") as e:
        async with gateway(server, server_cfg(tools)):
            pass
    assert e.value.missing_params == ["documentId"]


async def test_is_error_raises_mcp_call_error():
    server = make_fake_server()
    async with gateway(server) as gw:
        with pytest.raises(McpCallError, match="no such document") as e:
            await gw.call("update_doc", {"doc_id": "nope", "body": "x"})
    assert e.value.action == "update_doc"


async def test_text_result_parsed_as_json_and_result_keys_normalized():
    def search_documents(query: str):  # no return annotation -> text content only, no structuredContent
        return json.dumps({"documentId": "d9", "url": "https://docs.example.com/d/d9",
                           "documents": [{"documentId": "d9", "url": "https://docs.example.com/d/d9"}]})

    def plain(query: str):
        return "not json"

    server = make_fake_server({"search_documents": search_documents, "list_drafts": plain})
    async with gateway(server) as gw:
        out = await gw.call("find_doc", {"title": "x"})
        assert (out["id"], out["url"]) == ("d9", "https://docs.example.com/d/d9")
        assert out["documents"][0]["id"] == "d9"  # list items are normalized too
        assert await gw.call("list_drafts", {"subject": "x"}) == {"text": "not json"}


async def test_read_action_retried_write_action_not():
    server = make_fake_server()
    no_sleep.calls = []
    async with gateway(server, sleep=no_sleep) as gw:
        server.fail_next["list_drafts"] = RuntimeError("flaky")
        assert await gw.call("list_drafts", {"subject": "x"}, retries=2) == {"drafts": []}
        assert no_sleep.calls == [1]

        server.fail_next["create_draft"] = RuntimeError("flaky")
        with pytest.raises(McpCallError):
            await gw.call("create_draft", {"to": "a@example.com", "subject": "s", "body": "b", "html": "h"})
    assert server.drafts == {}
    assert [c["ok"] for c in gw.calls] == [False, True, False]


async def test_read_retries_exhausted_raise():
    def search_documents(query: str) -> dict:
        raise RuntimeError("down")

    server = make_fake_server({"search_documents": search_documents})
    no_sleep.calls = []
    async with gateway(server, sleep=no_sleep) as gw:
        with pytest.raises(McpCallError, match="down"):
            await gw.call("find_doc", {"title": "x"}, retries=2)
    assert no_sleep.calls == [1, 2]
    assert len(gw.calls) == 3


async def test_timeout_raises_mcp_timeout():
    async def search_documents(query: str) -> dict:
        await anyio.sleep(5)
        return {}

    server = make_fake_server({"search_documents": search_documents})
    async with gateway(server, timeout_s=0.1) as gw:
        with pytest.raises(McpTimeout):
            await gw.call("find_doc", {"title": "x"})
    assert gw.calls[-1]["error"] == "McpTimeout"


async def test_audit_log_has_arg_keys_not_values():
    server = make_fake_server()
    secret_body = "BODY-TEXT-THAT-MUST-NOT-BE-LOGGED"
    async with gateway(server) as gw:
        await gw.call("create_doc", {"title": "Groww Weekly Pulse", "body": secret_body})
        with pytest.raises(McpCallError):
            await gw.call("update_doc", {"doc_id": "missing-doc", "body": secret_body})
        with pytest.raises(ToolNotAllowed):
            await gw.call("send_message", {"to": "x@example.com", "subject": "s", "body": secret_body})
    dumped = json.dumps(gw.calls)
    assert secret_body not in dumped and "Groww Weekly Pulse" not in dumped and "missing-doc" not in dumped
    assert gw.calls[0] == {"server": "fake", "action": "create_doc", "tool": "create_document",
                           "arg_keys": ["content", "title"], "ms": gw.calls[0]["ms"], "ok": True, "error": None}
    assert gw.calls[1]["ok"] is False and gw.calls[1]["error"] == "McpCallError"
    assert gw.calls[2]["error"] == "ToolNotAllowed"


async def test_session_closed_on_exception():
    server = make_fake_server()
    inner = fake_session_factory(server)
    state = {}

    @asynccontextmanager
    async def tracking_factory():
        state["open"] = True
        try:
            async with inner() as session:
                yield session
        finally:
            state["open"] = False

    with pytest.raises(RuntimeError, match="boom"):
        async with McpGateway(server_cfg(), "fake", session_factory=tracking_factory):
            assert state["open"] is True
            raise RuntimeError("boom")
    assert state["open"] is False


async def test_http_header_env_missing_raises_config_error(monkeypatch):
    monkeypatch.delenv("FAKE_MCP_TOKEN", raising=False)
    cfg = McpServer(transport="http", url="http://127.0.0.1:9/mcp", header_env={"Authorization": "FAKE_MCP_TOKEN"},
                    tools=TOOLS, allowlist=["find_doc"])
    with pytest.raises(ConfigError, match="FAKE_MCP_TOKEN"):
        async with McpGateway(cfg, "gmail"):
            pass
