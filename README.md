# pytripletex

Python client for Tripletex accounting — supports both the official API (token auth) and web session access (Visma Connect).

## Features

Two auth modes — most `/v2/*` endpoints work with both:

- **Web session** (Visma Connect + MFA) — interactive login, session persisted to disk
- **API tokens** (consumer/employee tokens) — non-interactive, for backends

Features: login, payments, voucher inbox, bank reconciliation, voucher backup,
wages, customers, products, orders, invoices, multi-company support.

## Install

```bash
uv pip install -e .
```

## Setup

Create `~/.tripletex/config.toml`:

```toml
[default]
# Web session auth (Visma Connect)
username = "you@example.com"
password_visma = "visma_connect_password"

# Official API auth (optional — enables token-based access)
consumer_token = "..."
employee_token = "..."
```

### Staying logged in

Web sessions die on their own schedule — observed anywhere from about a day to
about three months — and a death used to cost an interactive MFA login.

**Most of the time it no longer does.** Tripletex is a service provider in front
of Visma Connect, so a dead Tripletex session leaves the identity provider's
session untouched — which is why a browser signing back in is never asked for a
code. `reuse_idp_session`, on by default, reproduces that: the stored
`connect.visma.com` cookies are carried into the next login, and if Visma still
recognises the session the login completes with no password and no code.

```
$ tripletex login --force
Carrying 5 durable cookie(s) from the previous session.
Visma Connect session still valid — no login needed.
```

That is what makes an unattended re-login possible. It is on by default because
it changes nothing about what is stored — those cookies were always written to
the session file — only whether they are presented. Turn it off with
`reuse_idp_session = false` to force a full authentication every time.

Two further settings exist and neither is proven:

```toml
[prod]
trust_device = true          # tick "remember this device for 30 days" at the MFA step
persistent_session = true    # ask for a long-lived session, if the form offers one
```

`trust_device`'s **effect is unverified**. A login with the box explicitly
unticked is issued the same `remember2sv` cookie, with the same 30-day expiry,
as one that ticks it, and carrying that cookie *without* an identity-provider
session did not skip the MFA step. Nothing observed distinguishes the two, so it
may be inert. Do not rely on it.

Any of them can be set for one login without touching the config:

```bash
tripletex --env prod login --no-reuse-idp-session   # force a full authentication
```

`login` and `tripletex status` print the same readout, which is how you tell
whether the option took:

```
$ tripletex status
established : 2026-09-03 08:59 UTC (0d 0h ago)
expires     : 2026-10-03 08:59 UTC (remember2sv) — 29d 23h left
              soonest 2026-09-03 09:19 UTC (.AspNetCore.Antiforgery…), likely rotated rather than fatal
status      : VALID
```

The deadline line reads the longest expiry among cookies that actually carry
authority — never the soonest, since `CSRFTokenWriteOnly` and the antiforgery
cookie rotate per request and would report a good 30-day login as a failure, and
never blindly the longest either: `.AspNetCore.Culture` and `rememberUsername`
are stamped a year out and authenticate nothing. Nothing stamped at all would
point at an idle timeout rather than a fixed lifetime.

**It deliberately passes no verdict on a long deadline.** A plain login with no
options is issued `remember2sv` at 30 days, so any "looks persistent" verdict
would print on every run and confirm nothing. Only a short deadline is flagged,
because that one is actionable whatever caused it.

Re-running `login` on a healthy session is a no-op, so use `--force` to apply an
option to a session that still works:

```bash
tripletex login --trust-device --force
```

`status` also says whether the session still authenticates, which is a separate
question from whether it has expired on paper — a session can be inside its
deadline and still rejected.

Add `company_id` to any section to assert which company its credentials reach.
It is checked at authentication time, so a mistyped `--env`, a re-pointed section
or a token copied between companies fails immediately instead of quietly reading
the wrong company's books:

```toml
[BH]
company_id = 32611682        # from /v2/token/session/>whoAmI
consumer_token = "..."
employee_token = "..."
```

```
Error: 'BH' authenticates to company 56801690, but the config declares
company_id 32611682. Check the section's tokens, or drop company_id if the
move was intended.
```

It is an id rather than a name because companies get renamed and ids do not.
Sections without `company_id` are not checked.

Each section is a user account. If you have multiple logins, add named sections:

```toml
[default]
username = "you@example.com"
password_visma = "your_password"

[other]
username = "colleague@example.com"
password_visma = "their_password"
```

For test environments, override `base_url`:

```toml
[test]
base_url = "https://api-test.tripletex.tech"
consumer_token = "..."
employee_token = "..."
# username/password_visma also work here for web session testing
```

## Usage

### Auth mode

The `--auth` flag controls how you authenticate:

```bash
tripletex --auth web payments list   # force web session (Visma Connect)
tripletex --auth api customer list   # force API token (consumer/employee)
tripletex customer list              # auto-detect (API if tokens set, else web)
```

Most `/v2/*` endpoints work with both auth modes. Web session auth requires
interactive MFA login; API token auth is non-interactive.

### Company selection

```bash
# List all accessible companies
tripletex companies

# Run a command against a specific company
tripletex --company "Bonita Services" customer list

# With a named config section
tripletex --env other login
```

### Commands

```bash
# Login (interactive MFA prompt, persists session to ~/.tripletex/)
tripletex login

# Payments awaiting approval
tripletex payments list
tripletex payments list --due-within 14 --company "My Company"
tripletex payments list --status ALL

# Voucher inbox
tripletex inbox

# Bank reconciliation
tripletex reconciliation unreconciled --month 2026-03

# Vouchers
tripletex vouchers list --from 2026-08-01 --to 2026-09-01 [--json]
tripletex vouchers queue [--json]     # registered but not yet posted
tripletex vouchers reception [--json] # documents in bilagsmottak
tripletex vouchers backup --output-dir ./backup --from 2025-01-01 --to 2025-12-31

# Employee wages
tripletex wages dump -o wages.json

# Employees
tripletex employee list [--active] [-q "search"]
tripletex employee get 12345
tripletex employee access            # login access vs. employment status (web auth)
tripletex employee payslip [--manual]  # payslip delivery: app or manual (web auth)

# Customers
tripletex customer list [-q "search"]
tripletex customer get 12345

# Products
tripletex product list

# Orders
tripletex order list --from 2026-01-01 --to 2026-03-31
tripletex order get 12345

# Invoices
tripletex invoice list --from 2026-01-01 --to 2026-03-31
```

> **Date ranges are half-open `[from, to)`** — `--from` is inclusive, `--to` is
> exclusive. An empty range (`--from == --to`) is rejected with HTTP 422.

> **List functions return every match**, paging until a short page. Pass
> `limit=N` to stop early. They deliberately ignore `fullResultSize`, which
> Tripletex documents as "Indicates whether there are more values available.
> Note: The value is not exact" — measured, it is `min(total, count) + 1`.

#### `employee access`

Changing the unit an employee is registered on makes Tripletex end the current
employment (`EMPLOYMENT_END_INTERNAL_CHANGE`) and create a new one. When the
ended employment has "remove access at employment end" set, the login is revoked
— and starting the new employment does not bring it back. `employee access`
lists employees in that state: an active employment, but `allowLogin` off or a
`loginEndDate` in the past.

The login fields are not in any `/v2/*` endpoint (`SalesForceEmployee` carries
`loginEndDate` in the API spec, but no path serves it, and the internal
`/v2/salary/employee/overview/details` — which exposes `allowLogin` — rejects API
tokens with 403). They are scraped from each employee's "User access" tab, so
this command needs `--auth web` and makes one request per employee.

#### `employee payslip`

Shows whether each employee gets payslips through the Tripletex app or has them
handled manually, from the internal salary overview endpoint (web auth only —
API tokens get a 403). The method is a display string in your Tripletex language,
so `--manual` matches on the word "app" rather than on the full English string.

### Programmatic usage

```python
from tripletex import TripletexClient
from tripletex.config import load_config
from tripletex.endpoints.customers import list_customers
from tripletex.endpoints.inbox import list_inbox

config = load_config()

async with TripletexClient.web(config) as web:
    inbox = await list_inbox(web)

async with TripletexClient.api(config) as api:
    customers = await list_customers(api)

# Both in the same script
async with TripletexClient.web(config) as web, TripletexClient.api(config) as api:
    inbox = await list_inbox(web)
    customers = await list_customers(api)
```

You can also pass credentials via environment variables (`TRIPLETEX_USERNAME`,
`TRIPLETEX_PASSWORD_VISMA`, `TRIPLETEX_CONSUMER_TOKEN`, `TRIPLETEX_EMPLOYEE_TOKEN`)
or manual browser cookies (`--cookie`, `--context-id`, `--csrf-token`).

## Development

```bash
uv pip install -e ".[dev]"
pytest
```
