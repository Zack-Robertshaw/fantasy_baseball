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
MIN_TREND_AT_BATS_7D = 10
TREND_VELOCITY_DAYS = 23
WPE_WEIGHTS = {
    "score_7d": 0.50,
    "score_30d": 0.35,
    "score_26": 0.15,
    "score_25": 0.0,
}
WPE_TIE_THRESHOLD = 0.1
DIAGNOSTIC_CLUSTER_THRESHOLD = 0.35
DIAGNOSTIC_FLUKE_THRESHOLD = 1.0


def _today(date: Optional[str]) -> str:
    return date or datetime.now().strftime("%Y-%m-%d")


def _league_key_from_team_key(team_key: str) -> str:
    if ".t." not in team_key:
        raise ValueError(f"Invalid Yahoo team_key: {team_key}")
    return team_key.split(".t.")[0]


def _resolved_wpe_weights(
    weight_7d: Optional[float] = None,
    weight_30d: Optional[float] = None,
) -> dict[str, float]:
    weights = dict(WPE_WEIGHTS)
    if weight_7d is None and weight_30d is None:
        return weights

    recent_7d = max(0.0, weight_7d if weight_7d is not None else weights["score_7d"])
    recent_30d = max(0.0, weight_30d if weight_30d is not None else weights["score_30d"])
    recent_total = recent_7d + recent_30d
    if recent_total >= 1.0:
        if recent_total == 0:
            return weights
        return {
            "score_7d": recent_7d / recent_total,
            "score_30d": recent_30d / recent_total,
            "score_26": 0.0,
            "score_25": 0.0,
        }

    remaining_total = 1.0 - recent_total
    baseline_total = WPE_WEIGHTS["score_26"] + WPE_WEIGHTS["score_25"]
    if baseline_total <= 0:
        score_26_weight = remaining_total
        score_25_weight = 0.0
    else:
        score_26_weight = remaining_total * (WPE_WEIGHTS["score_26"] / baseline_total)
        score_25_weight = remaining_total * (WPE_WEIGHTS["score_25"] / baseline_total)

    return {
        "score_7d": recent_7d,
        "score_30d": recent_30d,
        "score_26": score_26_weight,
        "score_25": score_25_weight,
    }


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


def _merge_stat_windows(
    roster_details: list[dict],
    stat_rosters: dict[str, list[dict]],
) -> list[dict]:
    by_key: dict[str, dict] = {}
    for player in roster_details:
        player_key = player.get("player_key")
        if player_key:
            by_key[player_key] = dict(player)

    for window, roster in stat_rosters.items():
        for player in roster:
            player_key = player.get("player_key")
            if not player_key:
                continue
            merged = by_key.get(player_key, {}).copy()
            for key, value in player.items():
                if key in {"stats_map", "stat_type"}:
                    continue
                existing = merged.get(key)
                if key not in merged or existing is None or existing == "" or existing == []:
                    merged[key] = value
            merged[f"stats_{window}"] = dict(player.get("stats_map") or {})
            by_key[player_key] = merged

    for player in by_key.values():
        for window in stat_rosters:
            player.setdefault(f"stats_{window}", {})

    return sorted(
        by_key.values(),
        key=lambda player: (
            player.get("name", ""),
        ),
    )


def _merge_stat_rosters(
    roster_7d: list[dict],
    roster_30d: list[dict],
) -> list[dict]:
    """Backward-compatible two-window merge for older callers/tests."""
    return _merge_stat_windows(roster_30d, {"30d": roster_30d, "7d": roster_7d})


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


def _parse_at_bats_from_hits_at_bats(raw_value: str | None) -> int:
    value = str(raw_value or "").strip()
    if "/" not in value:
        return 0
    _, at_bats = value.split("/", 1)
    try:
        return int(float(at_bats))
    except ValueError:
        return 0


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


def _trend_metrics(score_7d: float, score_30d: float, at_bats_7d: int) -> dict:
    if at_bats_7d < MIN_TREND_AT_BATS_7D:
        return {
            "at_bats_7d": at_bats_7d,
            "trend_sample_ok": False,
            "trend_sample_reason": f"Fewer than {MIN_TREND_AT_BATS_7D} AB in 7-day window",
            "breakout_index": None,
            "stability_index": None,
            "trend_velocity": None,
        }

    breakout_index = score_7d - score_30d
    stability_index = score_30d - score_7d
    return {
        "at_bats_7d": at_bats_7d,
        "trend_sample_ok": True,
        "trend_sample_reason": None,
        "breakout_index": round(breakout_index, 3),
        "stability_index": round(stability_index, 3),
        "trend_velocity": round(breakout_index / TREND_VELOCITY_DAYS, 4),
    }


def _diagnose_player(player: dict) -> dict:
    score_25 = player.get("score_25") or 0.0
    score_26 = player.get("score_26") or 0.0
    score_30d = player.get("score_30d") or 0.0
    score_7d = player.get("score_7d") or 0.0

    if score_25 > score_26 > score_30d > score_7d:
        return {
            "diagnostic_label": "washed",
            "diagnostic_action": "Drop/Trade",
            "diagnostic_reason": "2025 baseline, current season, 30-day, and 7-day scores decline in order",
        }

    if score_26 > score_30d and score_26 > score_7d:
        return {
            "diagnostic_label": "slump",
            "diagnostic_action": "Hold/Buy",
            "diagnostic_reason": "Current-season score remains above both recent windows",
        }

    long_window_scores = [score_25, score_26, score_30d]
    long_windows_clustered = max(long_window_scores) - min(long_window_scores) <= DIAGNOSTIC_CLUSTER_THRESHOLD
    if long_windows_clustered and score_7d - max(long_window_scores) >= DIAGNOSTIC_FLUKE_THRESHOLD:
        return {
            "diagnostic_label": "fluke",
            "diagnostic_action": "Sell High",
            "diagnostic_reason": "7-day score is materially above clustered longer-window baselines",
        }

    return {
        "diagnostic_label": "stable",
        "diagnostic_action": "Monitor",
        "diagnostic_reason": "No strong four-window diagnostic signal",
    }


def _trend_player_summary(player: dict, signal: str | None = None) -> dict:
    summary = {
        "player_key": player.get("player_key"),
        "player": player.get("name"),
        "team": player.get("team"),
        "display_position": player.get("display_position"),
        "eligible_positions": player.get("eligible_positions"),
        "selected_position": player.get("selected_position"),
        "at_bats_7d": player.get("at_bats_7d"),
        "score_25": player.get("score_25"),
        "score_26": player.get("score_26"),
        "score_7d": player.get("score_7d"),
        "score_30d": player.get("score_30d"),
        "composite_score": player.get("composite_score"),
        "wpe_score": player.get("wpe_score"),
        "breakout_index": player.get("breakout_index"),
        "stability_index": player.get("stability_index"),
        "trend_velocity": player.get("trend_velocity"),
        "trend_sample_ok": player.get("trend_sample_ok"),
        "trend_sample_reason": player.get("trend_sample_reason"),
        "diagnostic_label": player.get("diagnostic_label"),
        "diagnostic_action": player.get("diagnostic_action"),
        "diagnostic_reason": player.get("diagnostic_reason"),
    }
    if signal:
        summary["signal"] = signal
    return summary


def _trend_alerts(players: list[dict], limit: int = 10) -> list[dict]:
    alerts: list[tuple[float, dict]] = []
    for player in players:
        label = player.get("diagnostic_label")
        score_7d = player.get("score_7d") or 0.0
        score_30d = player.get("score_30d") or 0.0
        score_26 = player.get("score_26") or 0.0
        score_25 = player.get("score_25") or 0.0

        if label == "washed":
            alert = _trend_player_summary(player, signal="drop_or_trade_candidate")
            alert["reason"] = player.get("diagnostic_reason")
            alerts.append((300 + (score_25 - score_7d), alert))
            continue

        if label == "fluke":
            alert = _trend_player_summary(player, signal="sell_high_candidate")
            alert["reason"] = player.get("diagnostic_reason")
            alerts.append((200 + (score_7d - max(score_30d, score_26, score_25)), alert))
            continue

        if label == "slump":
            alert = _trend_player_summary(player, signal="hold_or_buy_candidate")
            alert["reason"] = player.get("diagnostic_reason")
            alerts.append((100 + (score_26 - min(score_30d, score_7d)), alert))

    return [
        alert
        for _, alert in sorted(
            alerts,
            key=lambda item: (
                -item[0],
                item[1].get("player") or "",
            ),
        )[:limit]
    ]


def _player_evaluation_fields(player: dict) -> dict:
    return {
        "score_25": player.get("score_25"),
        "score_26": player.get("score_26"),
        "score_30d": player.get("score_30d"),
        "score_7d": player.get("score_7d"),
        "wpe_score": player.get("wpe_score"),
        "composite_score": player.get("composite_score"),
        "diagnostic_label": player.get("diagnostic_label"),
        "diagnostic_action": player.get("diagnostic_action"),
        "diagnostic_reason": player.get("diagnostic_reason"),
    }


def _scored_roster_context(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str],
    weight_7d: Optional[float],
    weight_30d: Optional[float],
) -> tuple[str, str, dict, list[dict], list[dict]]:
    date = _today(date)
    league_key = _league_key_from_team_key(team_key)
    settings = api.get_league_scoring_settings(league_key) or {}
    batting_categories = settings.get("batting_categories") or []
    roster_details = api.get_team_roster_details(team_key, date=date)
    roster_25 = api.get_team_roster_stats(team_key, stat_type="season", season="2025")
    roster_26 = api.get_team_roster_stats(team_key, stat_type="season")
    roster_30d = api.get_team_roster_stats(team_key, stat_type="lastmonth")
    roster_7d = api.get_team_roster_stats(team_key, stat_type="lastweek")
    players = _apply_recent_stat_scores(
        _merge_stat_windows(
            roster_details,
            {
                "25": roster_25,
                "26": roster_26,
                "30d": roster_30d,
                "7d": roster_7d,
            },
        ),
        batting_categories,
        weight_7d,
        weight_30d,
    )
    return date, league_key, settings, batting_categories, players


def _apply_recent_stat_scores(
    players: list[dict],
    batting_categories: list[dict],
    weight_7d: Optional[float],
    weight_30d: Optional[float],
) -> list[dict]:
    stat_ids = _scoring_stat_ids(batting_categories)
    if not stat_ids:
        return players
    wpe_weights = _resolved_wpe_weights(weight_7d, weight_30d)

    windows = ["25", "26", "30d", "7d"]
    values_by_window: dict[str, dict[str, list[float]]] = {
        window: {stat_id: [] for stat_id in stat_ids}
        for window in windows
    }
    for player in players:
        stats_by_window = {
            window: player.get(f"stats_{window}") or {}
            for window in windows
        }
        for stat_id in stat_ids:
            for window in windows:
                values_by_window[window][stat_id].append(
                    _parse_stat_value(stat_id, stats_by_window[window].get(stat_id))
                )

    baselines_by_window = {
        window: {
            stat_id: (mean(series), pstdev(series) if len(series) > 1 else 0.0)
            for stat_id, series in stat_values.items()
        }
        for window, stat_values in values_by_window.items()
    }

    scored_players: list[dict] = []
    for player in players:
        z_scores_by_window: dict[str, dict[str, float]] = {window: {} for window in windows}
        scores_by_window: dict[str, float] = {window: 0.0 for window in windows}
        stats_by_window = {
            window: player.get(f"stats_{window}") or {}
            for window in windows
        }
        for stat_id in stat_ids:
            for window in windows:
                value = _parse_stat_value(stat_id, stats_by_window[window].get(stat_id))
                mean_value, stdev_value = baselines_by_window[window][stat_id]
                z_score = 0.0 if stdev_value == 0 else (value - mean_value) / stdev_value
                z_scores_by_window[window][stat_id] = round(z_score, 3)
                scores_by_window[window] += z_score

        rounded_score_25 = round(scores_by_window["25"], 3)
        rounded_score_26 = round(scores_by_window["26"], 3)
        rounded_score_30d = round(scores_by_window["30d"], 3)
        rounded_score_7d = round(scores_by_window["7d"], 3)
        wpe_score = round(
            (wpe_weights["score_7d"] * rounded_score_7d)
            + (wpe_weights["score_30d"] * rounded_score_30d)
            + (wpe_weights["score_26"] * rounded_score_26)
            + (wpe_weights["score_25"] * rounded_score_25),
            3,
        )
        composite_score = wpe_score
        at_bats_7d = _parse_at_bats_from_hits_at_bats(stats_by_window["7d"].get("60"))
        scored_player = {
            **player,
            "scoring_stat_ids": stat_ids,
            "z_scores_25": z_scores_by_window["25"],
            "z_scores_26": z_scores_by_window["26"],
            "z_scores_30d": z_scores_by_window["30d"],
            "z_scores_7d": z_scores_by_window["7d"],
            "score_25": rounded_score_25,
            "score_26": rounded_score_26,
            "score_30d": rounded_score_30d,
            "score_7d": rounded_score_7d,
            "wpe_score": wpe_score,
            # Keep composite_score for older UI/email consumers; it now means WPE.
            "composite_score": composite_score,
            **_trend_metrics(rounded_score_7d, rounded_score_30d, at_bats_7d),
        }
        scored_player.update(_diagnose_player(scored_player))
        scored_players.append(scored_player)

    return sorted(
        scored_players,
        key=lambda player: (
            -(player.get("wpe_score") or 0.0),
            -(player.get("score_26") or 0.0),
            player.get("name", ""),
        ),
    )


def _sorted_candidates(players: list[dict], slot: str) -> list[dict]:
    return sorted(
        [player for player in players if _player_can_fill_slot(player, slot)],
        key=lambda player: (
            -_numeric_player_score(player, "wpe_score"),
            -_numeric_player_score(player, "score_26"),
            player.get("name", ""),
        ),
    )


def _numeric_player_score(player: dict, field: str, default: float = -9999.0) -> float:
    value = player.get(field)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _best_candidate(candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda player: (
            -_numeric_player_score(player, "wpe_score"),
            player.get("name", ""),
        ),
    )
    best = ordered[0]
    tied = [
        player
        for player in ordered
        if abs((player.get("wpe_score") or 0.0) - (best.get("wpe_score") or 0.0)) < WPE_TIE_THRESHOLD
    ]
    if len(tied) <= 1:
        return best
    return sorted(
        tied,
        key=lambda player: (
            -_numeric_player_score(player, "score_26"),
            player.get("name", ""),
        ),
    )[0]


def _slot_priority(slot: str) -> tuple[int, str]:
    norm = _normalize_position(slot)
    if norm == "OF":
        return (2, slot)
    if norm == "UTIL":
        return (3, slot)
    return (1, slot)


def _assign_slots(players: list[dict], active_slots: list[str]) -> tuple[dict[str, str], list[dict]]:
    if not players or not active_slots:
        return {}, []

    player_by_index = list(players)
    eligible_player_indices_by_slot = [
        [
            player_index
            for player_index, player in enumerate(player_by_index)
            if _player_can_fill_slot(player, slot)
        ]
        for slot in active_slots
    ]

    def score_with_player(score: tuple[int, float, float], player: dict) -> tuple[int, float, float]:
        return (
            score[0] + 1,
            score[1] + _numeric_player_score(player, "wpe_score"),
            score[2] + _numeric_player_score(player, "score_26"),
        )

    def score_key(score: tuple[int, float, float]) -> tuple[int, float, float]:
        return (score[0], round(score[1], 6), round(score[2], 6))

    def assignment_signature(assignments: tuple[tuple[int, int], ...]) -> tuple[tuple[int, str, str], ...]:
        return tuple(
            (
                slot_index,
                player_by_index[player_index].get("name", ""),
                player_by_index[player_index].get("player_key", ""),
            )
            for slot_index, player_index in assignments
        )

    def is_better_state(
        candidate: tuple[tuple[int, float, float], tuple[tuple[int, int], ...]],
        incumbent: tuple[tuple[int, float, float], tuple[tuple[int, int], ...]] | None,
    ) -> bool:
        if incumbent is None:
            return True
        candidate_score, candidate_assignments = candidate
        incumbent_score, incumbent_assignments = incumbent
        if score_key(candidate_score) != score_key(incumbent_score):
            return score_key(candidate_score) > score_key(incumbent_score)
        return assignment_signature(candidate_assignments) < assignment_signature(incumbent_assignments)

    states: dict[int, tuple[tuple[int, float, float], tuple[tuple[int, int], ...]]] = {
        0: ((0, 0.0, 0.0), tuple())
    }
    for slot_index, player_indices in enumerate(eligible_player_indices_by_slot):
        next_states = dict(states)
        for used_mask, (score, assignments_so_far) in states.items():
            for player_index in player_indices:
                bit = 1 << player_index
                if used_mask & bit:
                    continue
                player = player_by_index[player_index]
                candidate_state = (
                    score_with_player(score, player),
                    assignments_so_far + ((slot_index, player_index),),
                )
                next_mask = used_mask | bit
                if is_better_state(candidate_state, next_states.get(next_mask)):
                    next_states[next_mask] = candidate_state
        states = next_states

    best_state: tuple[tuple[int, float, float], tuple[tuple[int, int], ...]] | None = None
    for state in states.values():
        if is_better_state(state, best_state):
            best_state = state
    if best_state is None:
        return {}, []
    best_score, best_assignment_pairs = best_state
    if best_score[0] == 0:
        return {}, []

    assignments: dict[str, str] = {}
    details: list[dict] = []
    for slot_index, player_index in sorted(best_assignment_pairs):
        slot = active_slots[slot_index]
        player = player_by_index[player_index]
        assignments[player["player_key"]] = slot
        details.append(
            {
                "action": "start",
                "player": player["name"],
                "to": slot,
                "reason": "Globally optimal WPE assignment across active batting slots",
                **_player_evaluation_fields(player),
            }
        )

    return assignments, details


def get_optimal_batting_lineup_changes(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    weight_7d: Optional[float] = None,
    weight_30d: Optional[float] = None,
) -> tuple[list[tuple[str, str]], list[dict], dict]:
    """
    Compute batting-only lineup changes based on four-window WPE scores.

    Returns:
        (position_changes, details, summary)
    """
    date, league_key, settings, batting_categories, players = _scored_roster_context(
        api,
        team_key,
        date,
        weight_7d,
        weight_30d,
    )
    wpe_weights = _resolved_wpe_weights(weight_7d, weight_30d)

    movable_batters = [
        player
        for player in players
        if not _is_pitcher_only(player) and not _is_locked_player(player)
    ]
    active_slots, _ = _parse_roster_slots(settings.get("roster_positions") or [])
    if not active_slots:
        return [], [], {
            "date": date,
            "team_key": team_key,
            "league_key": league_key,
            "wpe_weights": wpe_weights,
            "score_mode": "four_window_wpe",
            "movable_batters": 0,
            "playing_batters": 0,
            "off_day_batters": 0,
            "confirmed_bench_batters": 0,
            "changes_count": 0,
            "trend_alerts": _trend_alerts(movable_batters),
            "warning": "No active batting slots were returned from Yahoo league settings",
        }

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
                        else "Weighted performance expectation optimization"
                    ),
                    **_player_evaluation_fields(player),
                    "z_scores_25": player.get("z_scores_25"),
                    "z_scores_26": player.get("z_scores_26"),
                    "z_scores_7d": player.get("z_scores_7d"),
                    "z_scores_30d": player.get("z_scores_30d"),
                    "opponent": player.get("opponent"),
                }
            )

    summary = {
        "date": date,
        "team_key": team_key,
        "league_key": league_key,
        "wpe_weights": wpe_weights,
        "wpe_tie_threshold": WPE_TIE_THRESHOLD,
        "score_mode": "four_window_wpe",
        "scoring_stat_ids": _scoring_stat_ids(batting_categories),
        "movable_batters": len(movable_batters),
        "playing_batters": len(playing_batters),
        "off_day_batters": len(off_day_batters),
        "confirmed_bench_batters": len(confirmed_bench_batters),
        "changes_count": len(changes),
        "trend_alerts": _trend_alerts(movable_batters),
    }
    return changes, details, summary


def evaluate_roster_trends(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    weight_7d: Optional[float] = None,
    weight_30d: Optional[float] = None,
) -> dict:
    """
    Evaluate roster batters for WPE and four-window diagnostic signals without moving players.
    """
    date, league_key, _, batting_categories, players = _scored_roster_context(
        api,
        team_key,
        date,
        weight_7d,
        weight_30d,
    )
    wpe_weights = _resolved_wpe_weights(weight_7d, weight_30d)
    batters = [player for player in players if not _is_pitcher_only(player)]
    diagnostic_priority = {
        "washed": 0,
        "fluke": 1,
        "slump": 2,
        "stable": 3,
    }
    trends = [
        _trend_player_summary(player)
        for player in sorted(
            batters,
            key=lambda player: (
                diagnostic_priority.get(player.get("diagnostic_label"), 9),
                -(player.get("wpe_score") or -9999.0),
                player.get("name", ""),
            ),
        )
    ]

    return {
        "date": date,
        "team_key": team_key,
        "league_key": league_key,
        "wpe_weights": wpe_weights,
        "wpe_tie_threshold": WPE_TIE_THRESHOLD,
        "score_mode": "four_window_wpe",
        "scoring_stat_ids": _scoring_stat_ids(batting_categories),
        "min_trend_at_bats_7d": MIN_TREND_AT_BATS_7D,
        "trend_velocity_days": TREND_VELOCITY_DAYS,
        "trend_alerts": _trend_alerts(batters),
        "players": trends,
    }


def optimize_batting_lineup(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    dry_run: bool = True,
    weight_7d: Optional[float] = None,
    weight_30d: Optional[float] = None,
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
