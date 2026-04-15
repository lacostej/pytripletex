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

# Voucher backup
tripletex vouchers backup --output-dir ./backup --from 2025-01-01 --to 2025-12-31

# Employee wages
tripletex wages dump -o wages.json

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
