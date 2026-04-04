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

Create `~/.tripletex/config.toml` with your Visma Connect credentials:

```toml
[default]
username = "you@example.com"
password_visma = "visma_connect_password"
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

## Usage

```bash
# Login (interactive MFA prompt)
tripletex login

# With a named section
tripletex --env other login

# List companies (one login can access multiple companies)
tripletex companies

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
```

You can also pass credentials via environment variables (`TRIPLETEX_USERNAME`, `TRIPLETEX_PASSWORD_VISMA`) or manual browser cookies (`--cookie`, `--context-id`, `--csrf-token`).

## Development

```bash
uv pip install -e ".[dev]"
pytest
```
