"""Salary drafts — import a run, inspect it, commit it.

**There are two salary APIs in Tripletex and they behave differently.**

`/v2/salary/transaction` is documented and works with an API token, and a POST
to it *books a wage voucher immediately*. It has no draft concept, no `PUT`, and
no approve step — a correction means deleting the run and recreating it, which
issues a fresh payslip to everybody.

`/v2/tsk/salaryv2/*`, used here, is undocumented and **web session only** (403
to a token). It models the run the way the UI does: an editable draft that
becomes a voucher only when `:run` is called.

    completed=False  voucher=None   "Salary payment (under process)"
      -> run_salary()
    completed=True   voucher={id}   "Salary voucher 14-2026"

That difference is the whole reason this module exists. Corrections before the
run touch one payslip; corrections after it mean reversing and reissuing for
everyone, which is what makes automated payroll unpleasant for staff.

The import itself goes through two legacy endpoints rather than `/v2` — a
multipart upload, then a JSON-RPC form invocation. Both are captured from the UI
and verified 2026-09-04.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tripletex.models import ImportedFile, SalaryDraft, SalaryPaymentDraft
from tripletex.session import require_web_session

if TYPE_CHECKING:
    from tripletex.client import TripletexClient

logger = logging.getLogger(__name__)

_V2 = "/v2/tsk/salaryv2"

#: Everything needed to judge a draft before committing it.
_DRAFT_FIELDS = (
    "id,displayName,date,year,month,periodAsString,completed,isHistorical,"
    "voucherComment,paymentDate,paySlipsAvailableDate,sumPaidAmount,"
    "sumTaxDeductionAmount,voucher(id,numberAsString),"
    "salaryPayments(id,isTaxCardMissing,payrollTaxPercentage,grossAmount,amount,"
    "employee(id,displayName,employeeNumber),validationResults(*),"
    "specifications(id,specificationType,rate,count,amount,description,"
    "countAndRateEditable,salaryType(id,number,name)))"
)

_PAYMENT_FIELDS = (
    "id,grossAmount,amount,isTaxCardMissing,payrollTaxPercentage,"
    "employee(id,displayName,employeeNumber),validationResults(*),travelExpenses(*),"
    "specifications(id,specificationType,rate,count,amount,description,"
    "countAndRateEditable,project(id,displayName),department(id,displayName),"
    "salaryType(id,number,name))"
)

#: Columns the salary import CSV expects, in order. Header row included when
#: `ignore_first_row` is on, which is the UI's default.
CSV_COLUMNS = (
    "YEAR", "MONTH", "EMPLOYEE NUMBER", "WAGE TYPE NUMBER", "COMMENT",
    "QUANTITY", "RATE", "PROJECT NUMBER", "DEPARTMENT NUMBER",
)


class SalaryImportRejected(RuntimeError):
    """Tripletex refused the CSV, and said why.

    Distinct from a transport failure: the file reached Tripletex, was parsed,
    and was rejected on its contents. The message is Tripletex's own and usually
    names the offending row — worth showing to whoever produced the file rather
    than wrapping in something vaguer.

    Rejections arrive as a JSON-RPC `error` at HTTP 200, so nothing about the
    status code distinguishes this from success.
    """


async def get_salary_draft(
    client: TripletexClient, transaction_id: int
) -> SalaryDraft:
    """A salary run and every payslip on it.

    GET /v2/tsk/salaryv2/transaction/{id}. Works whether or not the run has been
    committed — check `SalaryDraft.is_committed` rather than assuming.
    """
    require_web_session(client.session, "Salary drafts")
    data = await client.get_json(
        f"{_V2}/transaction/{transaction_id}", params={"fields": _DRAFT_FIELDS}
    )
    return SalaryDraft.model_validate(data.get("value") or {})


async def get_salary_payment(
    client: TripletexClient, payment_id: int
) -> SalaryPaymentDraft:
    """One employee's draft payslip, with its specifications expanded.

    GET /v2/tsk/salaryv2/payment/{id}. Carries more than the nested copy inside
    the draft — travel expenses, cost carriers, and the per-line editability
    flags that say which values a correction may touch.
    """
    require_web_session(client.session, "Salary drafts")
    data = await client.get_json(
        f"{_V2}/payment/{payment_id}", params={"fields": _PAYMENT_FIELDS}
    )
    return SalaryPaymentDraft.model_validate(data.get("value") or {})


async def upload_import_file(
    client: TripletexClient, path: Path | str, client_id: str = "importFile"
) -> ImportedFile:
    """Stage a CSV for import. Returns the handle `import_salary_csv` needs.

    POST /execute/uploadCentral — multipart, not `/v2`. The response carries the
    id, revision and checksum that identify the staged file; the import call
    will not accept a path or the bytes themselves.

    Staging alone changes nothing: no draft exists until the file is imported.
    """
    session = require_web_session(client.session, "Salary import")
    path = Path(path)

    response = await client._request(
        "POST",
        "/execute/uploadCentral",
        params={"contextId": session.context_id},
        files={"file": (path.name, path.read_bytes(), "text/csv")},
        data={"uid": "1", "clientId": client_id},
        for_json=False,
    )
    rows = response.json()
    if not rows:
        raise RuntimeError(f"Upload of {path.name} returned no file handle")
    return ImportedFile.model_validate(rows[0])


async def import_salary_csv(
    client: TripletexClient,
    uploaded: ImportedFile,
    *,
    voucher_date: date,
    ignore_first_row: bool = True,
    generate_tax_deduction: bool = True,
    is_historical: bool = False,
    delimiter: str = ",",
) -> int:
    """Turn a staged CSV into a salary **draft**. Returns its transaction id.

    POST /JSON-RPC?method=no.tripletex.tcp.web.SalaryImportForm — a legacy form
    invocation, not `/v2`. The three booleans are the three switches the import
    screen shows.

    **This creates a draft, not a voucher.** Nothing is booked and no payslip
    reaches an employee until `run_salary`. That is the whole reason to import
    through this path rather than `POST /v2/salary/transaction`, which books
    immediately.

    `generate_tax_deduction` makes Tripletex compute the withholding line
    (`6000 Skattetrekk`) from each employee's tax card. Leave it on unless you
    are supplying the tax lines yourself — and if an employee's card is missing,
    the draft says so in `is_tax_card_missing` rather than failing.
    """
    session = require_web_session(client.session, "Salary import")

    body = {
        "method": "BaseForm.invoke",
        "params": [
            {
                "javaClass": "no.tripletex.tcp.web.SalaryImportForm",
                "documentationComponent": "256",
                "fileType": "0",
                "encoding": "",
                "delimiter": delimiter,
                "voucherDate": voucher_date.isoformat(),
                "importFile": {
                    "name": uploaded.name,
                    "id": uploaded.id,
                    "revision": uploaded.revision,
                    "checksum": uploaded.checksum,
                    "clientId": "importFile",
                },
                "file": "",
                "ignoreFirstRow": ignore_first_row,
                "generateTaxDeduction": generate_tax_deduction,
                "isHistorical": is_historical,
            },
            "doImport",
        ],
        "id": 1,
    }

    response = await client._request(
        "POST",
        "/JSON-RPC",
        params={
            "method": "no.tripletex.tcp.web.SalaryImportForm",
            "contextId": session.context_id,
        },
        content=json.dumps(body),
        extra_headers={"Content-type": "text/plain"},
        for_json=False,
    )
    payload = response.json() or {}

    # A rejected import is a JSON-RPC *error*, not a result with validations on
    # it, and it arrives at HTTP 200. Reading only `result` turns a perfectly
    # explicit message — "Arbeidsforholdet er ikke knyttet mot en virksomhet.
    # Linje 2: …", naming the offending row — into "validations=None".
    error = payload.get("error") or {}
    if error:
        messages = error.get("generalMessages") or []
        detail = "; ".join(messages).strip() or str(
            error.get("endUserMessage") or error.get("msg") or error.get("codeName") or ""
        ).strip('"')
        raise SalaryImportRejected(detail or "Tripletex rejected the import, without saying why")

    result = payload.get("result") or {}

    # The new draft's id arrives only inside a UI navigation instruction —
    # there is no field for it. Parse it out rather than making the caller do so.
    transaction_id = _transaction_id_from_forward(result.get("forward"))
    if transaction_id is None:
        raise RuntimeError(
            "Salary import returned neither a transaction id nor an error. "
            f"validations={result.get('validations')} messages={result.get('messages')}"
        )
    logger.info("Salary CSV imported as draft transaction %s", transaction_id)
    return transaction_id


def _transaction_id_from_forward(forward: object) -> int | None:
    """Dig the new transaction id out of the JSON-RPC `forward` instruction.

    It arrives as a JSON *string* holding a list holding a URL:
    `"[{\\"_action\\":\\"navigateDirect\\",\\"url\\":\\"/execute/salary?salaryTransactionId=7562010\\"}]"`
    """
    if not isinstance(forward, str):
        return None
    try:
        actions = json.loads(forward)
    except ValueError:
        return None
    for action in actions if isinstance(actions, list) else []:
        url = (action or {}).get("url", "")
        _, _, tail = url.partition("salaryTransactionId=")
        digits = "".join(c for c in tail if c.isdigit())
        if digits:
            return int(digits)
    return None


async def delete_salary_draft(
    client: TripletexClient,
    transaction_id: int,
    *,
    force: bool = False,
) -> None:
    """Discard a salary draft. **Web session only.** Returns nothing.

    DELETE /v2/tsk/salaryv2/transaction/{id}.

    This is what makes importing a draft a reversible act, and therefore what
    makes it safe to test an import against a real company at all: a draft you
    cannot abandon is a one-way door, and it shows up in the UI as "Salary
    payment (under process)" where whoever runs payroll will find it.

    **A committed run is refused unless `force`.** The same route deletes both,
    and on a committed run that means reversing a booked voucher and reissuing
    payslips for everybody — the exact outcome this module exists to avoid. The
    state is checked first rather than trusted to the caller, because the id of
    a draft and the id of a booked run look identical.

    `force` exists because deleting a committed run is occasionally the right
    answer, and a library that makes it impossible invites someone to do it by
    hand in a worse way. It should be a deliberate keystroke, not a default.
    """
    require_web_session(client.session, "Salary drafts")

    if not force:
        draft = await get_salary_draft(client, transaction_id)
        if draft.is_committed:
            raise ValueError(
                f"Salary transaction {transaction_id} is already committed"
                f"{f' as voucher {draft.voucher}' if draft.voucher else ''}. "
                f"Deleting it reverses the voucher and reissues payslips for "
                f"every employee on the run. Pass force=True if that is truly "
                f"what you want."
            )

    await client._request("DELETE", f"{_V2}/transaction/{transaction_id}", for_json=False)
    logger.info("Deleted salary draft %s", transaction_id)


async def add_calculated_specs(
    client: TripletexClient,
    transaction_id: int,
    *,
    payment_ids: list[int],
    spec_types: str = "EXPENSES",
) -> SalaryDraft:
    """Pull Tripletex-calculated lines into named payslips on a draft.

    PUT /v2/tsk/salaryv2/transaction/{id}/:addTlxCalculatedSpecs

    `spec_types` is `EXPENSES` in the flow captured from the UI — that is how
    approved travel and employee expenses reach a payslip. This edits the draft
    and books nothing.
    """
    require_web_session(client.session, "Salary drafts")
    if not payment_ids:
        raise ValueError("payment_ids is empty — nothing to add specs to")

    data = await client.put_json(
        f"{_V2}/transaction/{transaction_id}/:addTlxCalculatedSpecs",
        params={
            "salaryPaymentIds": ",".join(str(i) for i in payment_ids),
            "specTypesToAdd": spec_types,
        },
    )
    return SalaryDraft.model_validate(data.get("value") or {})


async def run_salary(
    client: TripletexClient,
    transaction_id: int,
    *,
    payment_type_id: int,
    payment_date: date,
    payslips_available_date: date,
    one_time_password: str = "",
) -> SalaryDraft:
    """Commit the draft: book the voucher and release the payslips.

    PUT /v2/tsk/salaryv2/transaction/{id}/:run

    **This is the irreversible step, and the only one.** Everything before it is
    editable; after it, a correction means reversing the voucher and reissuing
    payslips to everyone on the run. Inspect the draft first —
    `blocking_problems` and `employees_without_tax_card` exist for that — and
    treat this as the moment a human decides.

    `payslips_available_date` controls when employees can see their payslip and
    is set *here*, not at import, so visibility can be held back from a run that
    is booked today.

    `one_time_password` was empty in the captured flow but is part of the
    signature, so some configurations demand one; if yours does, an empty string
    will be rejected rather than silently ignored.
    """
    require_web_session(client.session, "Salary drafts")

    logger.warning(
        "Committing salary draft %s — booking a voucher and releasing payslips on %s",
        transaction_id, payslips_available_date,
    )
    data = await client.put_json(
        f"{_V2}/transaction/{transaction_id}/:run",
        params={
            "paymentTypeId": str(payment_type_id),
            "paymentDate": payment_date.isoformat(),
            "paySlipsAvailableDate": payslips_available_date.isoformat(),
            "oneTimePassword": one_time_password,
        },
    )
    return SalaryDraft.model_validate(data.get("value") or {})
