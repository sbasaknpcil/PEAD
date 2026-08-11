"""Extract Revenue / EBITDA / Net Profit / EPS (latest, prior, year-ago quarter)
directly from a company's actual NSE results filing PDF — no dependency on the
FinanciallyFreeBot card at all.

The PDF's own text layer is corrupted (broken embedded-font CID mapping, at
least for filings tested) — pypdf/pdfplumber pull out garbled labels and even
some garbled numbers. The rendered pixels are clean, so this renders each page
to an image (PyMuPDF) and OCRs it (pytesseract), same technique as
vision_parser.py uses for the PEAD cards, just applied to a real financial
statement table instead of a stylized card.

Indian listed companies file in a fairly standardized Reg. 33 layout: a table
with "Particulars" rows and four value columns — quarter ended (current),
quarter ended (immediately preceding = QoQ), quarter ended (year-ago = YoY),
and full accounting year. Only the first three columns are used here.
"""
import logging
import re

import fitz
import pytesseract
from PIL import Image

log = logging.getLogger("nse_result_parser")

NUMBER_RE = re.compile(r"^\(?-?[\d,]+\.?\d*\)?$")

ROW_LABELS = {
    "revenue": ["total income from operations", "revenue from operations"],
    # Each filer phrases this differently — "Profit before Exceptional Items and
    # Tax" / "Profit/(Loss) before Exceptional Items and Tax" (slash breaks a
    # contiguous-phrase match) / singular "item". Match on co-occurrence
    # instead of one exact phrase — see _find_row's tuple-fragment support.
    "pbt": [("profit", "exceptional item"), "profit from before tax"],
    "finance_cost": ["finance cost"],
    "depreciation": ["depreciation and amortis", "depreciation and amortiz"],  # British/American spelling both seen
    # "Net Profit /loss for the period" / "profit for the year (vii-viii)" (no
    # "net" at all) / OCR splitting "Profit" oddly — short fragments, several filers seen.
    "net_profit": ["net profi", "profit for the period", "profit for the year"],
    "eps_basic": ["basic"],
}


MAX_SCAN_PAGES = 20
SCAN_DPI = 200  # fast pass just to find the right page; full extraction re-renders at 500dpi
MIN_TABLE_DECIMALS = 20  # floor to bother trying a page at all; real tables run 100+, seen a notes-page false positive at 23
DECIMAL_RE = re.compile(r"\d+\.\d+")


def candidate_pages_by_decimal_density(pdf_path):
    """Returns page indices ranked by decimal-number count, descending — a real
    financial table has 100+ decimal numbers; notes/cover-letter pages have
    single digits to a few dozen (dates, section references, the occasional
    OCR artifact). No keyword requirement at all: every keyword tried so far
    ("PARTICULARS", "QUARTER ENDED", "CONSOLIDATED"/"STANDALONE") has produced
    a false negative on at least one real filer's page or a false positive on
    a notes page — decimal density is the one signal that's held up across
    three different companies' filings. Doesn't try to pick standalone vs.
    consolidated here; extract_quarters() tries candidates in this order and
    keeps whichever one actually yields all required rows, which is a
    stronger correctness check than any page-level heuristic."""
    doc = fitz.open(pdf_path)
    n_pages = min(doc.page_count, MAX_SCAN_PAGES)

    scored = []
    for i in range(n_pages):
        pix = doc[i].get_pixmap(dpi=SCAN_DPI)
        mode = "RGB" if pix.n < 4 else "RGBA"
        image = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        text = pytesseract.image_to_string(image, config="--psm 11")
        count = len(DECIMAL_RE.findall(text))
        if count >= MIN_TABLE_DECIMALS:
            scored.append((count, i))

    scored.sort(reverse=True)
    return [i for _, i in scored]


def render_page(pdf_path, page_index, dpi=500):
    doc = fitz.open(pdf_path)
    pix = doc[page_index].get_pixmap(dpi=dpi)
    mode = "RGB" if pix.n < 4 else "RGBA"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _ocr_words(image):
    # psm 11 (sparse text) reliably catches rows psm 6 (uniform block) drops
    # on this table layout — verified against Finance Cost/Depreciation rows.
    data = pytesseract.image_to_data(image, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if text and int(data["conf"][i]) > 0:
            words.append({
                "text": text,
                "x": data["left"][i], "y": data["top"][i],
                "w": data["width"][i], "h": data["height"][i],
            })
    return words


def _cluster_rows(words, y_tolerance=12):
    """Groups words into rows by y-center proximity, each row sorted left to right."""
    rows = []
    for word in sorted(words, key=lambda w: w["y"]):
        cy = word["y"] + word["h"] / 2
        placed = False
        for row in rows:
            row_cy = sum(w["y"] + w["h"] / 2 for w in row) / len(row)
            if abs(cy - row_cy) < y_tolerance:
                row.append(word)
                placed = True
                break
        if not placed:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x"])
    return rows


def _row_text(row):
    return " ".join(w["text"] for w in row).lower()


def _parse_number(text):
    cleaned = text.strip().strip("|[]{}").replace(",", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if not NUMBER_RE.match(cleaned.strip("()")) and not cleaned.replace(".", "").replace("-", "").isdigit():
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _numbers_in_row(row):
    numbers = []
    for w in row:
        val = _parse_number(w["text"])
        if val is not None:
            numbers.append(val)
    return numbers


def _fragment_matches(text, fragment):
    """A fragment is either a string (simple substring match) or a tuple of
    strings (all must be present — for labels that vary too much for one
    contiguous phrase to match, e.g. "profit"+"exceptional item" co-occurring
    regardless of exact wording between them)."""
    if isinstance(fragment, tuple):
        return all(part in text for part in fragment)
    return fragment in text


def _find_row(rows, label_fragments):
    for row in rows:
        text = _row_text(row)
        if any(_fragment_matches(text, frag) for frag in label_fragments):
            return row
    return None


def _extract_from_page(pdf_path, page_index):
    """Tries to pull all 6 required rows from one page. Returns the row dict
    list, or raises ValueError naming what's missing — callers decide whether
    to try the next candidate page or give up."""
    image = render_page(pdf_path, page_index)
    words = _ocr_words(image)
    rows = _cluster_rows(words)

    revenue_row = _find_row(rows, ROW_LABELS["revenue"])
    pbt_row = _find_row(rows, ROW_LABELS["pbt"])
    finance_row = _find_row(rows, ROW_LABELS["finance_cost"])
    dep_row = _find_row(rows, ROW_LABELS["depreciation"])
    profit_row = _find_row(rows, ROW_LABELS["net_profit"])
    eps_row = _find_row(rows, ROW_LABELS["eps_basic"])

    missing = [name for name, row in [
        ("revenue", revenue_row), ("PBT", pbt_row), ("finance cost", finance_row),
        ("depreciation", dep_row), ("net profit", profit_row), ("EPS basic", eps_row),
    ] if row is None]
    if missing:
        raise ValueError(f"Could not locate rows: {missing} (page {page_index})")

    def q3(row):
        nums = _numbers_in_row(row)[:3]
        while len(nums) < 3:
            nums.append(None)
        return nums

    rev = q3(revenue_row)
    pbt = q3(pbt_row)
    fin = q3(finance_row)
    dep = q3(dep_row)
    profit = q3(profit_row)
    eps = q3(eps_row)

    ebitda_abs = [
        (pbt[i] + fin[i] + dep[i]) if None not in (pbt[i], fin[i], dep[i]) else None
        for i in range(3)
    ]
    ebitda_margin = [
        round(ebitda_abs[i] / rev[i] * 100, 2) if ebitda_abs[i] is not None and rev[i] else None
        for i in range(3)
    ]

    def row_dict(metric, values):
        return {"metric": metric, "latest_quarter": values[0], "prior_quarter": values[1], "year_ago_quarter": values[2]}

    return [
        row_dict("Revenue", rev),
        row_dict("EBITDA", ebitda_margin),
        row_dict("Net Profit", profit),
        row_dict("EPS", eps),
    ]


def sanity_check(financials):
    """Catches the dangerous failure mode: a row matched and numbers parsed,
    but the wrong numbers (Quality Power's Net Profit came back 370.64/1855.47
    against an actual 51/37 — plausible-*looking*, not plausible). Returns
    (ok, reason). Cheap, always-true-for-real-companies invariants only:
      - every quarter's Revenue/EBITDA/Net Profit/EPS must be present (a row
        being found but yielding fewer than 3 numbers is itself suspect)
      - |Net Profit| < Revenue for every quarter (profit exceeding revenue is
        essentially never real; this alone would have caught Quality Power)
      - EBITDA margin within -100%..100% (a derived value outside that range
        means one of PBT/Finance Cost/Depreciation was misread)
    Not a full audit — a wrong number that happens to satisfy all three could
    still slip through. It's a floor, not a guarantee."""
    by_metric = {f["metric"]: f for f in financials}
    for quarter in ("latest_quarter", "prior_quarter", "year_ago_quarter"):
        values = {m: by_metric[m][quarter] for m in ("Revenue", "EBITDA", "Net Profit", "EPS")}
        if any(v is None for v in values.values()):
            return False, f"missing value(s) for {quarter}: {[m for m, v in values.items() if v is None]}"
        if abs(values["Net Profit"]) >= values["Revenue"]:
            return False, f"{quarter}: |Net Profit| ({values['Net Profit']}) >= Revenue ({values['Revenue']}) — implausible"
        if not (-100 <= values["EBITDA"] <= 100):
            return False, f"{quarter}: EBITDA margin {values['EBITDA']}% outside plausible range"
    return True, None


def extract_quarters(pdf_path):
    """Returns a dict shaped like vision_parser's `financials` list: Revenue,
    EBITDA (margin %), Net Profit, EPS — each with latest_quarter/prior_quarter/
    year_ago_quarter. Tries candidate pages highest-decimal-density first,
    keeping the first one that yields all 6 required rows AND passes
    sanity_check — this also settles consolidated-vs-standalone implicitly
    (whichever page actually works). Raises ValueError if no candidate page
    works (fail loudly rather than silently returning partial/wrong data that
    would feed a buy decision)."""
    candidates = candidate_pages_by_decimal_density(pdf_path)
    if not candidates:
        raise ValueError(f"No page with enough numeric density to be a results table in {pdf_path}")

    errors = []
    for page_index in candidates:
        try:
            financials = _extract_from_page(pdf_path, page_index)
        except ValueError as e:
            errors.append(str(e))
            continue

        ok, reason = sanity_check(financials)
        if not ok:
            errors.append(f"page {page_index} extracted but failed sanity check: {reason}")
            continue

        return financials

    raise ValueError(f"No candidate page in {pdf_path} yielded all required rows: {errors}")
