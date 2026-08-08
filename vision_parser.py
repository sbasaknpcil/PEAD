import logging
import re

import pytesseract
from PIL import Image

log = logging.getLogger("vision_parser")

NUMBER_RE = re.compile(r"^-?[\d,]+\.?\d*%?$")
QUARTER_RE = re.compile(r"Q[1-4l]\s*FY\s*(\d{2})", re.IGNORECASE)

METRIC_LABELS = ["Revenue", "EBITDA", "PBT", "Profit", "EPS"]

# PEAD score is documented/observed as a 0-100 scale. OCR occasionally
# inserts or misreads a stray digit (e.g. "55.7" -> "595.7"); a score outside
# this range is more likely a misread than a real value, and a bad score
# feeding straight into a buy decision is worse than skipping the card.
PEAD_SCORE_MIN, PEAD_SCORE_MAX = 0, 100


def _words(image):
    data = pytesseract.image_to_data(image, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        if text and int(data["conf"][i]) > 0:
            words.append(
                {
                    "text": text,
                    "x": data["left"][i],
                    "y": data["top"][i],
                    "w": data["width"][i],
                    "h": data["height"][i],
                }
            )
    return words


def _center_x(word):
    return word["x"] + word["w"] / 2


def _center_y(word):
    return word["y"] + word["h"] / 2


def _parse_number(text):
    text = text.strip()
    if text in ("-", "—", "--", ""):
        return None
    cleaned = text.replace(",", "").rstrip("%")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _verified_number(image, word, pad=15):
    """The full-page sparse-text pass (--psm 11) occasionally misreads a digit in
    isolation (observed: a card's "53.3" PEAD score read back as "93.3", a "5"/"9"
    confusion) even though it locates the right word. A second, targeted OCR pass
    cropped to just that word's box with a digit-only whitelist reliably gets it
    right, so it's used to double-check/override numbers the buy decision depends on."""
    box = (
        max(word["x"] - pad, 0),
        max(word["y"] - pad, 0),
        word["x"] + word["w"] + pad,
        word["y"] + word["h"] + pad,
    )
    crop = image.crop(box)
    reocr_text = pytesseract.image_to_string(
        crop, config="--psm 7 -c tessedit_char_whitelist=0123456789.,-%"
    ).strip()
    reocr_value = _parse_number(reocr_text)
    original_value = _parse_number(word["text"])
    if reocr_value is None:
        return original_value
    # The crop's edge can clip a trailing digit ("5.59" -> "5.5"), which still parses
    # cleanly but silently drops information rather than fixing a misread — only trust
    # a re-check that read the same number of digits as the original.
    original_digits = sum(c.isdigit() for c in word["text"])
    reocr_digits = sum(c.isdigit() for c in reocr_text)
    if reocr_digits != original_digits:
        return original_value
    # A crop can also pick up stray marks (an adjacent comma, a border pixel) that the
    # digit-only whitelist turns into a bogus digit — trust the re-check only when it's
    # plausibly the same number (a single misread digit), not a different one entirely.
    same_magnitude = original_value is None or abs(reocr_value) < 1e-9 or abs(original_value) < 1e-9 or (
        0.3 < abs(reocr_value / original_value) < 3
    )
    if not same_magnitude:
        return original_value
    if reocr_value != original_value:
        log.warning(
            "Sparse-pass OCR read %r but targeted re-check read %r for the same word; using the re-check",
            word["text"], reocr_text,
        )
    return reocr_value


def _number_below_label(image, words, label_text, max_y_gap=150, max_x_gap=150):
    label = next((w for w in words if w["text"].lower() == label_text.lower()), None)
    if label is None:
        return None
    candidates = [
        w
        for w in words
        if NUMBER_RE.match(w["text"])
        and w["y"] > label["y"]
        and w["y"] - label["y"] < max_y_gap
        and abs(_center_x(w) - _center_x(label)) < max_x_gap
    ]
    if not candidates:
        return None
    # Closest by horizontal alignment to the label, not by y — two adjacent
    # stat boxes (e.g. PEAD SCORE / FWD PE) can sit at nearly the same y, and
    # x-proximity is what actually identifies which box a number belongs to.
    closest = min(candidates, key=lambda w: abs(_center_x(w) - _center_x(label)))
    return _verified_number(image, closest)


def _find_ticker(words):
    nse = next((w for w in words if w["text"].upper() == "NSE"), None)
    if nse is None:
        return None
    same_row = [w for w in words if abs(_center_y(w) - _center_y(nse)) < nse["h"] and w["x"] > nse["x"]]
    if not same_row:
        return None
    return min(same_row, key=lambda w: w["x"])["text"].upper()


def _find_company_name(words):
    anchor = next((w for w in words if w["text"].lower() in ("consolidated", "standalone")), None)
    if anchor is None:
        return None
    name_words = [w for w in words if 200 < w["y"] < anchor["y"] - 20]
    if not name_words:
        return None
    name_words.sort(key=lambda w: w["x"])
    return " ".join(w["text"] for w in name_words)


def _find_market_cap_cr(words):
    label = next((w for w in words if w["text"].lower() == "mcap"), None)
    if label is None:
        return None
    same_row = [
        w
        for w in words
        if abs(_center_y(w) - _center_y(label)) < label["h"] * 0.8
        and w["x"] > label["x"] + label["w"]
        and NUMBER_RE.match(w["text"])
    ]
    if not same_row:
        return None
    return _parse_number(min(same_row, key=lambda w: w["x"])["text"])


def _find_quarter(words):
    joined = " ".join(w["text"] for w in words)
    match = QUARTER_RE.search(joined)
    if not match:
        return None
    return f"Q1 FY{match.group(1)}"


def _extract_financials(words):
    rows = []
    for label in METRIC_LABELS:
        label_word = next((w for w in words if w["text"].strip(":%").lower() == label.lower()), None)
        if label_word is None:
            continue
        same_row = [
            w
            for w in words
            if abs(_center_y(w) - _center_y(label_word)) < label_word["h"] * 0.8
            and w["x"] > label_word["x"] + label_word["w"]
            and NUMBER_RE.match(w["text"])
        ]
        same_row.sort(key=lambda w: w["x"])
        values = [_parse_number(w["text"]) for w in same_row[:3]]
        while len(values) < 3:
            values.append(None)
        display_name = "Net Profit" if label == "Profit" else label
        rows.append(
            {
                "metric": display_name,
                "latest_quarter": values[0],
                "prior_quarter": values[1],
                "year_ago_quarter": values[2],
            }
        )
    return rows


def extract_card(image_path):
    image = Image.open(image_path)
    words = _words(image)

    pead_score = _number_below_label(image, words, "SCORE")
    if pead_score is not None and not (PEAD_SCORE_MIN <= pead_score <= PEAD_SCORE_MAX):
        log.warning("Implausible PEAD score %s for %s, discarding as a likely OCR misread", pead_score, image_path)
        pead_score = None

    return {
        "company_name": _find_company_name(words),
        "nse_ticker": _find_ticker(words),
        "quarter": _find_quarter(words),
        "pead_score": pead_score,
        "fwd_pe": _number_below_label(image, words, "PE"),
        "market_cap_cr": _find_market_cap_cr(words),
        "financials": _extract_financials(words),
        "last_price": None,
        "price_change_1d_pct": None,
        "price_change_1y_pct": None,
        "price_change_3y_pct": None,
    }
