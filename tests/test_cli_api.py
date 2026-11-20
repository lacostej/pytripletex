"""Tests for `tripletex api`, the raw request pass-through."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from tripletex.cli import main
from tripletex.client import TripletexClient
from tripletex.session import WebSession


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Routes requests to `server.handler`, recording each one in `server.seen`."""

    class Server:
        seen: list[httpx.Request] = []

        @staticmethod
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"value": {"id": 1}})

    def transport(request: httpx.Request) -> httpx.Response:
        Server.seen.append(request)
        return Server.handler(request)

    class _Authenticated(TripletexClient):
        async def authenticate(self, force: bool = False) -> None:
            cookies = httpx.Cookies()
            cookies.set("JSESSIONID", "sess", domain="tripletex.no", path="/")
            self._session = WebSession(cookies=cookies, context_id="1")

    def make_client(ctx):
        client = _Authenticated(ctx.obj["config"])
        client._http = httpx.AsyncClient(
            base_url=client.config.base_url, transport=httpx.MockTransport(transport)
        )
        return client

    monkeypatch.setattr(main, "_make_client", make_client)
    Server.config = str(tmp_path / "absent.toml")
    return Server


def _run(server, *args):
    return CliRunner().invoke(main.cli, ["--config", server.config, "api", *args])


def test_get_prints_json_only(server):
    result = _run(server, "get", "/v2/employee/1?fields=id")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"value": {"id": 1}}
    request = server.seen[0]
    assert request.method == "GET"
    assert request.url.path == "/v2/employee/1"
    assert request.url.params["fields"] == "id"


def test_paginate_walks_every_page(server):
    rows = [{"id": i} for i in range(2500)]

    def handler(request):
        start, count = int(request.url.params["from"]), int(request.url.params["count"])
        return httpx.Response(200, json={"fullResultSize": 1, "values": rows[start : start + count]})

    server.handler = handler
    result = _run(server, "GET", "/v2/customer?isInactive=false", "--paginate")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"count": 2500, "values": rows}
    assert [r.url.params["from"] for r in server.seen] == ["0", "1000", "2000"]
    assert all(r.url.params["isInactive"] == "false" for r in server.seen)


def test_paginate_refuses_a_non_list_endpoint(server):
    result = _run(server, "GET", "/v2/employee/1", "--paginate")

    assert result.exit_code == 2
    assert "not a list endpoint" in result.stderr
    assert result.stdout == ""


def test_paginate_refuses_from_and_count(server):
    result = _run(server, "GET", "/v2/customer?count=10", "--paginate")

    assert result.exit_code == 2
    assert server.seen == []


def test_put_sends_the_body_from_a_file(server, tmp_path):
    body = tmp_path / "customer.json"
    body.write_text('{"name": "Example AS"}')

    result = _run(server, "PUT", "/v2/customer/5", "--body", f"@{body}", "--yes")

    assert result.exit_code == 0, result.output
    request = server.seen[0]
    assert request.method == "PUT"
    assert json.loads(request.content) == {"name": "Example AS"}
    assert request.headers["content-type"] == "application/json"


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_writes_need_yes(server, method):
    result = _run(server, method, "/v2/customer/5")

    assert result.exit_code == 2
    assert "--yes" in result.stderr
    assert server.seen == []


def test_delete_with_no_content_prints_nothing(server):
    server.handler = lambda request: httpx.Response(204)

    result = _run(server, "DELETE", "/v2/customer/5", "--yes")

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert server.seen[0].method == "DELETE"


@pytest.mark.parametrize(
    "path",
    ["https://example.com/v2/employee", "//example.com/v2/employee", "v2/employee"],
)
def test_refuses_anything_but_a_path(server, path):
    result = _run(server, "GET", path)

    assert result.exit_code == 2
    assert server.seen == []


def test_error_shows_tripletex_messages_on_stderr(server):
    server.handler = lambda request: httpx.Response(
        422,
        json={
            "status": 422,
            "message": "Validation failed.",
            "validationMessages": [{"field": "name", "message": "Must be set."}],
            "requestId": "req-1",
        },
    )

    result = _run(server, "POST", "/v2/customer", "--body", "{}", "--yes")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "422: Validation failed." in result.stderr
    assert "name: Must be set." in result.stderr
    assert "requestId: req-1" in result.stderr


def test_non_json_response_is_an_error(server):
    server.handler = lambda request: httpx.Response(
        200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
    )

    result = _run(server, "GET", "/v2/document/1/content")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "application/pdf" in result.stderr


def test_invalid_body(server):
    result = _run(server, "POST", "/v2/customer", "--body", "{nope", "--yes")

    assert result.exit_code == 2
    assert "not valid JSON" in result.stderr
    assert server.seen == []
