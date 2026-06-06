"""
Waiver-wire target suggestions driven by category needs.
"""

from __future__ import annotations

from datetime import datetime
from statistics import mean, pstdev
from typing import Optional

from batter_optimizer import (
    BENCH_SLOT,
    LOCKED_SLOTS,
    NON_SCORING_STAT_IDS,
    OUTFIELD_POSITIONS,
    _is_pitcher_only,
    _merge_stat_windows,
    _normalize_position,
    _parse_at_bats_from_hits_at_bats,
    _parse_stat_value,
    _resolved_wpe_weights,
    _scoring_stat_ids,
)
from yahoo_api import YahooFantasyAPI


WINDOWS = ["25", "26", "30d", "7d"]
ROSTER_ONLY_POSITIONS = LOCKED_SLOTS | {BENCH_SLOT}
STANDARD_BATTER_POSITIONS = {"C", "1B", "2B", "3B", "SS", "OF"}


def _today(date: Optional[str]) -> str:
    return date or datetime.now().strftime("%Y-%m-%d")


def _league_key_from_team_key(team_key: str) -> str:
    if ".t." not in team_key:
        raise ValueError(f"Invalid Yahoo team_key: {team_key}")
    return team_key.split(".t.")[0]


def _category_name_by_id(batting_categories: list[dict]) -> dict[str, str]:
    return {
        str(row.get("stat_id")): str(row.get("name") or row.get("stat_id"))
        for row in batting_categories or []
        if row.get("stat_id")
    }


def _derive_category_weights(
    category_records: dict,
    batting_categories: list[dict],
) -> dict[str, float]:
    weights: dict[str, float] = {}
    for row in batting_categories or []:
        stat_id = str(row.get("stat_id") or "")
        if not stat_id or stat_id in NON_SCORING_STAT_IDS:
            continue
        category = str(row.get("name") or stat_id)
        record = category_records.get(category) or category_records.get(stat_id) or {}
        wins = int(record.get("W") or 0)
        losses = int(record.get("L") or 0)
        ties = int(record.get("T") or 0)
        total = wins + losses + ties
        weights[stat_id] = round((losses + 0.5 * ties) / total, 3) if total else 0.5
    return weights


def _windowed_score(player: dict, field_prefix: str, wpe_weights: dict[str, float]) -> float:
    return round(
        (wpe_weights["score_7d"] * float(player.get(f"{field_prefix}_7d") or 0.0))
        + (wpe_weights["score_30d"] * float(player.get(f"{field_prefix}_30d") or 0.0))
        + (wpe_weights["score_26"] * float(player.get(f"{field_prefix}_26") or 0.0))
        + (wpe_weights["score_25"] * float(player.get(f"{field_prefix}_25") or 0.0)),
        3,
    )


def _score_player_pool(
    players: list[dict],
    batting_categories: list[dict],
    category_weights: dict[str, float],
    weight_7d: Optional[float] = None,
    weight_30d: Optional[float] = None,
) -> list[dict]:
    stat_ids = _scoring_stat_ids(batting_categories)
    if not stat_ids:
        return players
    wpe_weights = _resolved_wpe_weights(weight_7d, weight_30d)

    values_by_window: dict[str, dict[str, list[float]]] = {
        window: {stat_id: [] for stat_id in stat_ids}
        for window in WINDOWS
    }
    for player in players:
        for window in WINDOWS:
            stats = player.get(f"stats_{window}") or {}
            for stat_id in stat_ids:
                values_by_window[window][stat_id].append(
                    _parse_stat_value(stat_id, stats.get(stat_id))
                )

    baselines = {
        window: {
            stat_id: (mean(series), pstdev(series) if len(series) > 1 else 0.0)
            for stat_id, series in values.items()
        }
        for window, values in values_by_window.items()
    }

    scored: list[dict] = []
    for player in players:
        z_scores_by_window: dict[str, dict[str, float]] = {window: {} for window in WINDOWS}
        weighted_scores_by_window: dict[str, float] = {window: 0.0 for window in WINDOWS}
        unweighted_scores_by_window: dict[str, float] = {window: 0.0 for window in WINDOWS}
        for window in WINDOWS:
            stats = player.get(f"stats_{window}") or {}
            for stat_id in stat_ids:
                value = _parse_stat_value(stat_id, stats.get(stat_id))
                mean_value, stdev_value = baselines[window][stat_id]
                z_score = 0.0 if stdev_value == 0 else (value - mean_value) / stdev_value
                z_scores_by_window[window][stat_id] = round(z_score, 3)
                unweighted_scores_by_window[window] += z_score
                weighted_scores_by_window[window] += z_score * category_weights.get(stat_id, 0.5)

        enriched = {
            **player,
            "scoring_stat_ids": stat_ids,
            "z_scores_25": z_scores_by_window["25"],
            "z_scores_26": z_scores_by_window["26"],
            "z_scores_30d": z_scores_by_window["30d"],
            "z_scores_7d": z_scores_by_window["7d"],
            "score_25": round(unweighted_scores_by_window["25"], 3),
            "score_26": round(unweighted_scores_by_window["26"], 3),
            "score_30d": round(unweighted_scores_by_window["30d"], 3),
            "score_7d": round(unweighted_scores_by_window["7d"], 3),
            "weighted_score_25": round(weighted_scores_by_window["25"], 3),
            "weighted_score_26": round(weighted_scores_by_window["26"], 3),
            "weighted_score_30d": round(weighted_scores_by_window["30d"], 3),
            "weighted_score_7d": round(weighted_scores_by_window["7d"], 3),
            "at_bats_7d": _parse_at_bats_from_hits_at_bats(
                (player.get("stats_7d") or {}).get("60")
            ),
        }
        enriched["weighted_score"] = _windowed_score(enriched, "weighted_score", wpe_weights)
        scored.append(enriched)

    return scored


def _free_agent_query_positions(positions: set[str] | None = None) -> set[str]:
    queryable = {"C", "1B", "2B", "3B", "SS", "OF"}
    if not positions:
        return set(queryable)
    mapped: set[str] = set()
    for position in positions:
        if position in {"MI"}:
            mapped.update({"2B", "SS"})
        elif position in {"CI"}:
            mapped.update({"1B", "3B"})
        elif position in OUTFIELD_POSITIONS:
            mapped.add("OF")
        elif position in queryable:
            mapped.add(position)
    return mapped


def _format_positions(player: dict) -> list[str]:
    return [
        str(position)
        for position in player.get("eligible_positions") or player.get("positions") or []
        if str(position or "").strip()
        and _normalize_position(position) not in ROSTER_ONLY_POSITIONS
    ]


def _has_roster_only_status(player: dict) -> bool:
    selected_position = _normalize_position(player.get("selected_position"))
    if selected_position in LOCKED_SLOTS:
        return True
    positions = {
        _normalize_position(position)
        for position in player.get("eligible_positions") or player.get("positions") or []
        if str(position or "").strip()
    }
    return bool(positions & LOCKED_SLOTS)


def _weak_category_scores(
    player: dict,
    category_weights: dict[str, float],
    name_by_id: dict[str, str],
    limit: int = 3,
) -> dict[str, float]:
    scores = []
    z_scores = player.get("z_scores_30d") or player.get("z_scores_26") or {}
    for stat_id, weight in category_weights.items():
        if stat_id not in z_scores:
            continue
        scores.append((weight, name_by_id.get(stat_id, stat_id), float(z_scores.get(stat_id) or 0.0)))
    return {
        name: round(score, 3)
        for _, name, score in sorted(scores, key=lambda item: (-item[0], item[1]))[:limit]
    }


def _top_target_categories(
    player: dict,
    category_weights: dict[str, float],
    name_by_id: dict[str, str],
    limit: int = 3,
) -> list[str]:
    categories = []
    z_scores = player.get("z_scores_30d") or player.get("z_scores_26") or {}
    for stat_id, weight in category_weights.items():
        z_score = float(z_scores.get(stat_id) or 0.0)
        if z_score > 0:
            categories.append((z_score * weight, name_by_id.get(stat_id, stat_id), z_score))
    return [
        f"{name} (+{z_score:.1f}z)"
        for _, name, z_score in sorted(categories, key=lambda item: (-item[0], item[1]))[:limit]
    ]


def _target_reason(
    player: dict,
    category_weights: dict[str, float],
    name_by_id: dict[str, str],
) -> str:
    categories = _top_target_categories(player, category_weights, name_by_id)
    if categories:
        return "Strong available contributor in need categories: " + ", ".join(categories)
    return f"Top available hitter by category-weighted score ({player.get('weighted_score')})"


def _merge_free_agent_windows(free_agents: list[dict], stats_by_window: dict[str, list[dict]]) -> list[dict]:
    return _merge_stat_windows(free_agents, stats_by_window)


def get_add_drop_suggestions(
    api: YahooFantasyAPI,
    team_key: str,
    date: Optional[str] = None,
    fa_count_per_position: int = 25,
    top_n: int = 10,
    weight_7d: Optional[float] = None,
    weight_30d: Optional[float] = None,
) -> dict:
    roster_date = _today(date)
    league_key = _league_key_from_team_key(team_key)
    settings = api.get_league_scoring_settings(league_key) or {}
    batting_categories = settings.get("batting_categories") or []
    category_records = api.get_team_category_records(league_key, team_key)
    category_weights = _derive_category_weights(category_records, batting_categories)
    name_by_id = _category_name_by_id(batting_categories)

    query_positions = _free_agent_query_positions(STANDARD_BATTER_POSITIONS)
    free_agents = api.get_league_free_agents(
        league_key,
        positions=query_positions,
        count_per_position=fa_count_per_position,
    )
    free_agents = [
        player
        for player in free_agents
        if not _is_pitcher_only(player) and not _has_roster_only_status(player)
    ]
    fa_keys = [player["player_key"] for player in free_agents if player.get("player_key")]
    fa_players = _merge_free_agent_windows(
        free_agents,
        {
            "25": api.get_free_agent_stats(league_key, fa_keys, stat_type="season", season="2025"),
            "26": api.get_free_agent_stats(league_key, fa_keys, stat_type="season"),
            "30d": api.get_free_agent_stats(league_key, fa_keys, stat_type="lastmonth"),
            "7d": api.get_free_agent_stats(league_key, fa_keys, stat_type="lastweek"),
        },
    )
    fa_players = [
        player
        for player in fa_players
        if not _is_pitcher_only(player) and not _has_roster_only_status(player)
    ]
    scored_fas = _score_player_pool(
        fa_players,
        batting_categories,
        category_weights,
        weight_7d,
        weight_30d,
    )
    target_players = sorted(
        scored_fas,
        key=lambda player: (
            -float(player.get("weighted_score") or 0.0),
            -float(player.get("weighted_score_30d") or 0.0),
            player.get("name", ""),
        ),
    )[: max(0, top_n)]

    suggestions = [
        {
            "player_key": player.get("player_key"),
            "player": player.get("name"),
            "mlb_team": player.get("team"),
            "eligible_positions": _format_positions(player),
            "weighted_score": player.get("weighted_score"),
            "category_scores": _weak_category_scores(player, category_weights, name_by_id),
            "helps_categories": _top_target_categories(player, category_weights, name_by_id),
            "target_reason": _target_reason(player, category_weights, name_by_id),
            "score_7d": player.get("weighted_score_7d"),
            "score_30d": player.get("weighted_score_30d"),
            "score_26": player.get("weighted_score_26"),
            "at_bats_7d": player.get("at_bats_7d"),
        }
        for player in target_players
    ]

    return {
        "date": roster_date,
        "team_key": team_key,
        "league_key": league_key,
        "score_mode": "category_need_weighted_free_agent_targets",
        "category_records": category_records,
        "category_weights": category_weights,
        "fa_count_per_position": fa_count_per_position,
        "free_agents_evaluated": len(fa_players),
        "queried_positions": sorted(query_positions),
        "suggestions": suggestions,
    }
