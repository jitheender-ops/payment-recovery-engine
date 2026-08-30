"""Statement of account — the substance a B2B dunning email carries.

A B2B dunning message without a statement is just another nag: the AP clerk
who opens it cannot answer "what do I owe, on which invoices, by when, and
where does the money go" without a round-trip email. The published ladders
(Chaser/Upflow/Gaviti) all converge on the statement attachment or in-body
invoice table as the core of every rung.

This module is PURE: it turns case rows and account facts into a plain-data
statement dict plus rendered text/HTML bodies. No I/O, no session access —
the caller (integration phase) hands it rows and sends the result. That
makes the exact wording and totals testable without a database.

Honesty rules carried over from the codebase:

* Amounts are paise-in, rupee-displayed via the shared money() — one
  formatting truth across console, page and statement.
* A statement never claims a payment that did not happen, and never hides
  one that did: part-paid invoices show both figures.
* The pay link per invoice is the per-case recovery URL the engine already
  mints — attribution stays per-invoice even when the message consolidates
  accounts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.formatting import ist, money

# Subject lines per ladder tone. Deliberately plain: B2B AP inboxes filter
# on spamminess, and "URGENT!!!" in a subject line is the fastest way to
# land in the junk folder. Firmness lives in the body, not the punctuation.
SUBJECTS: dict[str, str] = {
    "courtesy": "Invoice reminder from {merchant} — {total} due on {due}",
    "friendly": "Invoice reminder from {merchant} — {total} past due",
    "firm": "Payment overdue — {total} across {count} invoice(s) from {merchant}",
    "urgent": "Payment overdue — {total} from {merchant} ({oldest_days} days)",
    "final": "Final notice — {total} from {merchant}",
}

# Stage-aware SMS copy, one line per tone, under 160 chars. The link is the
# account statement page (all open invoices, per-invoice pay buttons), not a
# single-invoice link: one message, one place to settle everything.
SMS_BY_TONE: dict[str, str] = {
    "courtesy": (
        "{merchant}: invoice of {total} is due on {due}. Statement: {link}"
    ),
    "friendly": (
        "{merchant}: invoice of {total} is past due. Pay or see all invoices: {link}"
    ),
    "firm": (
        "{merchant}: {total} now overdue across {count} invoice(s). "
        "Statement: {link}"
    ),
    "urgent": (
        "{merchant}: {total} overdue {oldest_days}d. Please clear today: {link}"
    ),
    "final": (
        "{merchant}: final notice — {total} overdue {oldest_days}d. {link}"
    ),
}


def statement_lines(
    cases: list[dict[str, Any]],
    *,
    now: datetime,
) -> dict[str, Any]:
    """
    Consolidate open invoice cases into a statement payload.

    ``cases`` is a list of plain dicts (the caller maps RecoveryCase rows):
    ``subject_ref, due_at, amount_at_risk, amount_recovered, pay_url``.
    Returns the statement dict a message renders from: totals, per-invoice
    lines, aging, and the oldest-days figure every urgent/final template
    needs. Part-paid invoices keep their outstanding figure honest:
    at_risk − recovered, never the original amount.
    """
    lines: list[dict[str, Any]] = []
    total_outstanding = 0
    oldest_days = 0
    for c in cases:
        outstanding = int(c["amount_at_risk"]) - int(c.get("amount_recovered") or 0)
        due = c.get("due_at")
        days_past = 0
        if due is not None:
            days_past = max(0, (now - due).days)
        oldest_days = max(oldest_days, days_past)
        total_outstanding += outstanding
        lines.append(
            {
                "ref": str(c["subject_ref"]),
                "due_date": ist(due).strftime("%d %b %Y") if due else "—",
                "days_past": days_past,
                "original": int(c["amount_at_risk"]),
                "paid": int(c.get("amount_recovered") or 0),
                "outstanding": outstanding,
                "pay_url": c.get("pay_url"),
            }
        )
    return {
        "now": now,
        "lines": lines,
        "count": len(lines),
        "total_outstanding": total_outstanding,
        "oldest_days": oldest_days,
    }


def render_subject(
    statement: dict[str, Any], *, tone: str, merchant_name: str
) -> str:
    """The email subject for a stage. Plain, honest, no urgency theatre."""
    template = SUBJECTS.get(tone, SUBJECTS["friendly"])
    return template.format(
        merchant=merchant_name,
        total=money(statement["total_outstanding"]),
        count=statement["count"],
        due=statement["lines"][0]["due_date"] if statement["lines"] else "—",
        oldest_days=statement["oldest_days"],
    )


def render_sms(
    statement: dict[str, Any],
    *,
    tone: str,
    merchant_name: str,
    link: str,
) -> str:
    """
    The SMS line for a stage. Hard-capped at 160 chars — an SMS that spills
    to two segments splits its link across the boundary and the customer
    cannot tap it. Capping happens here, not at the sender, so every future
    sender inherits the limit.
    """
    template = SMS_BY_TONE.get(tone, SMS_BY_TONE["friendly"])
    text = template.format(
        merchant=merchant_name,
        total=money(statement["total_outstanding"]),
        due=statement["lines"][0]["due_date"] if statement["lines"] else "—",
        count=statement["count"],
        oldest_days=statement["oldest_days"],
        link=link,
    )
    return text[:160]


def render_email_text(
    statement: dict[str, Any],
    *,
    tone: str,
    merchant_name: str,
    link: str,
) -> str:
    """
    Plain-text email body. AP clerks forward dunning emails into ticketing
    systems that strip HTML; the text body is the one that survives.

    The invoice table is fixed-width columns rather than markdown tables —
    the widest terminal font in a ticketing system still renders it aligned.
    """
    head = (
        f"Dear {merchant_name} accounts team,\n\n"
        f"Payment is outstanding on the following invoice(s):\n\n"
    )
    rows = []
    for line in statement["lines"]:
        rows.append(
            f"  {line['ref']:<20} {line['due_date']:<12} "
            f"{money(line['outstanding']):>14}  {line['days_past']:>3}d overdue"
        )
    table = "\n".join(rows)
    total_line = f"\n  Total outstanding: {money(statement['total_outstanding'])}"
    footer = (
        f"\n\nPay securely, or see every open invoice, here:\n{link}\n\n"
        f"— {merchant_name}\n"
        f"This is a payment reminder from a system operated by {merchant_name}."
    )
    return head + table + total_line + footer


def render_email_html(
    statement: dict[str, Any],
    *,
    tone: str,
    merchant_name: str,
    link: str,
) -> str:
    """
    HTML email body. Emails are the one surface where the engine's premium-
    fintech-light direction carries: light background, one deep recovery-
    green accent, restrained pure-sans — pinned in the design system, kept
    here as inline styles because email clients strip stylesheets.

    Every merchant-influenced value is escaped with html.escape; the pay
    URL is rendered as a proper anchor with its origin displayed, so a
    customer can verify where the link goes before tapping.
    """
    import html as _html

    rows = []
    for line in statement["lines"]:
        pay_cell = (
            f'<a href="{_html.escape(str(line["pay_url"]))}">Pay</a>'
            if line["pay_url"]
            else ""
        )
        rows.append(
            "<tr>"
            f"<td>{_html.escape(line['ref'])}</td>"
            f"<td>{_html.escape(line['due_date'])}</td>"
            f"<td style='text-align:right'>{money(line['outstanding'])}</td>"
            f"<td>{line['days_past']}d</td>"
            f"<td>{pay_cell}</td>"
            "</tr>"
        )
    table = "".join(rows)
    return f"""
<div style="font-family:Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;
            color:#1a1a1a;background:#faf9f7;padding:32px">
  <p style="font-size:15px;margin:0 0 24px">Dear {_html.escape(merchant_name)} accounts team,</p>
  <p style="font-size:15px">Payment is outstanding on the following
     {_html.escape(str(statement["count"]))} invoice(s):</p>
  <table style="border-collapse:collapse;width:100%;font-size:14px">
    <tr style="border-bottom:1px solid #1a4d2e">
      <th align="left">Invoice</th><th align="left">Due</th>
      <th align="right">Outstanding</th><th>Overdue</th><th></th>
    </tr>
    {table}
    <tr><td colspan="5" style="padding-top:12px;font-weight:600">
      Total outstanding: {money(statement["total_outstanding"])}
    </td></tr>
  </table>
  <p style="margin:24px 0 8px">
    <a href="{_html.escape(link)}"
       style="background:#1a4d2e;color:#faf9f7;padding:12px 24px;
              text-decoration:none;border-radius:6px;display:inline-block">
      View statement &amp; pay
    </a>
  </p>
  <p style="font-size:12px;color:#666;margin-top:24px">
    Sent by a payment-recovery system operated by {_html.escape(merchant_name)}.
    If anything looks wrong, reply to this email.
  </p>
</div>
"""


def compose_stage_message(
    cases: list[dict[str, Any]],
    *,
    tone: str,
    merchant_name: str,
    statement_link: str,
    now: datetime,
) -> dict[str, Any]:
    """Statement + subject + SMS + both email bodies in one call.

    The single entry point the integration phase calls from a ladder rung:
    one dict with everything the sender needs, so the sender's interface is
    data-only and remains trivially testable.
    """
    statement = statement_lines(cases, now=now)
    return {
        "statement": statement,
        "subject": render_subject(statement, tone=tone, merchant_name=merchant_name),
        "sms": render_sms(
            statement, tone=tone, merchant_name=merchant_name, link=statement_link
        ),
        "email_text": render_email_text(
            statement, tone=tone, merchant_name=merchant_name, link=statement_link
        ),
        "email_html": render_email_html(
            statement, tone=tone, merchant_name=merchant_name, link=statement_link
        ),
    }
