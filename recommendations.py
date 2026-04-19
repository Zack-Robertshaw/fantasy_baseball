"""
Recommendation logic for the AI co-manager: call-up alerts (promoted players available as free agents),
league-wide NA stashes, and NA-eligible free-agent adds.
"""
from difflib import SequenceMatcher

from yahoo_api import YahooFantasyAPI
from mlb_client import get_recent_callups
from lineup_optimizer import _parse_roster_player


def _name_similarity(a: str, b: str) -> float:
    """Return similarity ratio between two names (0-1)."""
    a_clean = a.lower().strip()
    b_clean = b.lower().strip()
    if a_clean == b_clean:
        return 1.0
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def _normalize_name(name: str) -> str:
    """Normalize name for matching (e.g. 'Jr.', suffixes)."""
    for suffix in (" Jr.", " Jr", " III", " II", " IV", " Sr."):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def get_callup_recommendations(api: YahooFantasyAPI, league_key: str, days: int = 3) -> list[dict]:
    """
    Find players recently called up to MLB who are available on the waiver wire.

    Returns:
        list: Dicts with yahoo player info + callup context
    """
    callups = get_recent_callups(days=days)
    free_agents = api.get_league_players(league_key, status="FA", count=250)

    if not free_agents:
        return []

    recommendations = []
    for c in callups:
        mlb_name = c["full_name"]
        mlb_name_norm = _normalize_name(mlb_name)

        for fa in free_agents:
            yahoo_name = fa.get("name", "")
            yahoo_name_norm = _normalize_name(yahoo_name)

            sim = _name_similarity(mlb_name_norm, yahoo_name_norm)
            if sim >= 0.85:  # High confidence match
                recommendations.append({
                    "player_key": fa["player_key"],
                    "name": yahoo_name,
                    "positions": fa.get("positions", []),
                    "team": fa.get("team", ""),
                    "callup_date": c["date"],
                    "callup_description": c["description"],
                    "match_confidence": round(sim, 2),
                })
                break

    return recommendations


def _is_na_roster_slot(selected_position: str | None) -> bool:
    """True if Yahoo lineup slot is NA (Not Active / minors stash)."""
    return str(selected_position or "").strip().upper() == "NA"


def _has_na_eligibility(positions: list) -> bool:
    """True if eligible_positions includes NA."""
    if not positions:
        return False
    return any(str(p).strip().upper() == "NA" for p in positions)


def _is_yahoo_na_stash_display(p: dict) -> bool:
    """
    True if Yahoo shows this player as NA (minors / not active), not as an active MLB slot.

    Yahoo's position=NA filter can include players who still have NA eligibility on paper but
    are active MLB (display OF, Util, SP). Those should not appear on NA adds.
    """
    dp = str(p.get("display_position") or "").strip().upper()
    if dp == "NA":
        return True
    # No display_position: only accept if the only listed position is NA
    pos = p.get("positions") or []
    if len(pos) == 1 and str(pos[0]).strip().upper() == "NA":
        return True
    return False


def get_league_na_stashes(api: YahooFantasyAPI, league_key: str) -> list[dict]:
    """
    Every player in an NA roster slot across all teams in the league.
    """
    teams = api.get_league_teams_list(league_key)
    if not teams:
        return []

    out: list[dict] = []
    for t in teams:
        roster = api.get_team_roster(t["team_key"])
        if not roster:
            continue
        for k, v in roster.items():
            if not k.isdigit():
                continue
            p = _parse_roster_player(v)
            if not p:
                continue
            if _is_na_roster_slot(p.get("selected_position")):
                out.append({
                    "team_key": t["team_key"],
                    "team_name": t["name"],
                    "player_key": p["player_key"],
                    "name": p["name"],
                    "display_position": p.get("position"),
                    "selected_position": p.get("selected_position"),
                })

    out.sort(key=lambda x: (x["team_name"].lower(), x["name"].lower()))
    return out


def get_na_add_recommendations(
    api: YahooFantasyAPI,
    league_key: str,
    days: int = 7,
    count: int = 50,
) -> dict:
    """
    NA-eligible free agents sorted by Yahoo overall rank, with MLB call-up urgency overlay.

    Primary: get_league_players(FA, position=NA, sort=OR).
    Fallback: FA sorted by OR, then filter to players with NA in eligible positions.
    """
    callups = get_recent_callups(days=days)

    # Request extra rows so that after display=NA filter we still have up to `count` players.
    fetch_n = min(500, max(count * 8, 80))
    players = api.get_league_players(
        league_key, status="FA", position="NA", sort="OR", sort_type="season", count=fetch_n
    )
    source = "yahoo_na_filter"
    if not players:
        players = api.get_league_players(
            league_key, status="FA", sort="OR", sort_type="season", count=min(500, max(count * 10, 100))
        )
        source = "fallback_eligibility"
        if players:
            players = [p for p in players if _has_na_eligibility(p.get("positions", []))]

    if players:
        players = [p for p in players if _is_yahoo_na_stash_display(p)][:count]

    recommendations: list[dict] = []
    for i, p in enumerate(players or []):
        name = p.get("name", "")
        nn = _normalize_name(name)
        matched = None
        for c in callups:
            if _name_similarity(_normalize_name(c["full_name"]), nn) >= 0.85:
                matched = c
                break
        api_rank = p.get("yahoo_rank")
        if api_rank is not None:
            yahoo_rank = int(api_rank)
            yahoo_rank_source = "yahoo_api"
        else:
            # Yahoo often omits player_ranks on league/players; use position in OR-sorted list
            yahoo_rank = i + 1
            yahoo_rank_source = "ordinal"
        callup_urgency = matched is not None
        row = {
            "player_key": p.get("player_key"),
            "name": p.get("name"),
            "positions": p.get("positions", []),
            "display_position": p.get("display_position"),
            "team": p.get("team"),
            "yahoo_rank": yahoo_rank,
            "yahoo_rank_source": yahoo_rank_source,
            "callup_urgency": callup_urgency,
            "source": source,
        }
        if matched:
            row["callup_date"] = matched.get("date")
            row["callup_description"] = matched.get("description")
        recommendations.append(row)

    return {
        "recommendations": recommendations,
        "source": source,
        "sort": "yahoo_or",
        "callup_days": days,
        "callup_urgency_count": sum(1 for r in recommendations if r.get("callup_urgency")),
    }
