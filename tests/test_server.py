"""MCP surface: tool list, describe text, and Smithery config reaching the fetcher. No network."""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from homeharvest_mcp import server as srv
from homeharvest_mcp.describe import FOOTER
from homeharvest_mcp.engine import Engine
from homeharvest_mcp.store import Store

from test_engine_offline import FakeSource, home


@pytest.fixture
def fake(tmp_path, monkeypatch):
    src = FakeSource({("tempe, az", "for_sale"): [home(i) for i in range(1, 6)]})
    eng = Engine(store=Store(tmp_path / "c.duckdb"), source_factory=src)
    monkeypatch.setattr(srv, "_engine", eng)
    monkeypatch.delenv("HOMEHARVEST_PROXY", raising=False)
    return src


def test_tools_listed():
    tools = asyncio.run(srv.build_server().list_tools())
    names = sorted(t.name for t in tools)
    assert names == ["describe", "health", "query"]
    for t in tools:
        assert t.annotations.readOnlyHint is True
        assert len(t.description) < 600


def test_describe_footer_and_content():
    text = srv.describe_text()
    assert text.rstrip().endswith(FOOTER)
    assert text.count("modernrealty.io") == 1
    for word in ("properties", "agents", "offices", "open_houses", "price_history", "tax_history", "pushdown", "examples"):
        assert word in text
    one = srv.describe_text("properties")
    assert "sold_date" in one and one.rstrip().endswith(FOOTER)
    assert "unknown table" in srv.describe_text("nope")


def test_describe_size_is_small():
    assert len(srv.describe_text()) / 4 < 3000  # tokens, roughly


def _rpc(client, method, params=None, id_=1, session=None, qs=""):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if session:
        headers["mcp-session-id"] = session
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    r = client.post("/mcp" + qs, headers=headers, content=json.dumps(body))
    assert r.status_code in (200, 202), r.text
    data = None
    for line in r.text.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:])
    if data is None and r.text.strip().startswith("{"):
        data = r.json()
    return r, data


def test_smithery_config_reaches_fetcher(fake):
    app = srv.create_server().streamable_http_app()
    qs = "?proxy_url=http%3A%2F%2Fuser%3Apw%40proxy%3A8080&max_pages=3"
    with TestClient(app) as client:
        r, init = _rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}, qs=qs)
        session = r.headers.get("mcp-session-id")
        _rpc(client, "notifications/initialized", id_=None, session=session, qs=qs)
        _, res = _rpc(client, "tools/call", {"name": "query", "arguments": {
            "sql": "SELECT count(*) n FROM properties WHERE location = 'Tempe, AZ' AND status = 'for_sale'"}},
            id_=2, session=session, qs=qs)
        text = res["result"]["content"][0]["text"]
        assert json.loads(text.splitlines()[0]) == {"n": 5}
        assert fake.proxy == "http://user:pw@proxy:8080"
        _, res = _rpc(client, "tools/call", {"name": "health", "arguments": {}}, id_=3, session=session, qs=qs)
        h = json.loads(res["result"]["content"][0]["text"])
        assert h["settings"]["proxy_configured"] is True and h["settings"]["max_pages_per_call"] == 3
        cfg = client.get("/.well-known/mcp-config")
        assert cfg.status_code == 200 and "proxy_url" in cfg.text


def test_env_proxy_for_stdio(fake, monkeypatch):
    monkeypatch.setenv("HOMEHARVEST_PROXY", "http://env:1")
    s = srv.settings_from_ctx(None)
    assert s.proxy == "http://env:1"
