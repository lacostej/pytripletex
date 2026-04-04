# pytripletex

Python client for Tripletex accounting — web session API access for features not covered by the official API.

## Features

- **Login** — Automated Visma Connect authentication with MFA
- **Payments** — List bank payments awaiting approval (with due date filtering)
- **Voucher inbox** — List unprocessed receipts and invoices
- **Bank reconciliation** — Find unreconciled transactions across all accounts
- **Voucher backup** — Download all vouchers with metadata and PDF attachments
- **Wages** — Fetch employee salary data from the web UI
- **Multi-company** — Iterates all companies accessible to your account

## Install

```bash
uv pip install -e .
```

## Setup

Create `~/.tripletex/config.toml`:

```toml
[bonita]
username = "you@example.com"
password_visma = "visma_connect_password"
```

## Usage

```bash
# Login (interactive MFA prompt)
tripletex --env bonita login

# List companies
tripletex --env bonita companies

# Payments awaiting approval
tripletex --env bonita payments list
tripletex --env bonita payments list --due-within 14 --company "My Company"
tripletex --env bonita payments list --status ALL

# Voucher inbox
tripletex --env bonita inbox

# Bank reconciliation
tripletex --env bonita reconciliation unreconciled --month 2026-03
tripletex --env bonita reconciliation missing-receipts --month 2026-03

# Voucher backup
tripletex --env bonita vouchers backup --output-dir ./backup --from 2025-01-01 --to 2025-12-31

# Employee wages
tripletex --env bonita wages dump -o wages.json
```

You can also pass credentials via environment variables (`TRIPLETEX_USERNAME`, `TRIPLETEX_PASSWORD`, `TRIPLETEX_PASSWORD_VISMA`) or manual browser cookies (`--cookie`, `--context-id`, `--csrf-token`).

## Development

```bash
uv pip install -e ".[dev]"
pytest
```
