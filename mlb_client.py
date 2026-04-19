"""
MLB Stats API client for transaction data (call-ups, roster moves).
Uses the free, public statsapi.mlb.com API - no API key required.
"""

import requests
from datetime import datetime, timedelta
from typing import Optional

# MLB team IDs for the 30 major league clubs (sportId=1)
# Used to filter call-ups (player promoted TO an MLB team)
MLB_TEAM_IDS = {
    108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120,
    121, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143, 144,
    145, 146, 147, 158
}

# Transaction type codes: REC=Recalled, RV=Reinstated from IL
# OPT=Optioned (demotion - exclude)
CALLUP_TYPE_CODES = {'REC', 'RV'}


def get_player_age_lookup(season: int = 2026) -> dict:
    """
    Fetch all active MLB players for a season and return a name -> age info dict.
    Single API call — use this to enrich Yahoo player lists with age data.

    Returns:
        dict mapping lowercase full_name -> {
            "age": int,
            "birth_date": str (YYYY-MM-DD),
            "mlb_id": int,
        }
        Also keyed by "firstname lastname" variants for fuzzy name matching.
    """
    url = f"https://statsapi.mlb.com/api/v1/sports/1/players"
    try:
        response = requests.get(url, params={"season": season}, timeout=15)
        response.raise_for_status()
        people = response.json().get("people", [])
    except requests.exceptions.RequestException as e:
        print(f"MLB API request failed (age lookup): {e}")
        return {}

    lookup = {}
    for p in people:
        age = p.get("currentAge")
        birth_date = p.get("birthDate", "")
        mlb_id = p.get("id")
        full_name = p.get("fullName", "")
        if not full_name or age is None:
            continue
        entry = {"age": age, "birth_date": birth_date, "mlb_id": mlb_id}
        lookup[full_name.lower()] = entry
        # Also index by ascii-simplified name for accent handling
        ascii_name = _ascii_normalize(full_name)
        if ascii_name != full_name.lower():
            lookup[ascii_name] = entry

    return lookup


def _ascii_normalize(name: str) -> str:
    """Lowercase and strip common accent characters for fuzzy name matching."""
    replacements = {
        "á": "a", "à": "a", "ä": "a", "â": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "ô": "o", "ö": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ñ": "n", "ç": "c",
    }
    result = name.lower()
    for accented, plain in replacements.items():
        result = result.replace(accented, plain)
    return result


def get_transactions(date: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None):
    """
    Fetch MLB transactions for a given date or date range.

    Args:
        date: Single date YYYY-MM-DD
        start_date: Start of range (use with end_date)
        end_date: End of range (use with start_date)

    Returns:
        list: Transaction objects from the API
    """
    url = "https://statsapi.mlb.com/api/v1/transactions"
    params = {}

    if date:
        params["date"] = date
    elif start_date and end_date:
        params["startDate"] = start_date
        params["endDate"] = end_date
    else:
        params["date"] = datetime.now().strftime("%Y-%m-%d")

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("transactions", [])
    except requests.exceptions.RequestException as e:
        print(f"MLB API request failed: {e}")
        return []


def get_recent_callups(days: int = 3) -> list[dict]:
    """
    Get players recently called up to the majors (promoted from minors to MLB).

    Returns:
        list: Dicts with keys: full_name, mlb_id, team_name, date, description, type_code
    """
    callups = []
    start = datetime.now() - timedelta(days=days)
    end = datetime.now()

    for i in range((end - start).days + 1):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        transactions = get_transactions(date=d)

        for txn in transactions:
            txn_type = txn.get("typeCode", "")
            to_team = txn.get("toTeam", {})
            to_team_id = to_team.get("id")
            from_team = txn.get("fromTeam", {})

            # Must be promoted TO an MLB team
            if to_team_id not in MLB_TEAM_IDS:
                continue

            # Only include recall/reinstate types (promotions to MLB)
            if txn_type not in CALLUP_TYPE_CODES:
                continue
            person = txn.get("person", {})
            full_name = person.get("fullName", "Unknown")
            mlb_id = person.get("id")
            team_name = to_team.get("name", "Unknown")
            description = txn.get("description", "")

            callups.append({
                "full_name": full_name,
                "mlb_id": mlb_id,
                "team_name": team_name,
                "date": d,
                "description": description,
                "type_code": txn_type,
            })

    return callups
