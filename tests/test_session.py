"""Tests for session persistence and manual auth."""

from pathlib import Path

import httpx

from tripletex.auth.manual import create_manual_session
from tripletex.session import ApiSession, WebSession


class TestManualSession:
    def test_create_from_cookie_string(self):
        session = create_manual_session(
            cookie="JSESSIONID=abc123; CSRFTokenWriteOnly=xyz789; other=val",
            context_id="32611682",
            csrf_token="deadbeef",
        )
        assert session.context_id == "32611682"
        assert session.csrf_token == "deadbeef"
        assert session.cookies.get("JSESSIONID") == "abc123"
        assert session.cookies.get("CSRFTokenWriteOnly") == "xyz789"


class TestWebSessionPersistence:
    def test_save_and_load(self, tmp_path: Path):
        cookies = httpx.Cookies()
        cookies.set("JSESSIONID", "test123")
        cookies.set("CSRFTokenWriteOnly", "token456")

        session = WebSession(
            cookies=cookies,
            csrf_token="csrf_abc",
            context_id="12345",
        )

        path = tmp_path / "session.json"
        session.save(path)

        loaded = WebSession.load(path)
        assert loaded is not None
        assert loaded.csrf_token == "csrf_abc"
        assert loaded.context_id == "12345"
        assert loaded.cookies.get("JSESSIONID") == "test123"

    def test_load_missing_file(self, tmp_path: Path):
        assert WebSession.load(tmp_path / "nonexistent.json") is None

    def test_load_corrupt_file(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("not json")
        assert WebSession.load(path) is None


class TestWebSessionHeaders:
    def test_json_headers(self):
        session = WebSession(
            cookies=httpx.Cookies(),
            csrf_token="abc",
            context_id="123",
        )
        headers = session.request_headers(for_json=True)
        assert headers["Accept"] == "application/json; charset=utf-8"
        assert headers["x-tlx-context-id"] == "123"
        assert headers["x-tlx-csrf-token"] == "abc"
        assert session.request_auth() is None
        assert session.request_cookies() is not None

    def test_html_headers(self):
        session = WebSession(
            cookies=httpx.Cookies(),
            csrf_token="abc",
            context_id="123",
        )
        headers = session.request_headers(for_json=False)
        assert headers["Accept"] == "*/*"
        assert "X-Requested-With" in headers


class TestApiSession:
    def test_request_headers(self):
        session = ApiSession(session_token="tok123", company_id=0)
        headers = session.request_headers(for_json=True)
        assert headers["Accept"] == "application/json; charset=utf-8"
        assert "x-tlx-context-id" not in headers
        assert "x-tlx-csrf-token" not in headers

    def test_basic_auth(self):
        session = ApiSession(session_token="tok123", company_id=0)
        auth = session.request_auth()
        assert isinstance(auth, httpx.BasicAuth)

    def test_no_cookies(self):
        session = ApiSession(session_token="tok123")
        assert session.request_cookies() is None

    def test_custom_company_id(self):
        session = ApiSession(session_token="tok123", company_id=42)
        assert session.company_id == 42
