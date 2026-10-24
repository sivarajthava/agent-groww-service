"""An in-memory MCP server shaped like a Google Docs + Gmail server, for tests and the eval harness.

State lives on the returned server object: `docs`, `drafts`, `sent`. Test switches:
- `fail_next[tool] = exc`: the next call of that tool raises `exc` (the client sees an isError result).
- `lose_response_of = {tool}`: the tool performs its write, then fails as if the response was lost.
- `fuzzy_search = True`: searches return every doc/draft, like a real full-text search with loose matches.
"""

from collections.abc import Callable
from itertools import count
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session


def make_fake_server(tools: dict[str, Callable] | None = None) -> FastMCP:
    server = FastMCP("fake-google", log_level="WARNING")
    server.docs, server.drafts, server.sent = {}, {}, []
    server.fail_next, server.lose_response_of, server.fuzzy_search = {}, set(), False
    doc_ids, draft_ids = count(1), count(1)  # never reused, like real ids, even after a delete

    def guard(tool: str) -> None:
        if tool in server.fail_next:
            raise server.fail_next.pop(tool)

    def lost(tool: str) -> None:
        if tool in server.lose_response_of:
            raise TimeoutError(f"{tool}: response lost")

    def search_documents(query: str) -> dict:
        guard("search_documents")
        docs = [{"documentId": i, "title": d["title"], "url": d["url"]}
                for i, d in server.docs.items() if server.fuzzy_search or query.lower() in d["title"].lower()]
        return {"documents": docs}

    def create_document(title: str, content: str) -> dict:
        guard("create_document")
        doc_id = f"doc{next(doc_ids)}"
        url = f"https://docs.example.com/d/{doc_id}"
        server.docs[doc_id] = {"title": title, "content": content, "url": url}
        lost("create_document")
        return {"documentId": doc_id, "url": url}

    def update_document(documentId: str, content: str) -> dict:
        guard("update_document")
        if documentId not in server.docs:
            raise ValueError("no such document")
        server.docs[documentId]["content"] = content
        return {"documentId": documentId, "url": server.docs[documentId]["url"]}

    def list_drafts(query: str) -> dict:
        guard("list_drafts")
        drafts = [{"draftId": i, "subject": d["subject"], **({"date": d["date"]} if "date" in d else {})}
                  for i, d in server.drafts.items() if server.fuzzy_search or query.lower() in d["subject"].lower()]
        return {"drafts": drafts}

    def create_draft(to: str, subject: str, body: str, htmlBody: str) -> dict:
        guard("create_draft")
        draft_id = f"draft{next(draft_ids)}"
        server.drafts[draft_id] = {"to": to, "subject": subject, "body": body, "htmlBody": htmlBody}
        lost("create_draft")
        return {"draftId": draft_id}

    def update_draft(draftId: str, body: str, htmlBody: str) -> dict:
        guard("update_draft")
        if draftId not in server.drafts:
            raise ValueError("no such draft")
        server.drafts[draftId].update(body=body, htmlBody=htmlBody)
        return {"draftId": draftId}

    def send_message(to: str, subject: str, body: str) -> dict:
        server.sent.append({"to": to, "subject": subject, "body": body})
        return {"sent": True}

    defaults = {f.__name__: f for f in (search_documents, create_document, update_document, list_drafts,
                                        create_draft, update_draft, send_message)}
    for name, fn in {**defaults, **(tools or {})}.items():
        server.add_tool(fn, name=name)
    return server


def fake_session_factory(server: FastMCP):
    """A McpGateway session_factory: an async context manager yielding an initialized ClientSession."""

    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(server) as session:
            yield session

    return factory
