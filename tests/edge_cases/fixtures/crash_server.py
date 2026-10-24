"""A stdio MCP server shaped like the Docs server that dies when asked to create a document (edge case E28)."""

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("crashing-docs", log_level="WARNING")


@server.tool()
def search_documents(query: str) -> dict:
    return {"documents": []}


@server.tool()
def create_document(title: str, content: str) -> dict:
    os._exit(1)  # the process disappears mid-call, as a crashed server would


@server.tool()
def update_document(documentId: str, content: str) -> dict:
    return {"documentId": documentId}


if __name__ == "__main__":
    server.run("stdio")
