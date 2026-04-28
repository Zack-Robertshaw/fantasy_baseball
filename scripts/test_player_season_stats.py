#!/usr/bin/env python3
"""
Discover Yahoo Fantasy API player stat endpoints.

  /fantasy/v2/player/{player_key}/stats;type=season;season=2025

Base URL is handled by YahooFantasyAPI; pass path starting with /player/...

Usage (from repo root, with .env + yahoo_tokens.json):

  python3 scripts/test_player_season_stats.py 461.p.10259
  python3 scripts/test_player_season_stats.py 461.p.10259 2025
  python3 scripts/test_player_season_stats.py 461.p.10259 2025 2025-04-27

Env:

  YAHOO_TEST_PLAYER_KEY — default player key if not passed as argv[1]
  YAHOO_TEST_TEAM_KEY   — optional roster team key for batch endpoint tests
  YAHOO_TEST_DATE       — optional date for date-specific stats
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Repo root (parent of scripts/)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.chdir(ROOT)

from datetime import datetime

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yahoo_api import YahooFantasyAPI  # noqa: E402


OUTPUT_DIR = Path(os.getenv("YAHOO_DISCOVERY_OUTPUT_DIR", "/tmp/fantasy_baseball_yahoo_stats_discovery"))
SCORING_STAT_IDS = {"60", "7", "12", "13", "16", "3"}


def _first_local_team_profile() -> tuple[str | None, str | None]:
    """Return (team_key, league_key) from LOCAL_TEAM_PROFILES when configured."""
    raw = (os.getenv("LOCAL_TEAM_PROFILES") or "").strip()
    if not raw:
        return None, None
    for chunk in raw.split(";"):
        parts = [part.strip() for part in chunk.split("|")]
        if len(parts) != 3:
            continue
        _, league_key, team_key = parts
        if team_key and ".t." in team_key:
            return team_key, league_key or team_key.split(".t.")[0]
    return None, None


def _configured_team_key() -> str | None:
    profile_team_key, _ = _first_local_team_profile()
    return (
        (os.getenv("LINEUP_TEAM_KEY") or "").strip()
        or (os.getenv("YAHOO_TEST_TEAM_KEY") or "").strip()
        or profile_team_key
        or None
    )


def _league_key_from_team_key(team_key: str | None) -> str | None:
    if team_key and ".t." in team_key:
        return team_key.split(".t.")[0]
    _, profile_league_key = _first_local_team_profile()
    return profile_league_key


def _discover_player_key_from_lineup_team(api: YahooFantasyAPI) -> str | None:
    """Use LINEUP_TEAM_KEY roster to obtain any valid MLB player_key for this login."""
    team_key = _configured_team_key()
    if not team_key:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    roster = api.get_team_roster_details(team_key, date=today)
    for row in roster or []:
        pk = (row.get("player_key") or "").strip()
        if pk and ".p." in pk:
            return pk
    return None


def _discover_roster_player_keys(api: YahooFantasyAPI, team_key: str | None, limit: int = 3) -> list[str]:
    """Use the configured roster to obtain a small sample of MLB player keys."""
    if not team_key:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    roster = api.get_team_roster_details(team_key, date=today)
    keys: list[str] = []
    for row in roster or []:
        player_key = (row.get("player_key") or "").strip()
        if player_key and ".p." in player_key and player_key not in keys:
            keys.append(player_key)
        if len(keys) >= limit:
            break
    return keys


def _stat_rows_from_player_obj(player_obj: list | dict) -> list[dict]:
    if not isinstance(player_obj, list):
        return []
    for element in player_obj:
        if not isinstance(element, dict) or "player_stats" not in element:
            continue
        player_stats = element.get("player_stats") or {}
        stats = player_stats.get("stats") or []
        return stats if isinstance(stats, list) else []
    return []


def _stats_metadata_from_player_obj(player_obj: list | dict) -> dict:
    if not isinstance(player_obj, list):
        return {}
    for element in player_obj:
        if not isinstance(element, dict) or "player_stats" not in element:
            continue
        player_stats = element.get("player_stats") or {}
        if isinstance(player_stats.get("0"), dict):
            return player_stats["0"]
        return {
            "coverage_type": player_stats.get("coverage_type"),
            "season": player_stats.get("season"),
            "week": player_stats.get("week"),
            "date": player_stats.get("date"),
        }
    return {}


def _stat_ids(stats: list[dict]) -> list[str]:
    ids: list[str] = []
    for row in stats:
        stat = row.get("stat") if isinstance(row, dict) else None
        if isinstance(stat, dict) and stat.get("stat_id") is not None:
            ids.append(str(stat["stat_id"]))
    return ids


def _scoring_stat_ids_present(stats: list[dict]) -> list[str]:
    return [stat_id for stat_id in _stat_ids(stats) if stat_id in SCORING_STAT_IDS]


def _compact_stat_rows(stats: list[dict], limit: int = 5) -> list[dict]:
    rows: list[dict] = []
    for row in stats[:limit]:
        stat = row.get("stat") if isinstance(row, dict) else None
        if not isinstance(stat, dict):
            continue
        rows.append(
            {
                "stat_id": stat.get("stat_id"),
                "value": stat.get("value"),
            }
        )
    return rows


def _extract_player_name(player_obj: list | dict) -> str | None:
    if not isinstance(player_obj, list):
        return None
    for element in player_obj:
        if not isinstance(element, list):
            continue
        for item in element:
            if isinstance(item, dict) and isinstance(item.get("name"), dict):
                return item["name"].get("full")
    return None


def _summarize_player_payload(data: dict | None) -> dict:
    if not data:
        return {"status": "no_json"}
    try:
        player = data.get("fantasy_content", {}).get("player", [])
        if isinstance(player, list) and len(player) > 1:
            block = player[1]
            stats = block.get("player_stats", {})
            metadata = stats.get("0", {}) if isinstance(stats.get("0"), dict) else {}
            rows = stats.get("stats") or []
            return {
                "coverage_type": stats.get("coverage_type") or metadata.get("coverage_type"),
                "season": stats.get("season") or metadata.get("season"),
                "week": stats.get("week") or metadata.get("week"),
                "date": stats.get("date") or metadata.get("date"),
                "stats_count": len(rows),
                "stat_ids": _stat_ids(rows),
                "scoring_stat_ids_present": _scoring_stat_ids_present(rows),
                "matches_all_scoring_stat_ids": SCORING_STAT_IDS.issubset(set(_stat_ids(rows))),
                "first_stats": _compact_stat_rows(rows),
            }
    except (KeyError, TypeError, IndexError):
        pass
    return _summarize_generic_payload(data)


def _summarize_roster_stats_payload(data: dict | None) -> dict:
    if not data:
        return {"status": "no_json"}
    try:
        roster = data.get("fantasy_content", {}).get("team", [{}])[1].get("roster", {})
        players = roster.get("0", {}).get("players", {})
        summaries = []
        for key, value in players.items():
            if not str(key).isdigit():
                continue
            player_obj = value.get("player", [])
            stats = _stat_rows_from_player_obj(player_obj)
            metadata = _stats_metadata_from_player_obj(player_obj)
            summaries.append(
                {
                    "player": _extract_player_name(player_obj),
                    "coverage_type": metadata.get("coverage_type"),
                    "season": metadata.get("season"),
                    "week": metadata.get("week"),
                    "date": metadata.get("date"),
                    "stats_count": len(stats),
                    "stat_ids": _stat_ids(stats),
                    "scoring_stat_ids_present": _scoring_stat_ids_present(stats),
                    "matches_all_scoring_stat_ids": SCORING_STAT_IDS.issubset(set(_stat_ids(stats))),
                    "first_stats": _compact_stat_rows(stats),
                }
            )
        return {
            "players_count": len(summaries),
            "batch_friendly": True,
            "first_players": summaries[:3],
        }
    except (KeyError, TypeError, IndexError, AttributeError):
        return _summarize_generic_payload(data)


def _summarize_league_players_payload(data: dict | None) -> dict:
    if not data:
        return {"status": "no_json"}
    try:
        league = data.get("fantasy_content", {}).get("league", [])
        players = league[1].get("players", {}) if isinstance(league, list) and len(league) > 1 else {}
        summaries = []
        for key, value in players.items():
            if not str(key).isdigit():
                continue
            player_obj = value.get("player", [])
            stats = _stat_rows_from_player_obj(player_obj)
            metadata = _stats_metadata_from_player_obj(player_obj)
            summaries.append(
                {
                    "player": _extract_player_name(player_obj),
                    "coverage_type": metadata.get("coverage_type"),
                    "season": metadata.get("season"),
                    "week": metadata.get("week"),
                    "date": metadata.get("date"),
                    "stats_count": len(stats),
                    "stat_ids": _stat_ids(stats),
                    "scoring_stat_ids_present": _scoring_stat_ids_present(stats),
                    "matches_all_scoring_stat_ids": SCORING_STAT_IDS.issubset(set(_stat_ids(stats))),
                    "first_stats": _compact_stat_rows(stats),
                }
            )
        return {
            "players_count": len(summaries),
            "batch_friendly": True,
            "first_players": summaries[:3],
        }
    except (KeyError, TypeError, IndexError, AttributeError):
        return _summarize_generic_payload(data)


def _summarize_generic_payload(data: dict | None) -> dict:
    if not data:
        return {"status": "no_json"}
    fantasy_content = data.get("fantasy_content", {}) if isinstance(data, dict) else {}
    summary = {
        "top_level_keys": sorted(data.keys()) if isinstance(data, dict) else [],
        "fantasy_content_keys": sorted(fantasy_content.keys()) if isinstance(fantasy_content, dict) else [],
    }
    if isinstance(fantasy_content, dict):
        player = fantasy_content.get("player")
        if isinstance(player, list):
            for element in player:
                if isinstance(element, dict) and "percent_owned" in element:
                    summary["percent_owned"] = element["percent_owned"]
                if isinstance(element, dict) and "draft_analysis" in element:
                    summary["draft_analysis"] = element["draft_analysis"]
        league = fantasy_content.get("league")
        if isinstance(league, list) and len(league) > 1:
            players = league[1].get("players", {}) if isinstance(league[1], dict) else {}
            ownership_rows = []
            for key, value in players.items():
                if not str(key).isdigit():
                    continue
                player_obj = value.get("player", []) if isinstance(value, dict) else []
                row = {"player": _extract_player_name(player_obj)}
                for element in player_obj:
                    if isinstance(element, dict) and "ownership" in element:
                        row["ownership"] = element["ownership"]
                ownership_rows.append(row)
            if ownership_rows:
                summary["players_count"] = len(ownership_rows)
                summary["first_players"] = ownership_rows[:3]
    return summary


def _print_result(label: str, endpoint: str, summary: dict, success: bool) -> None:
    status = "OK" if success else "FAILED"
    print(f"\n=== {label} [{status}] ===")
    print(f"GET ...{endpoint}?format=json")
    print(json.dumps(summary, indent=2, sort_keys=True))


def _write_raw_payload(label: str, identifier: str, season: str, data: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_identifier = identifier.replace(".", "_").replace(",", "_").replace(";", "_")
    path = OUTPUT_DIR / f"{label}_{safe_identifier}_{season}.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return path


def main() -> int:
    argv_key = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    env_key = (os.getenv("YAHOO_TEST_PLAYER_KEY") or "").strip()
    season = (sys.argv[2] if len(sys.argv) > 2 else os.getenv("YAHOO_TEST_SEASON", "2025")).strip()
    test_date = (
        sys.argv[3] if len(sys.argv) > 3 else os.getenv("YAHOO_TEST_DATE", "2025-04-27")
    ).strip()
    prior_season = str(int(season) - 1) if season.isdigit() else "2024"

    client_id = os.getenv("YAHOO_CLIENT_ID")
    client_secret = os.getenv("YAHOO_CLIENT_SECRET")
    redirect_uri = os.getenv("YAHOO_REDIRECT_URI", "http://localhost:8000/auth/callback")
    if not client_id or not client_secret:
        print("YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET not set in .env", file=sys.stderr)
        return 1

    api = YahooFantasyAPI(client_id, client_secret, redirect_uri)

    keys_to_try: list[str] = []
    for candidate in (argv_key, env_key):
        if candidate and candidate not in keys_to_try:
            keys_to_try.append(candidate)

    discovered = _discover_player_key_from_lineup_team(api)
    if discovered and discovered not in keys_to_try:
        keys_to_try.append(discovered)

    if not keys_to_try:
        print(
            "No player_key. Pass argv[1], set YAHOO_TEST_PLAYER_KEY, or set LINEUP_TEAM_KEY for auto-discovery.\n"
            "Example: python3 scripts/test_player_season_stats.py 461.p.XXXXX 2025",
            file=sys.stderr,
        )
        return 1

    player_key: str | None = None
    for candidate in keys_to_try:
        probe = api.make_api_request(f"/player/{candidate}/stats;type=season;season={season}")
        if probe is not None:
            player_key = candidate
            print(f"Using player_key={player_key} (season={season})")
            break
        print(f"(skip invalid or inaccessible player_key: {candidate})")

    if not player_key:
        print(
            "All candidate player keys failed for template request. "
            "Check tokens and that the key matches your Yahoo game (e.g. 461.p.* for MLB).",
            file=sys.stderr,
        )
        return 2

    team_key = _configured_team_key()
    league_key = _league_key_from_team_key(team_key)
    sample_player_keys = _discover_roster_player_keys(api, team_key)
    if player_key not in sample_player_keys:
        sample_player_keys.insert(0, player_key)
    sample_player_keys = sample_player_keys[:3]
    sample_player_keys_param = ",".join(sample_player_keys)
    tests: list[dict] = [
        {
            "label": "player_season_explicit",
            "endpoint": f"/player/{player_key}/stats;type=season;season={season}",
            "summary": _summarize_player_payload,
            "dump_raw": True,
        },
        {
            "label": "player_prior_season",
            "endpoint": f"/player/{player_key}/stats;type=season;season={prior_season}",
            "summary": _summarize_player_payload,
        },
        {
            "label": "player_season_default",
            "endpoint": f"/player/{player_key}/stats;type=season",
            "summary": _summarize_player_payload,
        },
        {
            "label": "player_lastweek",
            "endpoint": f"/player/{player_key}/stats;type=lastweek",
            "summary": _summarize_player_payload,
        },
        {
            "label": "player_lastmonth",
            "endpoint": f"/player/{player_key}/stats;type=lastmonth",
            "summary": _summarize_player_payload,
        },
        {
            "label": "player_date",
            "endpoint": f"/player/{player_key}/stats;type=date;date={test_date}",
            "summary": _summarize_player_payload,
        },
        {
            "label": "player_week_1",
            "endpoint": f"/player/{player_key}/stats;type=week;week=1",
            "summary": _summarize_player_payload,
        },
        {
            "label": "player_percent_owned",
            "endpoint": f"/player/{player_key}/percent_owned",
            "summary": _summarize_generic_payload,
        },
        {
            "label": "player_draft_analysis",
            "endpoint": f"/player/{player_key}/draft_analysis",
            "summary": _summarize_generic_payload,
        },
        {
            "label": "player_combined_out",
            "endpoint": f"/player/{player_key};out=metadata,stats,percent_owned,draft_analysis",
            "summary": _summarize_player_payload,
        },
    ]

    if team_key:
        tests.extend(
            [
                {
                    "label": "team_roster_season_explicit",
                    "endpoint": f"/team/{team_key}/roster/players/stats;type=season;season={season}",
                    "summary": _summarize_roster_stats_payload,
                },
                {
                    "label": "team_roster_season_default",
                    "endpoint": f"/team/{team_key}/roster/players/stats;type=season",
                    "summary": _summarize_roster_stats_payload,
                },
                {
                    "label": "team_roster_lastweek",
                    "endpoint": f"/team/{team_key}/roster/players/stats;type=lastweek",
                    "summary": _summarize_roster_stats_payload,
                },
                {
                    "label": "team_roster_lastmonth",
                    "endpoint": f"/team/{team_key}/roster/players/stats;type=lastmonth",
                    "summary": _summarize_roster_stats_payload,
                },
            ]
        )

    if league_key:
        tests.extend(
            [
                {
                    "label": "league_taken_players_season_stats",
                    "endpoint": (
                        f"/league/{league_key}/players;status=T;sort=OR;sort_type=season;count=25"
                        f"/stats;type=season;season={season}"
                    ),
                    "summary": _summarize_league_players_payload,
                    "dump_raw": True,
                },
                {
                    "label": "league_free_agents_season_stats",
                    "endpoint": (
                        f"/league/{league_key}/players;status=FA;sort=OR;sort_type=season;count=25"
                        f"/stats;type=season;season={season}"
                    ),
                    "summary": _summarize_league_players_payload,
                },
                {
                    "label": "league_available_players_season_stats",
                    "endpoint": (
                        f"/league/{league_key}/players;status=A;sort=OR;sort_type=season;count=25"
                        f"/stats;type=season;season={season}"
                    ),
                    "summary": _summarize_league_players_payload,
                },
                {
                    "label": "league_context_player_stats_default",
                    "endpoint": f"/league/{league_key}/players;player_keys={player_key}/stats",
                    "summary": _summarize_league_players_payload,
                },
                {
                    "label": "league_context_player_stats_2025",
                    "endpoint": f"/league/{league_key}/players;player_keys={player_key}/stats;type=season;season={season}",
                    "summary": _summarize_league_players_payload,
                    "dump_raw": True,
                },
                {
                    "label": "league_context_multi_player_stats_2025",
                    "endpoint": (
                        f"/league/{league_key}/players;player_keys={sample_player_keys_param}"
                        f"/stats;type=season;season={season}"
                    ),
                    "summary": _summarize_league_players_payload,
                },
                {
                    "label": "league_context_player_ownership",
                    "endpoint": f"/league/{league_key}/players;player_keys={player_key}/ownership",
                    "summary": _summarize_generic_payload,
                },
            ]
        )

    print(
        json.dumps(
            {
                "player_key": player_key,
                "team_key": team_key,
                "league_key": league_key,
                "season": season,
                "prior_season": prior_season,
                "test_date": test_date,
                "sample_player_keys": sample_player_keys,
            },
            indent=2,
        )
    )

    failures = 0
    raw_dump_path = None
    for test in tests:
        label = test["label"]
        endpoint = test["endpoint"]
        result = api.make_api_request(endpoint)
        if result is None:
            failures += 1
            _print_result(label, endpoint, {"status": "no_result"}, success=False)
            continue
        summary = test["summary"](result)
        _print_result(label, endpoint, summary, success=True)
        if test.get("dump_raw"):
            raw_dump_path = _write_raw_payload(label, player_key, season, result)
            print(f"raw_payload_path={raw_dump_path}")

    print(
        json.dumps(
            {
                "tests_run": len(tests),
                "successes": len(tests) - failures,
                "failures": failures,
                "raw_payload_path": str(raw_dump_path) if raw_dump_path else None,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
