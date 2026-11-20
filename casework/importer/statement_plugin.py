"""Local statement line-extraction plugin for the mixed-PDF counterexample.

The extraction entry point is extract_lines(text): it takes the text of a
single page and returns (entries, rejected) without touching the PDF, so the
same page text can be replayed independently of PDF-to-text conversion.

Row rule (the plugin's column assumption, deliberately narrow):

    MM/DD/YYYY  <description...>  <amount>

where the line starts with an MM/DD/YYYY date and ends with a decimal
amount. Anything else is never turned into an entry. In particular:

  * a page whose text layer contains no amount token at all is reported as
    NO_USABLE_AMOUNT_TEXT -- the money may still be visible in a raster image;
  * a line that does contain an amount but does not match the template is
    reported as LAYOUT_MISMATCH, with the failing condition spelled out, so
    "raw amount present" and "amount accepted by the rule" stay distinct.

A header-only text layer therefore cannot make a scanned page count as
extracted: the header has no amount token and the page is still reported as
NO_USABLE_AMOUNT_TEXT.
"""

import datetime
import decimal
import re

from beancount.core import data

ACCOUNT = "Assets:US:ACMEBank"
CURRENCY = "USD"

AMOUNT_PATTERN = re.compile(r"-?[\d,]+\.\d{2}")
ROW_PATTERN = re.compile(
    r"^(?P<date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<description>.+?)\s+"
    r"(?P<amount>-?[\d,]+\.\d{2})$"
)
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\b")

NO_USABLE_AMOUNT_TEXT = "no_usable_amount_text"
LAYOUT_MISMATCH = "layout_mismatch"
EXTRACTED = "extracted"


def parse_amount(token):
    return decimal.Decimal(token.replace(",", ""))


def extract_lines(text, source="<text>", page=None):
    """Extract entries from one page of text.

    Returns a dict with:
      entries   list of beancount Transaction directives
      rejected  list of {lineno, line, reason, detail} for amount-bearing
                lines the row rule refuses
      status    NO_USABLE_AMOUNT_TEXT, LAYOUT_MISMATCH, or EXTRACTED
    """
    entries = []
    rejected = []
    lines = text.splitlines()

    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or not AMOUNT_PATTERN.search(line):
            continue
        match = ROW_PATTERN.match(line)
        if match:
            date = datetime.datetime.strptime(match.group("date"), "%m/%d/%Y").date()
            amount = data.Amount(parse_amount(match.group("amount")), CURRENCY)
            meta = data.new_metadata(source, lineno)
            if page is not None:
                meta["page"] = str(page)
            entries.append(
                data.Transaction(
                    meta,
                    date,
                    "*",
                    None,
                    match.group("description"),
                    set(),
                    set(),
                    [data.Posting(ACCOUNT, amount, None, None, None, None)],
                )
            )
            continue

        details = []
        if ISO_DATE_PATTERN.match(line):
            details.append("date is ISO YYYY-MM-DD; rule requires MM/DD/YYYY first")
        if AMOUNT_PATTERN.search(line).group(0) != line.split()[-1]:
            details.append("amount is not the last whitespace-delimited column")
        rejected.append({
            "lineno": lineno,
            "line": line,
            "reason": LAYOUT_MISMATCH,
            "detail": "; ".join(details) or "row does not match date/description/amount",
        })

    if not any(AMOUNT_PATTERN.search(line) for line in lines):
        status = NO_USABLE_AMOUNT_TEXT
    elif entries and not rejected:
        status = EXTRACTED
    elif entries:
        status = EXTRACTED
    else:
        status = LAYOUT_MISMATCH
    return {"entries": entries, "rejected": rejected, "status": status}
