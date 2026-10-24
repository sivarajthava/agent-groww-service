"""Create or update this week's Gmail draft via MCP. Draft-only: sending is never in the allowlist."""

import html as html_lib
import logging

from groww_pulse.publish.mcp_client import McpCallError, McpGateway, find_all_exact

log = logging.getLogger(__name__)
DATE_KEYS = ("updated", "updatedAt", "modifiedTime", "internalDate", "date", "createdTime")


async def _find_draft(gw: McpGateway, subject: str) -> dict | None:
    """The draft with this exact subject. With several, the newest (by a date field, else the server's first,
    as list APIs return newest first), and a warning naming them all; never a reason to create another."""
    matches = await find_all_exact(gw, "list_drafts", {"subject": subject}, "subject", subject)
    if len(matches) > 1:
        dated = [m for m in matches if any(m.get(k) for k in DATE_KEYS)]
        if dated:
            matches = sorted(dated, key=lambda m: str(next(m[k] for k in DATE_KEYS if m.get(k))), reverse=True)
        log.warning("%d drafts share the subject %r: %s; updating %s", len(matches), subject,
                    ", ".join(str(m["id"]) for m in matches), matches[0]["id"])
    return matches[0] if matches else None


def email_subject(week: str) -> str:
    return f"[Groww Pulse] {week} — weekly review pulse"


async def upsert_draft(
    gw: McpGateway, week: str, to: str, markdown: str, html: str, doc_url: str | None
) -> str:
    """Update the draft whose subject is this week's, or create it. Returns the draft id.

    The body is the Doc link (when there is one), then the full pulse, so the email works without the Doc.
    `markdown`/`html` come from the validated pulse.md on disk. A failed create re-runs find, never create.
    """
    subject = email_subject(week)
    body, html_body = markdown, html
    if doc_url:
        link = html_lib.escape(doc_url)
        body = f"Doc: {doc_url}\n\n{markdown}"
        html_body = f'<p>Doc: <a href="{link}">{link}</a></p>{html}'

    found = await _find_draft(gw, subject)
    if found:
        await gw.call("update_draft", {"draft_id": found["id"], "body": body, "html": html_body})
        return found["id"]
    try:
        res = await gw.call("create_draft", {"to": to, "subject": subject, "body": body, "html": html_body})
    except McpCallError:
        found = await _find_draft(gw, subject)
        if not found:
            raise
        return found["id"]
    if not res.get("id"):
        raise McpCallError("create_draft", "result has no draft id; check the result map in config.yaml")
    return res["id"]
