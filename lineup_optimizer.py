"""
Lineup optimizer for fantasy baseball - ensures starting pitchers aren't left on the bench.
"""

from datetime import datetime
from typing import Optional

from yahoo_api import YahooFantasyAPI


PITCHING_POSITIONS = {'SP', 'RP', 'P'}
SP_POSITIONS = {'SP', 'P'}  # Slots we want to fill with starting SPs when they pitch
BENCH_SLOT = "BN"


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


def _today(date: Optional[str]) -> str:
    return date or datetime.now().strftime("%Y-%m-%d")


def _league_key_from_team_key(team_key: str) -> str:
    if ".t." not in team_key:
        raise ValueError(f"Invalid Yahoo team_key: {team_key}")
    return team_key.split(".t.")[0]


def _normalize_position(pos: str | None) -> str:
    return str(pos or "").strip().upper()


def _is_sp_eligible(player: dict) -> bool:
    eligible = {
        _normalize_position(pos)
        for pos in player.get("eligible_positions") or player.get("positions") or []
    }
    display_position = player.get("display_position")
    if display_position:
        eligible.update(_normalize_position(pos) for pos in str(display_position).split(","))
    return "SP" in eligible


def _starting_pitcher_slots(api: YahooFantasyAPI, team_key: str, players: list[dict]) -> list[str]:
    league_key = _league_key_from_team_key(team_key)
    settings = api.get_league_scoring_settings(league_key) or {}
    roster_positions = settings.get("roster_positions") or []

    slot_inventory: list[str] = []
    for row in roster_positions:
        position = _normalize_position(row.get("position"))
        if position not in SP_POSITIONS:
            continue
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        slot_inventory.extend([position] * count)

    # Fallback to the currently occupied active slots if league settings are unavailable.
    if not slot_inventory:
        slot_inventory = [
            _normalize_position(player.get("selected_position"))
            for player in players
            if _normalize_position(player.get("selected_position")) in SP_POSITIONS
        ]

    return slot_inventory


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
    date = _today(date)
    players = api.get_team_roster_details(team_key, date=date)
    if not players:
        return [], []

    sps = [player for player in players if _is_sp_eligible(player)]
    starting_today = [player for player in sps if player.get("is_starting") is True]
    starting_on_bench = [
        player
        for player in starting_today
        if _normalize_position(player.get("selected_position")) == BENCH_SLOT
    ]
    active_sp_slot_players = [
        player
        for player in sps
        if _normalize_position(player.get("selected_position")) in SP_POSITIONS
    ]

    slot_inventory = _starting_pitcher_slots(api, team_key, players)
    occupied_slots = [
        _normalize_position(player.get("selected_position"))
        for player in active_sp_slot_players
    ]

    remaining_occupied = list(occupied_slots)
    available_slots: list[str] = []
    for slot in slot_inventory:
        if slot in remaining_occupied:
            remaining_occupied.remove(slot)
        else:
            available_slots.append(slot)

    changes = []
    details = []
    swap_candidates = list(active_sp_slot_players)

    for p in starting_on_bench:
        if available_slots:
            target_slot = available_slots.pop(0)
        else:
            swap_candidate = next(
                (
                    player
                    for player in swap_candidates
                    if player.get("is_starting") is not True
                ),
                swap_candidates[0] if swap_candidates else None,
            )
            if not swap_candidate:
                continue

            target_slot = _normalize_position(swap_candidate.get("selected_position")) or "SP"
            changes.append((swap_candidate["player_key"], BENCH_SLOT))
            details.append(
                {
                    "action": "swap_to_bench",
                    "player_key": swap_candidate["player_key"],
                    "player": swap_candidate["name"],
                    "from": target_slot,
                    "to": BENCH_SLOT,
                    "reason": f"Opening {target_slot} for starting pitcher",
                }
            )
            swap_candidates.remove(swap_candidate)

        changes.append((p["player_key"], target_slot))
        details.append(
            {
                "action": "start",
                "player_key": p["player_key"],
                "player": p["name"],
                "from": BENCH_SLOT,
                "to": target_slot,
                "reason": "Starting today",
            }
        )

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
    date = _today(date)
    changes, details = get_optimal_lineup_changes(api, team_key, date)

    result = {"changes": changes, "details": details, "applied": False, "error": None}

    if not changes:
        return result

    if dry_run:
        return result

    resp = api.edit_roster_safe(team_key, date, changes)
    result["apply_result"] = resp
    result["applied_changes"] = resp.get("applied", [])
    result["failed_changes"] = resp.get("failed", [])
    if result["applied_changes"]:
        result["applied"] = True
    else:
        result["error"] = "Failed to apply lineup changes"
    if result["failed_changes"]:
        result["error"] = (
            f"Applied {len(result['applied_changes'])} of {len(changes)} pitcher lineup changes"
            if result["applied_changes"]
            else "Failed to apply lineup changes"
        )

    return result
