"""Trusted-device and persistent-session options on the Visma Connect login.

The MFA page below is the real one, trimmed — captured from
`connect.visma.com/totp` on 2026-09-03. The detail that matters is the pair
Visma emits for the checkbox:

    <input type="checkbox" name="RememberCode" value="true">
    ...
    <input name="RememberCode" type="hidden" value="false">

A browser posting a ticked box sends both, `true` first, and the ASP.NET model
binder takes the first. Flattened into a dict the hidden `false` wins instead,
which is what the library did for its whole life — silently declining the
30-day device trust on every login.
"""

from __future__ import annotations

import httpx
import pytest

from tripletex.auth.visma_connect import (
    LoginState,
    _checkbox_pair,
    _encode_form,
    _tickable,
    _trusted_device_fields,
    _get_forms,
)

MFA_HTML = """
<form class="form-horizontal" role="form" action="/totp/auth" method="post">
  <input type="tel" maxlength="6" id="AuthCode" name="AuthCode" value="">
  <input type="submit" id="ButtonSubmit" value="Verify">
  <div class="checkbox">
    <label for="RememberCode">
      <input type="checkbox" data-val="true" id="RememberCode"
             name="RememberCode" value="true"> Remember this device for 30 days
    </label>
  </div>
  <input type="hidden" id="DisableRememberDevice" value="False"
         data-val="true" name="DisableRememberDevice">
  <input type="hidden" id="ClientId" name="ClientId" value="tripletex">
  <input type="hidden" id="ReturnUrl" name="ReturnUrl" value="/connect/authorize/callback">
  <input name="__RequestVerificationToken" type="hidden" value="CfDJ8O4uQy5P">
  <input name="RememberCode" type="hidden" value="false">
</form>
"""


def _state(html: str = MFA_HTML, trust: bool = True) -> LoginState:
    (_, _, data), = _get_forms(html)
    return LoginState(
        cookies=httpx.Cookies(),
        visma_base="https://connect.visma.com",
        mfa_form_action="/totp/auth",
        mfa_form_data=data,
        mfa_field_name="AuthCode",
        base_url="https://tripletex.no",
        mfa_html=html,
        trust_device=trust,
    )


class TestFormFlattening:
    def test_hidden_partner_wins_when_flattened(self):
        """Documents the bug the rest of this file exists to work around."""
        (_, _, data), = _get_forms(MFA_HTML)
        assert data["RememberCode"] == "false"

    def test_checkbox_pair_finds_both_elements(self):
        on_value, has_hidden = _checkbox_pair(MFA_HTML, "RememberCode")
        assert on_value == "true"
        assert has_hidden is True

    def test_absent_checkbox_reports_none(self):
        assert _checkbox_pair(MFA_HTML, "RememberMachine") == (None, False)

    def test_checkbox_without_a_value_defaults_to_on(self):
        """`on` is the HTML default when a checkbox carries no value."""
        html = '<form><input type="checkbox" name="RememberMe"></form>'
        assert _checkbox_pair(html, "RememberMe") == ("on", False)


class TestEncoding:
    def test_ticked_box_posts_true_then_false(self):
        """Exactly what the browser sends. The binder takes the first."""
        (_, _, data), = _get_forms(MFA_HTML)
        encoded = _encode_form(data, {"RememberCode": ("true", True)})

        assert encoded["RememberCode"] == ["true", "false"]

    def test_encoding_is_a_dict_not_a_list_of_pairs(self):
        """httpx reads a list passed to `data=` as raw content and builds a sync
        byte stream from it, which fails on an AsyncClient. A list *value* is the
        supported way to repeat a key."""
        (_, _, data), = _get_forms(MFA_HTML)
        encoded = _encode_form(data, {"RememberCode": ("true", True)})

        assert isinstance(encoded, dict)

    def test_box_without_a_hidden_partner_posts_one_value(self):
        html = '<form><input type="checkbox" name="RememberMe" value="true"></form>'
        (_, _, data), = _get_forms(html)
        encoded = _encode_form(data, {"RememberMe": ("true", False)})

        assert encoded["RememberMe"] == "true"

    def test_untouched_fields_are_preserved(self):
        (_, _, data), = _get_forms(MFA_HTML)
        encoded = _encode_form(data, {"RememberCode": ("true", True)})

        assert encoded["ClientId"] == "tripletex"
        assert encoded["__RequestVerificationToken"] == "CfDJ8O4uQy5P"
        assert encoded["AuthCode"] == ""

    def test_nothing_ticked_leaves_the_form_alone(self):
        (_, _, data), = _get_forms(MFA_HTML)
        assert _encode_form(data, {}) == data


class TestTrustDeviceDecision:
    def test_real_form_offers_remembercode(self):
        assert _trusted_device_fields(_state()) == {"RememberCode": ("true", True)}

    def test_disabled_when_the_option_is_off(self):
        assert _trusted_device_fields(_state(trust=False)) == {}

    def test_server_opt_out_is_honoured(self):
        """`DisableRememberDevice: True` means the checkbox is decorative."""
        html = MFA_HTML.replace('id="DisableRememberDevice" value="False"',
                                'id="DisableRememberDevice" value="True"')
        assert _trusted_device_fields(_state(html)) == {}

    def test_missing_checkbox_is_not_fatal(self):
        """A login that fails because a checkbox moved would be worse than one
        that still asks for a code."""
        html = MFA_HTML.replace('type="checkbox"', 'type="text"')
        assert _trusted_device_fields(_state(html)) == {}

    def test_alternate_spelling_is_accepted(self):
        html = MFA_HTML.replace("RememberCode", "RememberMachine")
        assert _trusted_device_fields(_state(html)) == {"RememberMachine": ("true", True)}


class TestTickable:
    def test_only_fields_present_in_the_form_are_offered(self):
        data = {"RememberMe": "false"}
        html = '<form><input type="checkbox" name="RememberMe" value="true"></form>'
        assert _tickable(html, data, ("RememberLogin", "RememberMe")) == {
            "RememberMe": ("true", False)
        }

    def test_a_name_in_data_but_not_a_checkbox_is_skipped(self):
        """`DisableRememberDevice` is a hidden field, not something to tick."""
        (_, _, data), = _get_forms(MFA_HTML)
        assert _tickable(MFA_HTML, data, ("DisableRememberDevice",)) == {}

    def test_stops_at_the_first_match(self):
        html = (
            '<form><input type="checkbox" name="RememberMe" value="true">'
            '<input type="checkbox" name="RememberLogin" value="true"></form>'
        )
        data = {"RememberMe": "", "RememberLogin": ""}
        assert len(_tickable(html, data, ("RememberMe", "RememberLogin"))) == 1


class TestPriorCookiesCarryOver:
    async def test_prior_jar_is_seeded_into_the_login(self, monkeypatch):
        """Without this the login starts from an empty jar, Visma sees an
        unknown browser, and `trust_device` can never pay off."""
        from tripletex.auth import visma_connect
        from tripletex.config import TripletexConfig

        seen: dict = {}

        async def fake_phase1(config, http, prior_cookies=None):
            seen["names"] = [c.name for c in prior_cookies.jar] if prior_cookies else []
            from tripletex.session import WebSession
            return WebSession(cookies=httpx.Cookies(), context_id="1")

        monkeypatch.setattr(visma_connect, "_do_login_phase1", fake_phase1)

        jar = httpx.Cookies()
        jar.set("TrustedDevice", "abc", domain="connect.visma.com")

        await visma_connect.visma_connect_login(
            TripletexConfig(username="u", password_visma="p"),
            prior_cookies=jar,
        )

        assert seen["names"] == ["TrustedDevice"]


class TestCookieDeadlines:
    """Reading whether the option took, without waiting 30 days to find out."""

    def _session(self, **cookies):
        import time
        from tripletex.session import WebSession

        jar = httpx.Cookies()
        for name, days in cookies.items():
            if days is None:
                jar.set(name, "v", domain="tripletex.no")
            else:
                jar.jar.set_cookie(
                    __import__("http.cookiejar", fromlist=["Cookie"]).Cookie(
                        version=0, name=name, value="v", port=None,
                        port_specified=False, domain="tripletex.no",
                        domain_specified=False, domain_initial_dot=False,
                        path="/", path_specified=True, secure=True,
                        expires=int(time.time() + days * 86400),
                        discard=False, comment=None, comment_url=None, rest={},
                    )
                )
        return WebSession(cookies=jar, context_id="1")

    def test_longest_wins_over_the_rotating_csrf_token(self):
        """The bug ops-monitor hit: CSRFTokenWriteOnly rotates per request and
        expires in an hour, so the soonest deadline calls a good 30-day login a
        failure."""
        session = self._session(CSRFTokenWriteOnly=0.04, VismaAuth=30)
        name, _ = session.longest_lived_cookie()

        assert name == "VismaAuth"

    def test_short_deadline_means_the_option_did_not_take(self):
        session = self._session(CSRFTokenWriteOnly=0.04, VismaAuth=0.5)
        _, when = session.longest_lived_cookie()

        from datetime import datetime, timezone
        assert (when - datetime.now(timezone.utc)).days < 1

    def test_no_stamped_deadline_is_an_idle_timeout_not_a_short_session(self):
        """Distinct from a short deadline: nothing to lengthen, so the fix is a
        keepalive rather than a login option."""
        session = self._session(SessionOnly=None)

        assert session.cookie_expiries() == {}
        assert session.longest_lived_cookie() is None

    def test_session_cookies_are_omitted_not_reported_as_eternal(self):
        session = self._session(SessionOnly=None, VismaAuth=30)

        assert list(session.cookie_expiries()) == ["VismaAuth"]


class TestDurableCookieSeeding:
    """What may be carried from a dead session into a fresh login.

    Seeding the whole jar broke a real login. The stored jar held
    `.AspNetCore.Antiforgery.D5MU2Fjo4Ro` from the previous session; ASP.NET
    validates the form's freshly-issued `__RequestVerificationToken` against
    that cookie, so a stale one fails the check and the server silently
    re-renders the login page. The symptom was "Could not find password form"
    with the email form still on it.
    """

    def _jar(self, *specs):
        """specs are (name, days_until_expiry | None)."""
        import http.cookiejar
        import time

        jar = httpx.Cookies()
        for name, days in specs:
            jar.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value="v", port=None, port_specified=False,
                domain=".connect.visma.com", domain_specified=True,
                domain_initial_dot=True, path="/", path_specified=True,
                secure=True,
                expires=None if days is None else int(time.time() + days * 86400),
                discard=days is None, comment=None, comment_url=None, rest={},
            ))
        return jar

    def _seed(self, jar):
        from tripletex.auth.visma_connect import _seed_durable_cookies
        target = httpx.Cookies()
        count = _seed_durable_cookies(target, jar)
        return target, count

    def test_stale_antiforgery_cookie_is_never_replayed(self):
        """The cookie that broke the live login the first time."""
        jar = self._jar((".AspNetCore.Antiforgery.D5MU2Fjo4Ro", 30))
        target, count = self._seed(jar)

        assert count == 0
        assert list(target.jar) == []

    def test_only_the_device_grant_is_carried(self):
        """The second live failure: a denylist let eight cookies through —
        AWS load-balancer state, isTripletexUser, rememberUsername, Culture —
        and the email step bounced straight back to the login page."""
        jar = self._jar(
            ("AWSALB", 7), ("AWSALBCORS", 7), ("AWSALBTG", 7), ("AWSALBTGCORS", 7),
            ("isTripletexUser", 60), ("rememberUsername", 365),
            (".AspNetCore.Culture", 365), ("remember2sv", 30),
        )
        target, count = self._seed(jar)

        assert count == 1
        assert [c.name for c in target.jar] == ["remember2sv"]

    def test_session_cookies_are_dropped(self):
        """No expiry means per-browser-session state the server will reissue."""
        jar = self._jar(("tempSession", None), ("sid", None), ("remember2sv", None))
        _, count = self._seed(jar)

        assert count == 0

    def test_a_real_jar_before_any_trust_carries_nothing(self):
        """Measured: all 16 cookies in the stored session were session-scoped, so
        the login must be identical to one with no stored session at all."""
        jar = self._jar(
            ("JSESSIONID", None), ("CSRFTokenWriteOnly", None),
            (".AspNetCore.Antiforgery.D5MU2Fjo4Ro", None), ("tempSession", None),
            ("returnUrl", None), ("remember2sv", None), ("session", None),
            ("sid", None), ("rememberUsername", None),
        )
        _, count = self._seed(jar)

        assert count == 0

    def test_a_durable_trust_cookie_is_carried(self):
        jar = self._jar(("remember2sv", 30), ("tempSession", None))
        target, count = self._seed(jar)

        assert count == 1
        assert [c.name for c in target.jar] == ["remember2sv"]

    def test_expired_durable_cookie_is_dropped(self):
        jar = self._jar(("remember2sv", -1))
        _, count = self._seed(jar)

        assert count == 0


class TestSeedingIsGatedOnTrustDevice:
    async def test_no_seeding_when_trust_device_is_off(self, monkeypatch):
        """Seeding unconditionally changed the default login path for everyone
        and broke it. With the option off, the login must start from an empty
        jar exactly as before."""
        from tripletex.auth import visma_connect
        from tripletex.config import TripletexConfig

        seen: dict = {}

        async def fake_forms(http, url, cookies):
            seen["names"] = [c.name for c in cookies.jar]
            raise RuntimeError("stop here")

        monkeypatch.setattr(visma_connect, "_follow_redirects", fake_forms)

        jar = httpx.Cookies()
        jar.set("remember2sv", "abc", domain="connect.visma.com")

        config = TripletexConfig(username="u", password_visma="p", trust_device=False)
        with pytest.raises(RuntimeError):
            await visma_connect._do_login_phase1(config, httpx.AsyncClient(), jar)

        assert seen["names"] == []


class TestCookieCollectionPreservesExpiry:
    """`jar.set(name, value, domain, path)` builds a fresh cookie and defaults
    everything else, so it silently turns a 30-day cookie into a session one.

    Measured: all 16 cookies in a real session file recorded `expires: null`,
    including the trusted-device cookie, which made a granted 30-day trust look
    like the server declining to stamp any deadline.
    """

    def _response(self, *, expires=None, history=()):
        import http.cookiejar

        def make(name, exp):
            resp = httpx.Response(200, request=httpx.Request("GET", "https://connect.visma.com/"))
            resp.cookies.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value="v", port=None, port_specified=False,
                domain=".connect.visma.com", domain_specified=True,
                domain_initial_dot=True, path="/", path_specified=True,
                secure=True, expires=exp, discard=exp is None,
                comment=None, comment_url=None, rest={"HttpOnly": None},
            ))
            return resp

        final = make("remember2sv", expires)
        final.history = [make(n, e) for n, e in history]
        return final

    def test_expiry_survives_collection(self):
        from tripletex.auth.visma_connect import _collect_cookies
        import time

        deadline = int(time.time() + 30 * 86400)
        jar = httpx.Cookies()
        _collect_cookies(jar, self._response(expires=deadline))

        cookie, = jar.jar
        assert cookie.name == "remember2sv"
        assert cookie.expires == deadline

    def test_secure_and_httponly_survive(self):
        from tripletex.auth.visma_connect import _collect_cookies
        import time

        jar = httpx.Cookies()
        _collect_cookies(jar, self._response(expires=int(time.time() + 86400)))

        cookie, = jar.jar
        assert cookie.secure is True
        assert "HttpOnly" in (cookie._rest or {})

    def test_cookies_set_on_a_redirect_are_kept(self):
        """Visma sets the interesting cookies on the 302 from /totp/auth, not on
        the page it lands you."""
        from tripletex.auth.visma_connect import _collect_cookies
        import time

        deadline = int(time.time() + 30 * 86400)
        jar = httpx.Cookies()
        _collect_cookies(jar, self._response(history=[("TrustedOnRedirect", deadline)]))

        by_name = {c.name: c for c in jar.jar}
        assert by_name["TrustedOnRedirect"].expires == deadline

    def test_collected_expiry_reaches_the_saved_session(self):
        """End to end: what is collected is what `longest_lived_cookie` reads."""
        from tripletex.auth.visma_connect import _collect_cookies
        from tripletex.session import WebSession
        import time

        deadline = int(time.time() + 30 * 86400)
        jar = httpx.Cookies()
        _collect_cookies(jar, self._response(expires=deadline))

        restored = WebSession.from_dict(
            WebSession(cookies=jar, context_id="1").to_dict()
        )
        name, _ = restored.longest_lived_cookie()
        assert name == "remember2sv"


class TestDescribeSession:
    """One readout shared by `login` and `status`.

    They get compared minutes apart while checking whether a remember-device
    option took, so a login reporting one deadline and status another would be
    worse than either alone. Adopted from ops-monitor, which wrote it first.
    """

    def _session(self, *cookies, created_days_ago=0):
        import http.cookiejar
        import time
        from datetime import datetime, timedelta, timezone
        from tripletex.session import WebSession

        jar = httpx.Cookies()
        for name, days in cookies:
            jar.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value="v", port=None, port_specified=False,
                domain=".tripletex.no", domain_specified=True,
                domain_initial_dot=True, path="/", path_specified=True,
                secure=True,
                expires=None if days is None else int(time.time() + days * 86400),
                discard=days is None, comment=None, comment_url=None, rest={},
            ))
        return WebSession(
            cookies=jar, context_id="1",
            created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        )

    def test_reports_a_long_deadline_as_persistent(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("remember2sv", 30))))

        assert "remember2sv" in out
        assert "looks persistent" in out

    def test_reports_a_short_deadline_as_the_option_not_taking(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("VismaAuth", 0.5))))

        assert "did not take" in out

    def test_rotating_csrf_never_masks_a_good_deadline(self):
        """The bug this shares with ops-monitor: reading the soonest deadline
        calls a 30-day session short-lived."""
        from tripletex.session import describe_session

        out = "\n".join(describe_session(
            self._session(("CSRFTokenWriteOnly", 0.04), ("remember2sv", 30))
        ))

        assert "looks persistent" in out
        assert "(remember2sv)" in out
        assert "likely rotated rather than fatal" in out
        assert "CSRFTokenWriteOnly" in out  # shown, but as the soonest note

    def test_no_stamped_deadline_is_an_idle_timeout(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("JSESSIONID", None))))

        assert "idle timeout" in out
        assert "looks persistent" not in out

    def test_age_is_reported(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(created_days_ago=3)))

        assert "established" in out
        assert "3d" in out

    def test_last_ok_is_included_when_the_caller_knows_it(self):
        """pytripletex does not track this; ops-monitor does."""
        from datetime import datetime, timedelta, timezone
        from tripletex.session import describe_session

        seen = datetime.now(timezone.utc) - timedelta(days=2)
        out = "\n".join(describe_session(self._session(), last_ok_at=seen))

        assert "last used ok" in out

    def test_last_ok_is_omitted_when_unknown(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session()))

        assert "last used ok" not in out

    def test_expired_deadline_says_so(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("VismaAuth", -1))))

        assert "already expired" in out


class TestForcedLogin:
    """`authenticate()` means "ensure logged in", which leaves no way to apply a
    login option — an option only takes effect on a login that actually runs."""

    def _client(self, tmp_path, valid=True):
        from tripletex.client import TripletexClient
        from tripletex.config import TripletexConfig

        return TripletexClient.web(
            TripletexConfig(username="u", password_visma="p", session_dir=tmp_path)
        )

    async def test_healthy_session_short_circuits_without_force(self, tmp_path, monkeypatch):
        from tripletex.client import TripletexClient
        from tripletex.session import WebSession

        client = self._client(tmp_path)
        WebSession(cookies=httpx.Cookies(), context_id="1").save(
            tmp_path / "session_default.json"
        )

        monkeypatch.setattr(
            TripletexClient, "_validate_web_session", lambda self: _true()
        )
        called = {"login": False}

        async def fake_login(*a, **kw):  # pragma: no cover - must not run
            called["login"] = True

        monkeypatch.setattr(
            "tripletex.auth.visma_connect.visma_connect_login", fake_login
        )
        monkeypatch.setattr(TripletexClient, "_verify_company", lambda self: _none())

        await client.authenticate()
        assert called["login"] is False

    async def test_force_logs_in_again(self, tmp_path, monkeypatch):
        from tripletex.client import TripletexClient
        from tripletex.session import WebSession

        client = self._client(tmp_path)
        WebSession(cookies=httpx.Cookies(), context_id="1").save(
            tmp_path / "session_default.json"
        )

        seen = {}

        async def fake_login(config, http, prior_cookies=None):
            seen["ran"] = True
            seen["got_jar"] = prior_cookies is not None
            return WebSession(cookies=httpx.Cookies(), context_id="2")

        monkeypatch.setattr(
            "tripletex.auth.visma_connect.visma_connect_login", fake_login
        )
        monkeypatch.setattr(TripletexClient, "_verify_company", lambda self: _none())

        await client.authenticate(force=True)

        assert seen["ran"] is True
        # The stored jar is still handed over: a forced login is exactly when a
        # trusted-device cookie should be presented.
        assert seen["got_jar"] is True


async def _true():
    return True


async def _none():
    return None


class TestAuthenticatingCookiesOnly:
    """The longest deadline in the jar is not the session's deadline.

    Measured on a real post-login jar: `.AspNetCore.Culture` and
    `rememberUsername` are both stamped a year out. Both are preferences that
    authenticate nothing, and reporting either called the session persistent on
    evidence that meant nothing.
    """

    def _session(self, *cookies):
        import http.cookiejar
        import time
        from tripletex.session import WebSession

        jar = httpx.Cookies()
        for name, days in cookies:
            jar.jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value="v", port=None, port_specified=False,
                domain=".connect.visma.com", domain_specified=True,
                domain_initial_dot=True, path="/", path_specified=True,
                secure=True,
                expires=None if days is None else int(time.time() + days * 86400),
                discard=days is None, comment=None, comment_url=None, rest={},
            ))
        return WebSession(cookies=jar, context_id="1")

    def test_locale_preference_is_not_the_session_deadline(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(
            self._session((".AspNetCore.Culture", 365), ("remember2sv", 30))
        ))

        assert "(remember2sv)" in out
        assert "expires     : " in out
        assert ".AspNetCore.Culture" not in out

    def test_remembered_username_is_not_authority_either(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(
            self._session(("rememberUsername", 365), ("remember2sv", 30))
        ))

        assert "rememberUsername" not in out

    def test_load_balancer_cookies_are_ignored(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(
            self._session(("AWSALBTG", 7), ("remember2sv", 30))
        ))

        assert "AWSALB" not in out

    def test_trust_grant_gets_its_own_line(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("remember2sv", 30))))

        assert "device trust:" in out
        assert "should skip MFA" in out

    def test_no_trust_line_when_none_was_granted(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("VismaAuth", 1))))

        assert "device trust:" not in out

    def test_session_scoped_trust_cookie_is_not_a_grant(self):
        """Before the checkbox fix, `remember2sv` was present but session-scoped
        — issued, never granted."""
        assert self._session(("remember2sv", None)).trusted_device_cookie() is None

    def test_expired_grant_says_so_rather_than_promising_a_skip(self):
        from tripletex.session import describe_session

        out = "\n".join(describe_session(self._session(("remember2sv", -1))))

        assert "device trust: expired" in out
        assert "should skip MFA" not in out


class TestPageComplaint:
    """Visma rejects a step by re-rendering the page with the reason on it,
    not by returning an error status. Twice that looked like the form had moved
    and sent us guessing at passwordless and layout changes."""

    def test_validation_summary_is_surfaced(self):
        from tripletex.auth.visma_connect import _page_complaint

        html = """<div class="validation-summary-errors">
                    <ul><li>The username or password is incorrect.</li></ul>
                  </div>"""
        assert "username or password is incorrect" in _page_complaint(html)

    def test_field_error_is_surfaced(self):
        from tripletex.auth.visma_connect import _page_complaint

        html = '<span class="field-validation-error">Enter your 6-digit code</span>'
        assert "Enter your 6-digit code" in _page_complaint(html)

    def test_silence_is_reported_as_silence(self):
        from tripletex.auth.visma_connect import _page_complaint

        assert "no validation message" in _page_complaint("<form></form>")

    def test_duplicates_are_collapsed(self):
        from tripletex.auth.visma_connect import _page_complaint

        html = ('<div class="text-danger">Same</div>'
                '<div class="alert-danger">Same</div>')
        assert _page_complaint(html).count("Same") == 1
