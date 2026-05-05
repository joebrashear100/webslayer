from datetime import date

PORTFOLIO = {
    "positions": {
        "MNKD": {
            "shares": 103.5663,
            "exit_date": "2026-05-29",
            "exit_rule": "PDUFA hard exit — approval: sell 50-75% into spike; rejection: sell 100% immediately",
            "auto_invest": 15.00,
            "thesis": "Pediatric Afrezza label expansion binary",
        },
        "TTD": {
            "shares": 29.6854,
            "thesis": "Tech dislocation — programmatic ad recovery + CTV",
            "catalyst": "2026-05-07",
        },
        "GEV": {
            "shares": 0.4835,
            "thesis": "AI power infrastructure slow drip",
            "auto_invest": 10.00,
        },
        "SNDK": {
            "shares": 0.9507,
            "thesis": "NAND supercycle + HBF post-trim ride",
        },
        "VIST": {
            "shares": 6.7042,
            "thesis": "Vaca Muerta shale macro play",
        },
    },
    "pending_entry": {
        "VRDN": {
            "target_shares": 150,
            "entry_window_start": "2026-05-11",
            "entry_window_end": "2026-05-15",
            "pdufa": "2026-06-30",
            "thesis": "Veligrotug TED FDA binary — 9/12 score",
        }
    },
    "hard_rules": [
        "Never sell VOO or SCHG under any circumstances",
        "No position > 25% of spec book (~$2,125)",
        "Maximum 2 unresolved binary positions concurrently",
        "No margin ever",
        "Pre-committed exits are inviolable — agent cannot override",
    ],
    "spec_book_size": 8500,
}

# Convenience: all tracked tickers (positions + pending)
ALL_TICKERS = list(PORTFOLIO["positions"].keys()) + list(PORTFOLIO["pending_entry"].keys())
POSITION_TICKERS = list(PORTFOLIO["positions"].keys())
