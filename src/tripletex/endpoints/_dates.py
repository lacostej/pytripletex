"""Date-range conventions at the API boundary.

**Tripletex's list endpoints take `dateTo` as exclusive.** This is documented,
not a quirk we discovered: the published specification says so for every
date-ranged path this library calls —

    /ledger/posting                    'Format is yyyy-MM-dd (to and excl.).'
    /ledger/voucher                    'To and excluding'
    /ledger/closeGroup                 'To and excluding'
    /ledger/voucher/>nonPosted         'To and excluding'
    /ledger/voucher/>voucherReception  'To and excluding'
    /reminder                          'To and excluding'

— and a same-day range is refused outright, with a `422` whose message is
explicit: ``'From and including' value (Fri Jun 05 …) is greater than or equal
'To and excluding' value (Fri Jun 05 …) in filter``.

**pytripletex's own functions take `date_to` as inclusive**, because that is what
a caller asking for "January" or "FY2025" means, and because passing an
inclusive date straight through is a silent, plausible-looking error: the call
succeeds, the numbers are wrong by one day, and nothing complains.

That is not hypothetical. It shipped. Measured on account 1500 over
2026-01-01..06-30, sending the caller's `2026-06-30` unconverted returned **333
of 346 postings** and reported the account's movement as **-23 286.00 instead of
+20 182.00** — a sign flip, because the 13 lost lines were the month-end
settlements. Downstream that produced three published audit findings that had to
be retracted.

The last day of a range is the worst possible day to lose. Month-end and
year-end carry the accruals, the periodisering and the settlement postings — the
entries an audit is most interested in — so the failure is both large and biased
toward exactly what matters.

Convert once, here, where the parameter is built. Never send a caller's
`date_to` directly.
"""

from __future__ import annotations

from datetime import date, timedelta


def exclusive_end(date_to: date) -> str:
    """Render an inclusive `date_to` as the exclusive `dateTo` the API wants."""
    return (date_to + timedelta(days=1)).isoformat()
