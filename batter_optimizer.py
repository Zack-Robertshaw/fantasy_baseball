"""
Daily batter lineup optimizer based on recent Yahoo ranking windows.
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean, pstdev
from typing import Optional

from yahoo_api import YahooFantasyAPI


PITCHING_POSITIONS = {"SP", "RP", "P"}
LOCKED_SLOTS = {"NA", "IL", "DL", "IR"}
BENCH_SLOT = "BN"
UTILITY_SLOTS = {"UTIL", "UTIL.", "UTILSLOT", "UTILS"}
OUTFIELD_POSITIONS = {"OF", "LF", "CF", "RF"}
BATTING_FLEX_MAP = {
    "UTIL": {"*"},
    "CI": {"1B", "3B"},
    "MI": {"2B", "SS"},
    "OF": OUTFIELD_POSITIONS,
}
NON_SCORING_STAT_IDS = {"60"}  # H/AB display stat is redundant with AVG for scoring


def _today(date: Optional[str]) -> str:
    return date or datetime.now().strftime("%Y-%m-%d")


def _league_key_from_team_key(team_key: str) -> str:
    if ".t." not in team_key:
        raise ValueError(f"Invalid Yahoo team_key: {team_key}")
    return team_key.split(".t.")[0]


def _slot_name(slot: str) -> str:
    value = str(slot or "").strip()
    upper = value.upper()
    if upper in UTILITY_SLOTS:
        return "Util"
    return value


def _normalize_position(pos: str) -> str:
    value = str(pos or "").strip()
    upper = value.upper()
    if upper in UTILITY_SLOTS:
        return "UTIL"
    return upper


def _is_pitcher_only(player: dict) -> bool:
    positions = {_normalize_position(pos) for pos in player.get("eligible_positions") or player.get("positions") or []}
    return bool(positions) and positions.issubset(PITCHING_POSITIONS)


def _is_locked_player(player: dict) -> bool:
    return _normalize_position(player.get("selected_position")) in LOCKED_SLOTS


def _has_game_today(player: dict) -> bool:
    # If Yahoo does not expose opponent data for a player, treat it as "unknown but playable"
    # instead of aggressively benching someone.
    return player.get("has_game_today") is not False


def _parse_roster_slots(roster_positions: list[dict]) -> tuple[list[str], list[str]]:
    active_slots: list[str] = []
    bench_like_slots: list[str] = []
    for row in roster_positions or []:
        position = _slot_name(row.get("position"))
        if not position:
            continue
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue

        norm = _normalize_position(position)
        if norm in PITCHING_POSITIONS:
            continue
        if norm in LOCKED_SLOTS or norm == BENCH_SLOT:
            bench_like_slots.extend([position] * count)
            continue
        active_slots.extend([position] * count)
    return active_slots, bench_like_slots


def _player_can_fill_slot(player: dict, slot: str) -> bool:
    norm_slot = _normalize_position(slot)
    eligible = {_normalize_position(pos) for pos in player.get("eligible_positions") or player.get("positions") or []}
    if not eligible or _is_pitcher_only(player):
        return False
    if norm_slot in LOCKED_SLOTS or norm_slot == BENCH_SLOT:
        return False
    if norm_slot in BATTING_FLEX_MAP:
        allowed = BATTING_FLEX_MAP[norm_slot]
        if "*" in allowed:
            return not eligible.issubset(PITCHING_POSITIONS)
        return bool(eligible & allowed)
    return norm_slot in eligible


def _merge_stat_rosters(
    roster_7d: list[dict],
    roster_30d: list[dict],
) -> list[dict]:
    by_key: dict[str, dict] = {}
    for player in roster_30d:
        player_key = player.get("player_key")
        if player_key:
            by_key[player_key] = dict(player)

    for player in roster_7d:
        player_key = player.get("player_key")
        if not player_key:
            continue
        merged = by_key.get(player_key, {}).copy()
        merged.update(player)
        merged["stats_7d"] = dict(player.get("stats_map") or {})
        if "stats_30d" not in merged:
            merged["stats_30d"] = dict(by_key.get(player_key, {}).get("stats_map") or {})
        by_key[player_key] = merged

    for player in by_key.values():
        if "stats_30d" not in player:
            player["stats_30d"] = dict(player.get("stats_map") or {})

    return sorted(
        by_key.values(),
        key=lambda player: (
            player.get("name", ""),
        ),
    )


def _parse_stat_value(stat_id: str, raw_value: str | None) -> float:
    value = str(raw_value or "").strip()
    if not value or value == "-":
        return 0.0
    if stat_id == "60":
        if "/" not in value:
            return 0.0
        hits, at_bats = value.split("/", 1)
        try:
            hits_value = float(hits)
            at_bats_value = float(at_bats)
        except ValueError:
            return 0.0
        return hits_value / at_bats_value if at_bats_value else 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _scoring_stat_ids(batting_categories: list[dict]) -> list[str]:
    ids: list[str] = []
    has_avg = any(str(row.get("stat_id")) == "3" for row in batting_categories or [])
    for row in batting_categories or []:
        stat_id = str(row.get("stat_id") or "")
        if not stat_id:
            continue
        if stat_id in NON_SCORING_STAT_IDS and has_avg:
            continue
        ids.append(stat_id)
    return ids


def _apply_recent_stat_scores(
    players: list[dict],
    batting_categories: list[dict],
    weight_7d: float,
    weight_30d: float,
) -> list[dict]:
    stat_ids = _scoring_stat_ids(batting_categories)
    if not stat_ids:
        return players

    values_7d: dict[str, list[float]] = {stat_id: [] for stat_id in stat_ids}
    values_30d: dict[str, list[float]] = {stat_id: [] for stat_id in stat_ids}
    for player in players:
        stats_7d = player.get("stats_7d") or {}
        stats_30d = player.get("stats_30d") or {}
        for stat_id in stat_ids:
            values_7d[stat_id].append(_parse_stat_value(stat_id, stats_7d.get(stat_id)))
            values_30d[stat_id].append(_parse_stat_value(stat_id, stats_30d.get(stat_id)))

    baselines_7d = {
        stat_id: (mean(series), pstdev(series) if len(series) > 1 else 0.0)
        for stat_id, series in values_7d.items()
    }
    baselines_30d = {
        stat_id: (mean(series), pstdev(series) if len(series) > 1 else 0.0)
        for stat_id, series in values_30d.items()
    }

    scored_players: list[dict] = []
    for player in players:
        stats_7d = player.get("stats_7d") or {}
        stats_30d = player.get("stats_30d") or {}
        z_scores_7d: dict[str, float] = {}
        z_scores_30d: dict[str, float] = {}
        score_7d = 0.0
        score_30d = 0.0
        for stat_id in stat_ids:
            value_7d = _parse_stat_value(stat_id, stats_7d.get(stat_id))
            mean_7d, stdev_7d = baselines_7d[stat_id]
            z_7d = 0.0 if stdev_7d == 0 else (value_7d - mean_7d) / stdev_7d
            z_scores_7d[stat_id] = round(z_7d, 3)
            score_7d += z_7d

            value_30d = _parse_stat_value(stat_id, stats_30d.get(stat_id))
            mean_30d, stdev_30d = baselines_30d[stat_id]
            z_30d = 0.0 if stdev_30d == 0 else (value_30d - mean_30d) / stdev_30d
            z_scores_30d[stat_id] = round(z_30d, 3)
            score_30d += z_30d

        composite_score = round((weight_7d * score_7d) + (weight_30d * score_30d), 3)
        scored_players.append(
            {
                **player,
                "scoring_stat_ids": stat_ids,
                "z_scores_7d": z_scores_7d,
                "z_scores_30d": z_scores_30d,
                "score_7d": round(score_7d, 3),
                "score_30d": round(score_30d, 3),
                "composite_score": composite_score,
            }
        )

    return sorted(
        scored_players,
        key=lambda player: (
            -(player.get("composite_score") or 0.0),
            player.get("name", ""),
        ),
    )


def _sorted_candidates(players: list[dict], slot: str) -> list[dict]:
    return sorted(
        [player for player in players if _player_can_fill_slot(player, slot)],
        key=lambda player: (
            -(player.get("composite_score") or -9999.0),
            player.get("name", ""),
        ),
    )


def _slot_priority(slot: str) -> tuple[int, str]:
    norm = _normalize_position(slot)
    if norm == "OF":
        return (2, slot)
    if norm == "UTIL":
        return (3, slot)
    return (1, slot)


def _assign_slots(players: list[dict], active_slots: list[str]) -> tuple[dict[str, str], list[dict]]:
    remaining_players = {player["player_key"]: player for player in players}
    assignments: dict[str, str] = {}
    details: list[dict] = []

    position_slots = [slot for slot in active_slots if _normalize_position(slot) not in {"OF", "UTIL"}]
    outfield_slots = [slot for slot in active_slots if _normalize_position(slot) == "OF"]
    utility_slots = [slot for slot in active_slots if _normalize_position(slot) == "UTIL"]

    unfilled_slots = list(position_slots)

    # Lock one-option slots first so multi-eligible batters don't clog scarce positions.
    changed = True
    while changed:
        changed = False
        for slot in list(unfilled_slots):
            candidates = _sorted_candidates(list(remaining_players.values()), slot)
            if len(candidates) != 1:
                continue
            player = candidates[0]
            assignments[player["player_key"]] = slot
            details.append(
                {
                    "action": "start",
                    "player": player["name"],
                    "to": slot,
                    "reason": "Only eligible remaining candidate for slot",
                    "composite_score": player.get("composite_score"),
                }
            )
            remaining_players.pop(player["player_key"], None)
            unfilled_slots.remove(slot)
            changed = True

    while unfilled_slots:
        slot_candidates = []
        for slot in unfilled_slots:
            candidates = _sorted_candidates(list(remaining_players.values()), slot)
            slot_candidates.append((len(candidates), _slot_priority(slot), slot, candidates))
        slot_candidates.sort(key=lambda item: (item[0], item[1], item[2]))

        _, _, slot, candidates = slot_candidates[0]
        if not candidates:
            unfilled_slots.remove(slot)
            continue

        player = candidates[0]
        assignments[player["player_key"]] = slot
        details.append(
            {
                "action": "start",
                "player": player["name"],
                "to": slot,
                "reason": "Best ranked eligible batter for scarce slot",
                "composite_score": player.get("composite_score"),
            }
        )
        remaining_players.pop(player["player_key"], None)
        unfilled_slots.remove(slot)

    for slot in outfield_slots:
        candidates = _sorted_candidates(list(remaining_players.values()), slot)
        if not candidates:
            continue
        player = candidates[0]
        assignments[player["player_key"]] = slot
        details.append(
            {
                "action": "start",
                "player": player["name"],
                "to": slot,
                "reason": "Best remaining outfielder",
                "composite_score": player.get("composite_score"),
            }
        )
        remaining_players.pop(player["player_key"], None)

    for slot in utility_slots:
        candidates = _sorted_candidates(list(remaining_players.values()), slot)
        if not candidates:
            continue
        player = candidates[0]
        assignments[player["player_key"]] = slot
        details.append(
            {
                "action": "start",
                "player": player["name"],
                "to": slot,
                "reason": "Best remaining hitter for utility slot",
                "composite_score": player.get("composite_score"),
            }
        )
        remaining_players.pop(player["player_key"], None)

    return assignments, details


def get_optimal_batting_lineup_changes(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    weight_7d: float = 0.65,
    weight_30d: float = 0.35,
) -> tuple[list[tuple[str, str]], list[dict], dict]:
    """
    Compute batting-only lineup changes based on a weighted 7d/30d Yahoo rank.

    Returns:
        (position_changes, details, summary)
    """
    date = _today(date)
    league_key = _league_key_from_team_key(team_key)

    settings = api.get_league_scoring_settings(league_key) or {}
    batting_categories = settings.get("batting_categories") or []
    roster_7d = api.get_team_roster_stats(team_key, date=date, stat_type="lastweek")
    roster_30d = api.get_team_roster_stats(team_key, date=date, stat_type="lastmonth")
    players = _apply_recent_stat_scores(
        _merge_stat_rosters(roster_7d, roster_30d),
        batting_categories,
        weight_7d,
        weight_30d,
    )

    active_slots, _ = _parse_roster_slots(settings.get("roster_positions") or [])
    if not active_slots:
        return [], [], {
            "date": date,
            "team_key": team_key,
            "league_key": league_key,
            "weight_7d": weight_7d,
            "weight_30d": weight_30d,
            "score_mode": "category_zscore",
            "movable_batters": 0,
            "playing_batters": 0,
            "off_day_batters": 0,
            "confirmed_bench_batters": 0,
            "changes_count": 0,
            "warning": "No active batting slots were returned from Yahoo league settings",
        }

    movable_batters = [
        player
        for player in players
        if not _is_pitcher_only(player) and not _is_locked_player(player)
    ]
    playing_batters = [
        player
        for player in movable_batters
        if _has_game_today(player) and player.get("is_starting") is not False
    ]
    off_day_batters = [player for player in movable_batters if player.get("has_game_today") is False]
    confirmed_bench_batters = [
        player
        for player in movable_batters
        if _has_game_today(player) and player.get("is_starting") is False
    ]

    assignments, assignment_details = _assign_slots(playing_batters, active_slots)

    desired_positions: dict[str, str] = {}
    for player in playing_batters:
        desired_positions[player["player_key"]] = assignments.get(player["player_key"], BENCH_SLOT)
    for player in off_day_batters:
        desired_positions[player["player_key"]] = BENCH_SLOT
    for player in confirmed_bench_batters:
        desired_positions[player["player_key"]] = BENCH_SLOT

    changes: list[tuple[str, str]] = []
    details: list[dict] = list(assignment_details)
    for player in movable_batters:
        player_key = player["player_key"]
        desired = desired_positions.get(player_key, player.get("selected_position") or BENCH_SLOT)
        current = player.get("selected_position") or BENCH_SLOT
        if desired != current:
            changes.append((player_key, desired))
            details.append(
                {
                    "action": "move",
                    "player_key": player_key,
                    "player": player["name"],
                    "from": current,
                    "to": desired,
                    "reason": (
                        "Benching off-day batter"
                        if player.get("has_game_today") is False
                        else "Confirmed not in starting lineup"
                        if player.get("is_starting") is False
                        else "Recent performance optimization"
                    ),
                    "score_7d": player.get("score_7d"),
                    "score_30d": player.get("score_30d"),
                    "composite_score": player.get("composite_score"),
                    "z_scores_7d": player.get("z_scores_7d"),
                    "z_scores_30d": player.get("z_scores_30d"),
                    "opponent": player.get("opponent"),
                }
            )

    summary = {
        "date": date,
        "team_key": team_key,
        "league_key": league_key,
        "weight_7d": weight_7d,
        "weight_30d": weight_30d,
        "score_mode": "category_zscore",
        "scoring_stat_ids": _scoring_stat_ids(batting_categories),
        "movable_batters": len(movable_batters),
        "playing_batters": len(playing_batters),
        "off_day_batters": len(off_day_batters),
        "confirmed_bench_batters": len(confirmed_bench_batters),
        "changes_count": len(changes),
    }
    return changes, details, summary


def optimize_batting_lineup(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    dry_run: bool = True,
    weight_7d: float = 0.65,
    weight_30d: float = 0.35,
) -> dict:
    """
    Optimize batter slots and optionally apply the resulting Yahoo roster updates.
    """
    date = _today(date)
    changes, details, summary = get_optimal_batting_lineup_changes(
        api,
        team_key,
        date=date,
        weight_7d=weight_7d,
        weight_30d=weight_30d,
    )

    result = {
        "changes": changes,
        "details": details,
        "summary": summary,
        "applied": False,
        "error": None,
    }
    if not changes or dry_run:
        return result

    response = api.edit_roster_safe(team_key, date, changes)
    result["apply_result"] = response
    result["applied_changes"] = response.get("applied", [])
    result["failed_changes"] = response.get("failed", [])
    if result["applied_changes"]:
        result["applied"] = True
    else:
        result["error"] = "Failed to apply batting lineup changes"
        return result

    if result["failed_changes"]:
        result["error"] = f"Applied {len(result['applied_changes'])} of {len(changes)} batting lineup changes"
    return result
