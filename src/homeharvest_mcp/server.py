"""MCP server: describe / query / health over stdio or streamable HTTP (Smithery-compatible)."""

from __future__ import annotations

import argparse
import json
import os
import threading
from typing import Annotated

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from smithery.decorators import smithery

from .describe import describe_text
from .engine import Engine, Settings

INSTRUCTIONS = (
    "US property listings (for sale, pending, sold, rentals) as SQL tables. Call describe() once, then "
    "query() with a location filter. Aggregate in SQL to keep results small."
)


class ConfigSchema(BaseModel):
    proxy_url: str | None = Field(None, description="HTTP(S) proxy for Realtor.com requests, e.g. http://user:pass@host:port")
    max_pages: int = Field(25, ge=1, le=50, description="Max 200-row pages fetched from the source per query call")
    cache_ttl_hours: float = Field(24.0, gt=0, le=24 * 30, description="Hours to reuse fetched rows before refetching")


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


def settings_from_ctx(ctx: Context | None) -> Settings:
    """Session config (Smithery query params) wins over env vars; stdio uses env vars."""
    cfg = None
    try:
        request = ctx.request_context.request if ctx is not None else None
        if request is not None:
            cfg = request.scope.get("session_config")
            if cfg is None:
                # Plain HTTP without the Smithery middleware: read ?proxy_url=... directly.
                q = request.query_params
                cfg = ConfigSchema(**{k: q[k] for k in ConfigSchema.model_fields if k in q})
    except Exception:  # noqa: BLE001 - no request (stdio) or bad params
        cfg = None
    overrides = {}
    if cfg is not None:
        overrides = {
            "proxy": getattr(cfg, "proxy_url", None),
            "max_pages": getattr(cfg, "max_pages", None),
            "ttl_hours": getattr(cfg, "cache_ttl_hours", None),
        }
    return Settings.from_env(**overrides)


def build_server(host: str = "0.0.0.0", port: int = 8081) -> FastMCP:
    """host: the bind address. Loopback keeps the SDK's DNS-rebinding guard (Host must be
    localhost); any other bind (hosted, e.g. Smithery behind a proxy) turns it off so the
    public Host header is accepted."""
    server = FastMCP("homeharvest", instructions=INSTRUCTIONS, host=host, port=port)
    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)

    @server.tool(
        annotations=ro,
        description=(
            "Schema for the property tables (properties, agents, offices, open_houses, price_history, tax_history): "
            "columns, types, fill %, which WHERE filters are sent to the source and their cost, and example queries. "
            "Call first. Pass table for column descriptions."
        ),
    )
    def describe(
        table: Annotated[str | None, Field(description="One table name, or omit for all tables")] = None,
        ctx: Context = None,
    ) -> str:
        s = settings_from_ctx(ctx)
        return describe_text(table, max_pages=s.max_pages, ttl=s.ttl_hours, max_history=s.max_history)

    @server.tool(
        annotations=ro,
        description=(
            "Run one read-only SELECT (DuckDB SQL) over US property listings. Needs a location: "
            "WHERE location = 'Phoenix, AZ' (or zip = '85004', or city + state). Filters on status, sold_date/list_date, "
            "beds, sqft, list_price, year_built, property_type are sent to the source; the rest runs locally. "
            "Rows are cached 24h. Returns NDJSON rows, then a metadata line "
            '{"truncated","total_rows","fetched_pages","cache","complete"}. Prefer COUNT/AVG/MEDIAN/GROUP BY.'
        ),
    )
    async def query(
        sql: Annotated[str, Field(description="One SELECT statement")],
        max_rows: Annotated[int, Field(description="Rows to return (1-1000)", ge=1, le=1000)] = 50,
        ctx: Context = None,
    ) -> str:
        settings = settings_from_ctx(ctx)
        engine = get_engine()
        return await anyio.to_thread.run_sync(lambda: engine.query(sql, max_rows=max_rows, settings=settings))

    @server.tool(annotations=ro, description="Server and HomeHarvest versions, cache size, and fetch settings.")
    async def health(ctx: Context = None) -> str:
        settings = settings_from_ctx(ctx)
        engine = get_engine()
        info = await anyio.to_thread.run_sync(lambda: engine.health(settings))
        return json.dumps(info, separators=(",", ":"))

    return server


@smithery.server(config_schema=ConfigSchema)
def create_server():
    """Smithery entry point ([tool.smithery] in pyproject.toml). The config schema is passed
    here, which is what makes ?proxy_url=... reach the tools (v1 left it out)."""
    return build_server()


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(prog="homeharvest-mcp", description="HomeHarvest MCP server: US property data via SQL")
    p.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("MCP_TRANSPORT", "stdio"))
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8081")))
    args = p.parse_args(argv)
    if args.transport == "stdio":
        build_server().run("stdio")
        return
    import uvicorn

    # Same Smithery wrapper as hosted deploys: ?proxy_url=...&max_pages=... become session config, plus CORS.
    from smithery.server.fastmcp_patch import from_fastmcp

    app = from_fastmcp(build_server(host=args.host, port=args.port), config_schema=ConfigSchema).streamable_http_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
