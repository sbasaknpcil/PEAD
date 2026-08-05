import re

import pytesseract
from PIL import Image

NUMBER_RE = re.compile(r"^-?[\d,]+\.?\d*%?$")
QUARTER_RE = re.compile(r"Q[1-4l]\s*FY\s*(\d{2})", re.IGNORECASE)

METRIC_LABELS = ["Revenue", "EBITDA", "PBT", "Profit", "EPS"]


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


def _number_below_label(words, label_text, max_y_gap=150):
    label = next((w for w in words if w["text"].lower() == label_text.lower()), None)
    if label is None:
        return None
    candidates = [
        w
        for w in words
        if NUMBER_RE.match(w["text"])
        and w["y"] > label["y"]
        and w["y"] - label["y"] < max_y_gap
        and abs(_center_x(w) - _center_x(label)) < label["w"] * 3
    ]
    if not candidates:
        return None
    closest = min(candidates, key=lambda w: w["y"])
    return _parse_number(closest["text"])


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

    return {
        "company_name": _find_company_name(words),
        "nse_ticker": _find_ticker(words),
        "quarter": _find_quarter(words),
        "pead_score": _number_below_label(words, "SCORE"),
        "fwd_pe": _number_below_label(words, "PE"),
        "financials": _extract_financials(words),
        "last_price": None,
        "price_change_1d_pct": None,
        "price_change_1y_pct": None,
        "price_change_3y_pct": None,
    }
