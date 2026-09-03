# How Tripletex and Visma Connect authentication actually works

What we know, how we know it, and what is still guesswork. Written 2026-09-03
while implementing session reuse, after several confident conclusions turned out
to be wrong.

Every claim below is marked with its provenance:

- **[docs]** — stated in Visma's public documentation, quoted with a link.
- **[measured]** — observed directly on the wire against a live account.
- **[unverified]** — plausible, untested. Treat as a question, not a fact.

---

## Two parties, not one

Tripletex is a **service provider** sitting in front of **Visma Connect**, which
is the **identity provider** — a standard OpenID Connect one. Its discovery
document is public and unauthenticated **[measured]**:

```
https://connect.visma.com/.well-known/openid-configuration
```

That confirms the usual endpoints (`/connect/authorize`, `/connect/token`,
`/connect/userinfo`, `/connect/introspect`, `/connect/endsession`,
`/connect/checksession`) and `prompt_values_supported: ["none", "login",
"consent", "select_account"]`.

**None of them are usable by this library**, which is worth stating so nobody
spends an afternoon on it: `userinfo` needs an access token, `introspect` needs
client credentials, and we are not the OAuth client — Tripletex is. We drive the
browser-facing flow and never see a token. `prompt=none` would give a silent
session-liveness probe, but it needs a registered `redirect_uri`, so it is
Tripletex's to use, not ours.

Getting this wrong is easy and expensive: **logging out of Tripletex does not log
you out of Visma Connect.** The service-provider session ends; the identity
provider's does not.

## Three independent lifetimes

This is the part that matters, and the part we got wrong repeatedly by assuming
there was only one session.

| Layer | Lifetime | Source |
|---|---|---|
| Visma Connect IdP session | **10 hours**, absolute | **[docs]** |
| Device trust ("remember this device") | **30 days** | **[docs]** |
| Tripletex web session | days to months, TTL unknown | **[measured]** |
| API tokens (consumer + employee) | no session at all | **[measured]** |

> Visma Connect IdP maximum session lifetime is 10 hours, no matter if there is
> activity or not by the user.
> — <https://docs.connect.visma.com/docs/session-management>

Note "no matter if there is activity": it is an **absolute** cap, not a sliding
idle window, so using the session does not extend it.

> remember my device for 30 days […] you will only be prompted for the 2nd step
> once a month
> — <https://docs.connect.visma.com/docs/2fa-faq>

The consequence is not obvious and is easy to get backwards: because the
Tripletex session usually outlives the IdP session by days, **by the time a
Tripletex session dies, the IdP session is nearly always gone too.** Reusing the
IdP session therefore helps far less often than it appears to in testing, where
logins are minutes apart.

## The login flow

Six steps, of which only the middle three involve a human **[measured]**:

1. `GET tripletex.no/execute/login` → redirect chain to `connect.visma.com`.
2. `POST /` with `Username`.
3. `POST /login/password` with `Password`.
4. `POST /totp/auth` with `AuthCode` — skipped when the IdP session is live.
5. Redirect chain back to Tripletex, sometimes via an auto-submitting form.
6. `contextId` is read out of the final URL.

When a live IdP session exists, steps 2–4 vanish entirely: the chain lands
straight back on Tripletex with a `contextId` and no login form is ever
rendered. A client that treats "no login form" as an error will mistake success
for failure.

**Failures are re-rendered pages, not error statuses.** Visma rejects a step by
serving the same page again with the reason in a validation summary. Anything
that only looks at status codes sees a 200 and a missing form, which reads like
a layout change and sends you hunting the wrong thing.

## The MFA form has a trap in it

The "remember this device for 30 days" checkbox is rendered the ASP.NET way
**[measured]**:

```html
<input type="checkbox" name="RememberCode" value="true">
...
<input name="RememberCode" type="hidden" value="false">   <!-- last -->
```

A browser posting a ticked box sends the name **twice**, `true` then `false`,
and the model binder takes the first. Any scraper that flattens a form into a
`dict[str, str]` gets the hidden `false` last and therefore posts `false` —
meaning it actively declines the option rather than merely not asking for it.

There is also a hidden `DisableRememberDevice`; when `True` the checkbox is
decorative.

## Cookies

From a real jar after a successful login **[measured]**. Values omitted.

| Cookie | Domain | Expiry | What it is |
|---|---|---|---|
| `session` | `.connect.visma.com` | session | IdP session — **this is what skips MFA** |
| `sid` | `.connect.visma.com` | session | IdP session id |
| `tempSession` | `.connect.visma.com` | session | per-login scratch |
| `remember2sv` | `.connect.visma.com` | 30 days | 2SV state — **not a grant indicator**, see below |
| `.AspNetCore.Culture` | `.connect.visma.com` | 365 days | locale preference, authenticates nothing |
| `rememberUsername` | `.connect.visma.com` | 365 days | Data Protection blob; **breaks a fresh login when stale** |
| `.AspNetCore.Antiforgery.*` | `.connect.visma.com` | ~minutes | per-request; **breaks a fresh login when stale** |
| `returnUrl` | `.connect.visma.com` | ~minutes | pins the flow to one authorization request |
| `JSESSIONID` | `.tripletex.no` | session | Tripletex session |
| `CSRFTokenWriteOnly` | `.tripletex.no` | session | rotated per request |
| `isTripletexUser` | `.tripletex.no` | 60 days | flag, authenticates nothing |
| `AWSALB*` | `tripletex.no` | 7 days | load balancer stickiness |

Three things worth knowing about that table:

- **`remember2sv` is not evidence of device trust** despite the name and the
  30-day expiry. A login posting `RememberCode=false` — the box explicitly
  unticked — is issued the identical cookie with the identical expiry
  **[measured]**. There is no known signal for whether a device grant was given.
- **The longest expiry in the jar is not the session's deadline.**
  `.AspNetCore.Culture` and `rememberUsername` are stamped a year out and carry
  no authority. Neither is the *soonest*: `CSRFTokenWriteOnly` and the
  antiforgery cookie rotate per request. The meaningful figure is the longest
  among cookies that actually authenticate.
- **Cookies are domain-scoped and the jar enforces it** **[measured]**. A
  `tripletex.no` cookie is never sent to `connect.visma.com`. Carrying them
  across is pointless rather than dangerous — they are simply never transmitted.

## Replaying a jar into a new login

What survives usefully, and what actively breaks the flow **[measured]**:

- **Carry** the `connect.visma.com` cookies. That is what a browser does, and
  `session`/`sid` are what let the login skip authentication entirely.
- **Never carry** `.AspNetCore.Antiforgery.*` — ASP.NET validates the form's
  freshly-issued `__RequestVerificationToken` against it, so a stale one fails
  and the page is silently re-served.
- **Never carry** `returnUrl`, or `rememberUsername` — the latter narrowed down
  from two live runs that differed by exactly that cookie.
- **Ignore expiry on the IdP side.** A "session cookie" dies when a *browser*
  closes; that is a statement about a browser, not about the server-side session
  it names. The session is still valid and re-presenting the cookie is exactly
  what a still-open browser does. Requiring an expiry here drops `session` and
  `sid`, which is to say it drops the only thing that works.

## What is still unknown

- **Does device trust work for a non-browser client?** **[unverified]** The one
  test carried `remember2sv` alone into a fresh login and was still asked for a
  code, but that jar had been stripped to a single cookie, so it establishes
  very little. The decisive test needs the IdP session to have lapsed — more
  than ten hours since the last interactive login — and then a forced re-login:
  password expected, code hopefully not.
- **Is `persistent_session` offered at all?** **[unverified]** No
  stay-signed-in checkbox has been seen on the password form; the candidate
  field names are defensive and have never matched.
- **What is the Tripletex session's actual TTL?** **[unverified]** Observed
  between about a day and about three months. Nothing stamps a deadline, so it
  can only be watched.
- **Does anything identify a device besides the cookie?** **[unverified]** If
  Visma fingerprints the client, a non-browser will never hold device trust and
  the question above is settled in the negative.

## Sources

- <https://docs.connect.visma.com/docs/session-management> — the 10-hour cap
- <https://docs.connect.visma.com/docs/2fa-faq> — the 30-day device trust
- <https://docs.connect.visma.com/docs/check-session-iframe> — browser-only
  session checking
- <https://docs.connect.visma.com/llms.txt> — index of the above
- `https://connect.visma.com/.well-known/openid-configuration` — endpoints and
  supported flows
- `src/tripletex/auth/visma_connect.py` — the implementation, with the same
  findings recorded where they bite
