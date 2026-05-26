"""web_fetch (spec §6.6). HTTP GET; returns text or error string."""

from __future__ import annotations

import httpx

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


@imp_tool(
    name="web_fetch",
    description=(
        "HTTP GET a URL, return the response body. "
        "Returns 'error: …' on HTTP/network failure."
    ),
)
def web_fetch(ctx: ToolContext, url: str, timeout: float = 30.0) -> str:
    try:
        with httpx.Client() as c:
            r = c.get(url, timeout=timeout)
            if r.status_code >= 400:
                return f"error: HTTP {r.status_code}"
            return r.text
    except httpx.HTTPError as e:
        return f"error: {type(e).__name__}: {e}"
