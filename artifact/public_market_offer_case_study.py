#!/usr/bin/env python3
"""Public marketplace offer case study for data-product price scale.

The rows are public AWS Marketplace / AWS Data Exchange product pages observed
on 2026-06-08.  They are not LDP products and are not evidence of fraud.  The
purpose is only to anchor the paper's normalized utility scale to public annual
data-product contract magnitudes.
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path


BASE = Path(__file__).resolve().parent
OUT_OFFERS_CSV = BASE / "public_market_offer_case_study_offers.csv"
OUT_OFFERS_JSON = BASE / "public_market_offer_case_study_offers.json"
OUT_RISK_CSV = BASE / "public_market_offer_case_study_risk.csv"
OUT_RISK_JSON = BASE / "public_market_offer_case_study_risk.json"


OFFERS = [
    {
        "product": "Thinknum: Government Contracts",
        "provider": "BattleFin",
        "marketplace": "AWS Marketplace / AWS Data Exchange",
        "category": "business/public-sector observations",
        "annual_price_usd": 16800,
        "dimension": "ProductAccess, 12-month contract",
        "url": "https://aws.amazon.com/marketplace/pp/prodview-k3lcheknwwmj4",
    },
    {
        "product": "ChinaScope: Smart Tag",
        "provider": "BattleFin",
        "marketplace": "AWS Marketplace / AWS Data Exchange",
        "category": "financial insights / web observations",
        "annual_price_usd": 50000,
        "dimension": "ProductAccess, 12-month contract",
        "url": "https://aws.amazon.com/marketplace/pp/prodview-pvbx46lgomqc6",
    },
    {
        "product": "Quant IP: Patent Dataset",
        "provider": "BattleFin",
        "marketplace": "AWS Marketplace / AWS Data Exchange",
        "category": "patent / financial insights",
        "annual_price_usd": 59000,
        "dimension": "ProductAccess, 12-month contract",
        "url": "https://aws.amazon.com/marketplace/pp/prodview-cdy7bjam7vvkm",
    },
    {
        "product": "Greenwich.HR: Labor Market Intelligence Data",
        "provider": "BattleFin",
        "marketplace": "AWS Marketplace / AWS Data Exchange",
        "category": "labor market intelligence",
        "annual_price_usd": 85000,
        "dimension": "ProductAccess, 12-month contract",
        "url": "https://aws.amazon.com/marketplace/pp/prodview-t5fvgfurgypxg",
    },
]

HAIRCUTS = [0.10, 0.25, 0.50]


def currency(value: float) -> str:
    return f"${value:,.0f}"


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    offer_rows = [dict(row, observed_date="2026-06-08") for row in OFFERS]
    risk_rows = []
    for offer in OFFERS:
        for haircut in HAIRCUTS:
            risk_rows.append(
                {
                    "product": offer["product"],
                    "annual_price_usd": offer["annual_price_usd"],
                    "quality_haircut_fraction": haircut,
                    "contract_value_at_risk_usd": round(offer["annual_price_usd"] * haircut, 2),
                }
            )

    prices = [row["annual_price_usd"] for row in OFFERS]
    summary = {
        "artifact": "public_market_offer_case_study",
        "description": "public data-product offer price scale and transparent quality-haircut amounts",
        "observed_date": "2026-06-08",
        "limitations": [
            "public listings are not LDP products",
            "public listings are not evidence of fraud or over-claiming",
            "quality haircuts are transparent scale anchors, not calibrated market damages",
        ],
        "offer_count": len(OFFERS),
        "price_min_usd": min(prices),
        "price_median_usd": statistics.median(prices),
        "price_max_usd": max(prices),
        "price_min_label": currency(min(prices)),
        "price_median_label": currency(statistics.median(prices)),
        "price_max_label": currency(max(prices)),
        "risk_at_25pct_min_usd": min(prices) * 0.25,
        "risk_at_25pct_median_usd": statistics.median(prices) * 0.25,
        "risk_at_25pct_max_usd": max(prices) * 0.25,
        "risk_at_25pct_min_label": currency(min(prices) * 0.25),
        "risk_at_25pct_median_label": currency(statistics.median(prices) * 0.25),
        "risk_at_25pct_max_label": currency(max(prices) * 0.25),
        "offers": offer_rows,
        "risk_rows": risk_rows,
    }

    write_csv(OUT_OFFERS_CSV, offer_rows)
    write_csv(OUT_RISK_CSV, risk_rows)
    OUT_OFFERS_JSON.write_text(json.dumps({"offers": offer_rows, "summary": summary}, indent=2) + "\n", encoding="utf-8")
    OUT_RISK_JSON.write_text(json.dumps({"risk_rows": risk_rows, "summary": summary}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
