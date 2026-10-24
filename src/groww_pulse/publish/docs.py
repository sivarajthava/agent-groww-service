"""Publish the validated pulse to Google Docs via MCP, idempotently: one Doc per ISO week (architecture.md §4.9.2)."""

from groww_pulse.publish.mcp_client import McpCallError, McpGateway, find_exact


def doc_title(week: str) -> str:
    return f"Groww Weekly Pulse — {week}"


def _url(item: dict, doc_id: str) -> str:
    return item.get("url") or f"https://docs.google.com/document/d/{doc_id}/edit"


async def publish_doc(gw: McpGateway, week: str, markdown: str) -> tuple[str, str]:
    """Update this week's Doc, or create it. `markdown` is the validated pulse.md read from disk.

    A failed create is never retried: find runs again, and a Doc that exists now (the create went through but
    its response was lost) is used instead of making a second one.
    """
    title = doc_title(week)
    found = await find_exact(gw, "find_doc", {"title": title}, "title", title)
    if found:
        res = await gw.call("update_doc", {"doc_id": found["id"], "body": markdown})
        return found["id"], _url({**res, **found}, found["id"])
    try:
        res = await gw.call("create_doc", {"title": title, "body": markdown})
    except McpCallError:
        found = await find_exact(gw, "find_doc", {"title": title}, "title", title)
        if not found:
            raise
        return found["id"], _url(found, found["id"])
    if not res.get("id"):
        raise McpCallError("create_doc", "result has no document id; check the result map in config.yaml")
    return res["id"], _url(res, res["id"])
