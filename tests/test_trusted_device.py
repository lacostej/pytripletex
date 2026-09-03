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
