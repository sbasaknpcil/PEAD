"""Does my independent score actually predict Earnings Pulse's 'Excellent' tag?
Regression + correlation analysis, not just eyeballing a table. Earnings Pulse
ratings below are hardcoded from pead_fetch_earnings_pulse_history.py's 2026-08-07
pull (immutable historical data, no need to re-hit Telegram for this).

Small-sample caveat is real: n=20 with 4 candidate features is well below the
~10 samples/feature rule of thumb for a stable multivariate fit. Univariate
correlations are the primary signal here; the multivariate logistic fit is
reported for direction/plausibility only, not as a trustworthy weight scheme.
"""
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import LeaveOneOut, cross_val_predict

import pead_rate_results as rate_results

EARNINGS_PULSE_RATINGS = {
    "ARVSMART": "Excellent", "EMIL": "Good", "CUPID": "Good", "POKARNA": "Excellent",
    "PIXTRANS": "Good", "UNIVCABLES": "Excellent", "IMAGICAA": "Excellent",
    "PASUPTAC": "Good", "ULTRAMAR": "Excellent", "GOLDIAM": "Great", "HINDALCO": "Excellent",
    "GREENLAM": "Weak", "ELLEN": "Excellent", "GOPAL": "Good", "RGL": "Weak",
    "OIL": "Excellent", "AARTIPHARM": "Good", "PRSMJOHNSN": "Good", "PRADPME": "OK",
    "ADVAIT": "OK",
    # TRANSWORLD: not covered by Earnings Pulse that day — excluded, not imputed.
}
ORDINAL = {"Weak": 1, "OK": 2, "Good": 3, "Great": 4, "Excellent": 5}


def bare_ticker(symbol):
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "")


def build_dataset():
    cards = rate_results.load_cards("downloaded_cards/photo_2026-08-07_*.jpg")
    rows = []
    for card in cards:
        # Guidance pillar has only 3-6/20 non-null rows regardless (can't test
        # correlation on it either way) and screener.in already rate-limited us
        # once this session — skip it here to keep this rerun clean and fast.
        r = rate_results.rate_card(card, check_guidance=False)
        ticker = bare_ticker(r["symbol"])
        pulse = EARNINGS_PULSE_RATINGS.get(ticker)
        if pulse is None:
            continue
        rows.append({
            "company": r["company"],
            "ticker": ticker,
            "financials": r["financials_score"],
            "guidance": r["guidance_score"],
            "market": r["market_score"],
            "technical": r["technical_score"],
            "composite": r["composite"],
            "pulse_rating": pulse,
            "pulse_ordinal": ORDINAL[pulse],
            # "Excellent" is the top tier (confirmed), not "Excellent or Great" —
            # match the literal tag, not a merged top-two-tiers bucket.
            "pulse_excellent": 1 if pulse == "Excellent" else 0,
        })
    return pd.DataFrame(rows)


def univariate_correlations(df):
    print("=== Spearman correlation: each pillar vs Earnings Pulse ordinal tier ===")
    print("(rank correlation, not Pearson — the target is ordinal categories, not a continuum)\n")
    for col in ["financials", "guidance", "market", "technical", "composite"]:
        sub = df[[col, "pulse_ordinal"]].dropna()
        if len(sub) < 5:
            print(f"  {col:12s}: n={len(sub)} too few non-null rows to correlate")
            continue
        rho, p = stats.spearmanr(sub[col], sub["pulse_ordinal"])
        print(f"  {col:12s}: rho={rho:+.2f}  p={p:.3f}  n={len(sub)}")
    print()


def point_biserial_vs_excellent_tag(df):
    print("=== Point-biserial correlation: each pillar vs binary 'is Excellent' (top tier, exact) ===\n")
    for col in ["financials", "guidance", "market", "technical", "composite"]:
        sub = df[[col, "pulse_excellent"]].dropna()
        if len(sub) < 5 or sub["pulse_excellent"].nunique() < 2:
            print(f"  {col:12s}: n={len(sub)}, insufficient variation")
            continue
        r, p = stats.pointbiserialr(sub["pulse_excellent"], sub[col])
        print(f"  {col:12s}: r={r:+.2f}  p={p:.3f}  n={len(sub)}")
    print()


def logistic_fit(df):
    print("=== Logistic regression: financials + market + technical -> P(Excellent, exact top tier) ===")
    print("(guidance excluded from the multivariate fit — only 6/20 rows have it, too sparse)\n")

    features = ["financials", "market", "technical"]
    sub = df[features + ["pulse_excellent"]].dropna()
    X = sub[features].values
    y = sub["pulse_excellent"].values
    print(f"n={len(sub)}, positive class (Excellent)={y.sum()}, negative={len(y) - y.sum()}\n")

    model = LogisticRegression()
    model.fit(X, y)
    for name, coef in zip(features, model.coef_[0]):
        print(f"  coef[{name:10s}] = {coef:+.4f}")
    print(f"  intercept        = {model.intercept_[0]:+.4f}")

    # Leave-one-out CV accuracy — with n=17 this is the only honest way to estimate
    # out-of-sample performance; a plain train/test split would be near-meaningless.
    loo_preds = cross_val_predict(LogisticRegression(), X, y, cv=LeaveOneOut())
    loo_acc = (loo_preds == y).mean()
    baseline_acc = max(y.mean(), 1 - y.mean())
    print(f"\n  Leave-one-out CV accuracy: {loo_acc:.0%}  (baseline, always predict majority class: {baseline_acc:.0%})")
    print("  If LOO accuracy isn't clearly above baseline, the fit isn't earning its keep on this sample size.")
    print()


def top_scorers_hit_rate(df):
    print("=== Do my top scorers actually get 'Excellent' from Earnings Pulse? ===\n")
    ranked = df.sort_values("composite", ascending=False).reset_index(drop=True)
    for n in (5, 10):
        top_n = ranked.head(n)
        hits = top_n["pulse_excellent"].sum()
        print(f"  Top {n} by my composite: {hits}/{n} rated exactly 'Excellent' by Earnings Pulse")
    print()
    print(ranked[["company", "ticker", "composite", "pulse_rating"]].to_string(index=False))


def main():
    pd.set_option("display.width", 160)
    df = build_dataset()
    print(f"Dataset: {len(df)} companies (of 21 total; Transworld Shipping excluded — unrated by Earnings Pulse)\n")

    univariate_correlations(df)
    point_biserial_vs_excellent_tag(df)
    logistic_fit(df)
    top_scorers_hit_rate(df)


if __name__ == "__main__":
    main()
