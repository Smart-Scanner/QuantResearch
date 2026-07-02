"""
Indian Seasonal Intelligence Engine
--------------------------------------
- Maps current month to active Indian seasons/events
- Maps sectors to seasonal boost months
- Returns (score, active_seasons, boost_reasons) per stock
- Pure rule-based — no API needed, runs in microseconds
"""

from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

# Synonym normalization — prevents silent misses from plural/alternate forms
ALIASES = {
    "jewel":             "jewellery",
    "hotel":             "hotels",
    "railway":           "railways",
    "infra":             "infrastructure",
    "consumer durables": "consumer",
    "fertiliser":        "fertilizers",
    "fertilizer":        "fertilizers",
    "automobile":        "auto",
    "automotive":        "auto",
    "pharmaceutical":    "pharma",
    "healthcare":        "pharma",
    "realty":            "real estate",
    "it":                "technology",
    "software":          "technology",
}


def normalize_text(text: str) -> str:
    """Apply alias substitutions so both singular/plural and synonym forms match."""
    t = text.lower()
    for alias, canonical in ALIASES.items():
        t = t.replace(alias, canonical)
    return t

INDIA_SEASONS = {
    1:  ["Budget Season 📋", "Republic Day Rally 🇮🇳"],
    2:  ["Budget Rally 📋", "Valentine Consumer Boost 💝"],
    3:  ["Year-End Results 📊", "Holi FMCG Boost 🎨"],
    4:  ["IPL Season 🏏", "Summer Cooling 🌡️", "Q4 Results"],
    5:  ["Pre-Monsoon Agri Prep 🌱", "Summer Peak"],
    6:  ["Monsoon Arrival ☔", "Kharif Sowing 🌾"],
    7:  ["Monsoon Peak ☔", "Rural Demand Build 🌾"],
    8:  ["Monsoon Late ☔", "Independence Day 🇮🇳", "Rural Income"],
    9:  ["Festive Pre-Season 🎊", "Navratri Prep", "Onam 🌸"],
    10: ["Navratri 🕺", "Dussehra 🎆", "Pre-Diwali Rally 🪔"],
    11: ["Diwali 🪔", "Dhanteras 💛", "Wedding Season Start 💍"],
    12: ["Wedding Season Peak 💍", "Year-End Buying 🎁"],
}

# sector keyword → boost months (1–12)
SECTOR_SEASONAL_BOOST = {
    # Agri (Monsoon + Rabi season)
    "agri":            [5, 6, 7, 8, 9, 10],
    "agrochemicals":   [5, 6, 7, 8],
    "fertilizers":     [4, 5, 6, 7, 8],
    "seeds":           [5, 6, 7],
    "irrigation":      [5, 6, 7, 8],

    # Auto (Festive + post-monsoon rural)
    "auto":            [9, 10, 11, 12, 1],
    "two wheelers":    [9, 10, 11, 12],
    "tractors":        [9, 10, 11, 12, 1],

    # FMCG & Consumer (Festive + wedding)
    "fmcg":            [9, 10, 11, 12],
    "consumer":        [9, 10, 11, 12, 1],
    "jewellery":       [10, 11, 12, 1, 2, 3],
    "apparel":         [10, 11, 12, 1, 2],
    "retail":          [10, 11, 12, 1],

    # Construction & Cement
    "cement":          [1, 2, 3, 10, 11, 12],
    "construction":    [1, 2, 3, 10, 11],
    "real estate":     [10, 11, 12, 1, 2],
    "realty":          [10, 11, 12, 1, 2],
    "paints":          [1, 2, 3, 10, 11],

    # Hotels & Tourism
    "hotels":          [10, 11, 12, 1, 2],
    "tourism":         [10, 11, 12, 1],

    # Media & Entertainment
    "media":           [4, 5, 6, 7, 8],
    "entertainment":   [4, 5, 6, 7],

    # Budget plays (Jan–Mar)
    "defence":         [1, 2, 3],
    "railways":        [1, 2, 3],
    "infra":           [1, 2, 3],
    "infrastructure":  [1, 2, 3],
    "capital goods":   [1, 2, 3],
    "industrial":      [1, 2, 3, 10, 11],

    # Power & Renewables
    "power":           [4, 5, 6, 9, 10],
    "renewable":       [5, 6, 7, 8],

    # Pharma (monsoon illness)
    "pharma":          [6, 7, 8, 9],
    "healthcare":      [6, 7, 8, 9],

    # Banking (Q3 results + festive credit)
    "banking":         [1, 2, 3, 9, 10],
    "finance":         [1, 2, 3, 9, 10],

    # IT (US budget cycle + Q4)
    "it":              [1, 2, 3, 4],
    "technology":      [1, 2, 3, 4],
    "software":        [1, 2, 3, 4],

    # Metals (China PMI cycle)
    "metals":          [3, 4, 5, 9, 10],
    "steel":           [3, 4, 5, 9, 10],
    "mining":          [3, 4, 5],

    # Chemicals
    "chemicals":       [3, 4, 5, 9, 10],

    # Logistics (festive + year-end)
    "logistics":       [9, 10, 11, 12, 1],
}

# Specific event-driven boosts per month
EVENT_BOOSTS = {
    10: [  # Navratri + Dussehra + Pre-Diwali
        ("consumer", "Diwali Demand Surge 🪔", 10),
        ("fmcg", "Diwali Demand Surge 🪔", 10),
        ("retail", "Diwali Demand Surge 🪔", 10),
        ("auto", "Festive Auto Sales 🚗", 8),
        ("jewel", "Dhanteras Gold Rush 💛", 10),
    ],
    11: [  # Diwali + Dhanteras + Wedding
        ("consumer", "Diwali Peak 🪔", 12),
        ("fmcg", "Diwali Peak 🪔", 12),
        ("jewel", "Dhanteras + Wedding 💍", 15),
        ("apparel", "Wedding Season 💍", 10),
        ("hotel", "Wedding Hospitality 💍", 8),
    ],
    12: [  # Wedding Season + Year-End
        ("jewel", "Wedding Season Peak 💍", 12),
        ("apparel", "Wedding Season Peak 💍", 10),
        ("hotel", "Wedding + New Year 🎊", 8),
    ],
    6: [  # Monsoon
        ("agri", "Monsoon Season ☔", 10),
        ("fertilizer", "Kharif Input Season ☔", 10),
        ("pharma", "Monsoon Illness Peak 🏥", 8),
    ],
    7: [  # Monsoon peak
        ("agri", "Monsoon Peak ☔", 8),
        ("pharma", "Monsoon Illness Season 🏥", 8),
    ],
    4: [  # IPL + Summer
        ("media", "IPL Season 🏏", 7),
        ("consumer", "Summer Demand ☀️", 6),
    ],
    5: [  # IPL + Summer
        ("media", "IPL Finale 🏆", 5),
    ],
    1: [  # Budget
        ("defence", "Budget Capex Boost 📋", 8),
        ("infra", "Budget Capex Boost 📋", 8),
        ("railway", "Budget Capex Boost 📋", 8),
    ],
    2: [  # Budget
        ("defence", "Budget Rally 📋", 6),
        ("infra", "Budget Allocation 📋", 6),
    ],
}


def get_seasonal_score(sector: str, industry: str = "") -> tuple:
    """
    Returns (score, active_seasons, boost_reasons).
    sector and industry are matched against SECTOR_SEASONAL_BOOST keywords.
    """
    current_month = datetime.now(IST).month
    active_seasons = INDIA_SEASONS.get(current_month, [])
    score = 0
    reasons = []
    combined = normalize_text(sector + " " + industry)

    # Direct sector boost matching
    for sec_key, boost_months in SECTOR_SEASONAL_BOOST.items():
        if sec_key in combined:
            if current_month in boost_months:
                score += 12
                reasons.append(f"Peak Season: {sec_key.title()}")
            elif (current_month - 1) % 12 + 1 in boost_months:
                score += 5
                reasons.append(f"Entering Season: {sec_key.title()}")

    # Event-driven boosts
    for keyword, reason, boost in EVENT_BOOSTS.get(current_month, []):
        if normalize_text(keyword) in combined:
            score += boost
            if reason not in reasons:
                reasons.append(reason)

    # Cap score
    score = min(score, 30)

    return score, active_seasons, reasons[:4]
