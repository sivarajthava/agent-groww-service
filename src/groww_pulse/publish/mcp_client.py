"""Our MCP client: the only code that opens MCP sessions. Knows logical actions, not Docs or Gmail (architecture.md §4.9.1).

Real tool and parameter names come from config (`McpServer.tools`). The allowlist is checked before every call,
and the audit log (`calls`) records argument keys only, never values.
"""

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.types import TextContent

from groww_pulse.config import McpServer, ToolSpec

REQUEST_TIMEOUT = 408  # McpError code the SDK uses when no response arrives within read_timeout_seconds


class ConfigError(Exception):
    """MCP configuration can't be used, e.g. an http header's environment variable is not set."""


class CapabilityMissing(Exception):
    """A logical action maps to a tool the server doesn't have, or doesn't cover its required parameters."""

    def __init__(self, action: str, wanted: str | None = None, available: list[str] | None = None,
                 missing_params: list[str] | None = None):
        self.action, self.wanted = action, wanted
        self.available, self.missing_params = available or [], missing_params or []
        if missing_params:
            msg = f"{action}: tool {wanted!r} requires parameters not mapped in config: {missing_params}"
        else:
            msg = f"{action}: server has no tool {wanted!r}; available tools: {self.available}"
        super().__init__(msg)


class ToolNotAllowed(Exception):
    def __init__(self, action: str):
        super().__init__(f"{action}: not in the allowlist")
        self.action = action


class McpCallError(Exception):
    def __init__(self, action: str, message: str):
        super().__init__(f"{action}: {message}")
        self.action = action


class McpTimeout(McpCallError):
    """No response in time. The call may still have taken effect on the server."""


class McpGateway:
    def __init__(self, server: McpServer, name: str, timeout_s: float = 30, session_factory=None, sleep=asyncio.sleep):
        self.server, self.name, self.timeout_s = server, name, timeout_s
        self._session_factory, self._sleep = session_factory, sleep
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self.available_tools: dict[str, dict] = {}
        self.calls: list[dict] = []

    async def __aenter__(self) -> "McpGateway":
        self._stack = AsyncExitStack()
        try:
            if self._session_factory is not None:
                self._session = await self._stack.enter_async_context(self._session_factory())
            else:
                self._session = await self._connect(self._stack)
            self.available_tools = await self._list_tools()
            self._check_capabilities()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def _connect(self, stack: AsyncExitStack) -> ClientSession:
        s = self.server
        if s.transport == "stdio":
            params = StdioServerParameters(command=s.command, args=s.args, env=s.env or None)
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            missing = [var for var in s.header_env.values() if not os.environ.get(var)]
            if missing:
                raise ConfigError(f"{self.name}: environment variable(s) not set for MCP headers: {missing}")
            headers = {h: os.environ[var] for h, var in s.header_env.items()}
            read, write, _ = await stack.enter_async_context(streamablehttp_client(s.url, headers=headers))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _list_tools(self) -> dict[str, dict]:
        tools, cursor = {}, None
        while True:
            page = await self._session.list_tools(cursor) if cursor else await self._session.list_tools()
            tools.update({t.name: t.inputSchema or {} for t in page.tools})
            cursor = page.nextCursor
            if not cursor:
                return tools

    def _check_capabilities(self) -> None:
        for action, spec in self.server.tools.items():
            if spec.name not in self.available_tools:
                raise CapabilityMissing(action, wanted=spec.name, available=sorted(self.available_tools))
            mapped = set(spec.args.values())
            missing = [p for p in self.available_tools[spec.name].get("required", []) if p not in mapped]
            if missing:
                raise CapabilityMissing(action, wanted=spec.name, missing_params=missing)

    async def call(self, action: str, args: dict, retries: int = 0) -> dict:
        """Call a logical action. `retries` is for read actions only; writes are never retried blindly."""
        for attempt in range(retries + 1):
            try:
                return await self._call_once(action, args)
            except McpCallError:
                if attempt == retries:
                    raise
                await self._sleep(2**attempt)  # 1 s, then 2 s

    async def _call_once(self, action: str, args: dict) -> dict:
        if action not in self.server.allowlist or action not in self.server.tools:
            self._log(action, None, sorted(args), 0.0, ToolNotAllowed)
            raise ToolNotAllowed(action)
        spec = self.server.tools[action]
        renamed = {spec.args.get(k, k): v for k, v in args.items()}
        start = time.perf_counter()
        try:
            try:
                result = await self._session.call_tool(
                    spec.name, renamed, read_timeout_seconds=timedelta(seconds=self.timeout_s))
            except McpError as e:
                cls = McpTimeout if e.error.code == REQUEST_TIMEOUT else McpCallError
                raise cls(action, e.error.message) from e
            except TimeoutError as e:
                raise McpTimeout(action, "timed out") from e
            if result.isError:
                raise McpCallError(action, _text(result) or "tool returned an error")
        except McpCallError as e:
            self._log(action, spec.name, sorted(renamed), start, type(e))
            raise
        self._log(action, spec.name, sorted(renamed), start, None)
        return _normalize(_parse(result), spec)

    def _log(self, action: str, tool: str | None, arg_keys: list[str], start: float, error: type | None) -> None:
        ms = round((time.perf_counter() - start) * 1000) if start else 0
        self.calls.append({"server": self.name, "action": action, "tool": tool, "arg_keys": arg_keys,
                           "ms": ms, "ok": error is None, "error": error.__name__ if error else None})


async def find_exact(gw: McpGateway, action: str, args: dict, key: str, value: str) -> dict | None:
    """Run a read action (retried) and return the first result item whose `key` equals `value` exactly.

    Search tools match loosely, so only an exact match counts. Items are the result itself and every dict in
    its list values, e.g. {"documents": [...]}.
    """
    result = await gw.call(action, args, retries=2)
    items = [result] + [i for v in result.values() if isinstance(v, list) for i in v if isinstance(i, dict)]
    return next((i for i in items if i.get(key) == value and i.get("id")), None)


def _text(result) -> str | None:
    return next((c.text for c in result.content if isinstance(c, TextContent)), None)


def _parse(result) -> dict:
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    raw = _text(result) or ""
    try:
        data = json.loads(raw)
    except ValueError:
        return {"text": raw}
    return data if isinstance(data, dict) else {"result": data}


def _normalize(data: dict, spec: ToolSpec) -> dict:
    """Add our keys (id, url, ...) from the server's keys per `ToolSpec.result`, at the top level and in list items."""
    if not spec.result:
        return data

    def norm(d: dict) -> dict:
        d = dict(d)
        for ours, theirs in spec.result.items():
            if theirs in d:
                d.setdefault(ours, d[theirs])
        return d

    out = norm(data)
    for k, v in out.items():
        if isinstance(v, list):
            out[k] = [norm(i) if isinstance(i, dict) else i for i in v]
    return out
