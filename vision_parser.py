import base64
import json
import mimetypes

import requests

import config

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

EXTRACTION_PROMPT = """This is a PEAD (Post-Earnings Announcement Drift) result card from a \
stock research Telegram channel. Extract the following as strict JSON:

{
  "company_name": string,
  "nse_ticker": string,          // the NSE symbol shown near the top, e.g. "UNIPARTS"
  "quarter": string,             // e.g. "Q1 FY27"
  "pead_score": number,
  "fwd_pe": number,
  "financials": [
    {"metric": string, "latest_quarter": number|null, "prior_quarter": number|null, "year_ago_quarter": number|null}
    // one row per metric row in the table (Revenue, EBITDA %, PBT, Net Profit, EPS, etc.),
    // in the same left-to-right column order as shown on the card
  ],
  "last_price": number|null,
  "price_change_1d_pct": number|null,
  "price_change_1y_pct": number|null,
  "price_change_3y_pct": number|null
}

If a field is not visible on the card, use null."""


def extract_card(image_path):
    media_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    url = GEMINI_ENDPOINT.format(model=config.GEMINI_MODEL)
    response = requests.post(
        url,
        params={"key": config.GEMINI_API_KEY},
        json={
            "contents": [
                {
                    "parts": [
                        {"inline_data": {"mime_type": media_type, "data": image_b64}},
                        {"text": EXTRACTION_PROMPT},
                    ]
                }
            ],
            "generationConfig": {"response_mime_type": "application/json"},
        },
        timeout=60,
    )
    response.raise_for_status()

    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)
