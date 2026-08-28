"""Pagination helper for Tripletex list endpoints.

`fullResultSize` is not a total. On every list endpoint measured except
`/v2/ledger/voucher` it is ``min(total, count) + 1`` — a has-more flag that only
looks like a total when `count` happens to exceed the result set:

    /v2/employee   count=1 -> frs=2    count=3 -> frs=4    count=5000 -> frs=65 (65 rows)
    /v2/ledger/voucher      frs=2453 at every count       (a real total)

So we never read it. Paging stops on the first short page, which is correct on
both kinds of endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tripletex.client import TripletexClient


async def paginate(
    client: TripletexClient,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    page_size: int = 1000,
    limit: int | None = None,
) -> list[dict]:
    """Fetch every row of a list endpoint, or the first `limit` rows.

    Pages with `from`/`count` until a page comes back short. Pass `json_body` for
    endpoints that expect POST (the internal salary ones).
    """
    base = dict(params or {})
    rows: list[dict] = []
    offset = 0

    while True:
        size = page_size if limit is None else min(page_size, limit - len(rows))
        if size <= 0:
            break

        page_params = {**base, "from": str(offset), "count": str(size)}
        if json_body is None:
            data = await client.get_json(path, params=page_params)
        else:
            data = await client.post_json(path, params=page_params, json_body=json_body)

        values = data.get("values", [])
        rows.extend(values)
        if len(values) < size:
            break
        offset += len(values)

    return rows
