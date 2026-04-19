"""
Lineup optimizer for fantasy baseball - ensures starting pitchers aren't left on the bench.
"""

from datetime import datetime
from typing import Optional

from yahoo_api import YahooFantasyAPI


PITCHING_POSITIONS = {'SP', 'RP', 'P'}
SP_POSITIONS = {'SP', 'P'}  # Slots we want to fill with starting SPs when they pitch


def _parse_roster_player(player_data: dict) -> Optional[dict]:
    """Extract player_key, name, position, selected_position from Yahoo roster entry.
    selected_position includes BN, SP, NA (Not Active / minors stash), etc."""
    player_key = None
    name = "Unknown"
    position = "Unknown"
    selected_position = "Unknown"

    for element in player_data.get("player", []):
        if isinstance(element, list):
            for item in element:
                if isinstance(item, dict):
                    if "player_key" in item:
                        player_key = item["player_key"]
                    elif "name" in item:
                        name = item["name"].get("full", "Unknown")
                    elif "display_position" in item:
                        position = item["display_position"]
        elif isinstance(element, dict) and "selected_position" in element:
            sp = element["selected_position"]
            if isinstance(sp, list):
                for p in sp:
                    if isinstance(p, dict) and "position" in p:
                        selected_position = p["position"]
                        break
            elif isinstance(sp, dict) and "position" in sp:
                selected_position = sp["position"]

    if not player_key:
        return None
    return {
        "player_key": player_key,
        "name": name,
        "position": position,
        "selected_position": selected_position,
    }


def get_optimal_lineup_changes(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
) -> tuple[list[tuple[str, str]], list[dict]]:
    """
    Compute lineup changes to ensure SPs who are starting today are in active slots.

    Returns:
        (position_changes, details): List of (player_key, new_position) and human-readable details
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    roster_raw = api.get_team_roster(team_key, date=date)
    if not roster_raw:
        return [], []

    players = []
    for key, val in roster_raw.items():
        if key.isdigit():
            p = _parse_roster_player(val)
            if p:
                players.append(p)

    # Identify SPs and their starting status
    sps = [p for p in players if "SP" in p.get("position", "") or p.get("selected_position") == "SP"]
    sps_on_bench = [p for p in sps if p["selected_position"] == "BN"]
    sps_active = [p for p in sps if p["selected_position"] != "BN"]

    # Check who's actually starting today
    starting_today = []
    not_starting_today = []
    for p in sps:
        info = api.is_player_starting(p["player_key"], date, verbose=False)
        if info and info.get("is_starting"):
            starting_today.append(p)
        else:
            not_starting_today.append(p)

    # Build changes: move starting SPs from BN to SP slots, move non-starting from SP to BN
    changes = []
    details = []

    # SPs starting today on bench -> move to SP
    starting_on_bench = [p for p in starting_today if p["selected_position"] == "BN"]
    # SPs not starting in active slots -> move to BN
    not_starting_active = [p for p in not_starting_today if p["selected_position"] != "BN"]

    # We need to swap: each starting-on-bench needs an SP slot; each not-starting-active frees one
    sp_slots_to_fill = len(starting_on_bench)
    sp_slots_freed = len(not_starting_active)

    # Move non-starting SPs to BN first (free up slots)
    for p in not_starting_active:
        changes.append((p["player_key"], "BN"))
        details.append({"action": "bench", "player": p["name"], "reason": "Not starting today"})

    # Move starting SPs from BN to SP
    for p in starting_on_bench:
        changes.append((p["player_key"], "SP"))
        details.append({"action": "start", "player": p["name"], "reason": "Starting today"})

    # For roster PUT we need ALL players with position changes - Yahoo requires full roster?
    # From the docs: "You may move as many players as you like in your input XML – any players
    # whose position you do not change will stay in the same position they were previously."
    # So we only need to send the players we're changing. Good.

    return changes, details


def optimize_lineup(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """
    Optimize lineup and optionally apply changes.

    Args:
        api: YahooFantasyAPI instance
        team_key: Yahoo team key
        date: Date (YYYY-MM-DD), defaults to today
        dry_run: If True, only return recommendations; if False, apply via edit_roster

    Returns:
        dict with keys: changes, details, applied, error
    """
    date = date or datetime.now().strftime("%Y-%m-%d")
    changes, details = get_optimal_lineup_changes(api, team_key, date)

    result = {"changes": changes, "details": details, "applied": False, "error": None}

    if not changes:
        return result

    if dry_run:
        return result

    resp = api.edit_roster(team_key, date, changes)
    if resp is not None:
        result["applied"] = True
    else:
        result["error"] = "Failed to apply lineup changes"

    return result
