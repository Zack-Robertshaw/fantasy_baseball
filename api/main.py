"""
FastAPI backend for the Fantasy Baseball AI Co-Manager.
"""

import logging
import os
import sys
from datetime import datetime
from functools import partial
import html
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# Import from parent package - run with: uvicorn api.main:app --reload (from fantasy_baseball dir)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from batter_optimizer import (
    _blend_category_weights,
    _derive_category_weights,
    _week_blend_factor,
    evaluate_roster_trends,
    optimize_batting_lineup,
)
from lineup_optimizer import _parse_roster_player, optimize_lineup
from notifier import (
    format_optimization_email,
    get_last_email_cache_path,
    send_notification,
    should_notify_optimization_results,
)
from waiver_optimizer import get_add_drop_suggestions
from yahoo_api import YahooFantasyAPI

logger = logging.getLogger(__name__)

app = FastAPI(title="Fantasy Baseball AI Co-Manager")
_scheduler: BackgroundScheduler | None = None

ROBOT_LEAGUE_KEY = os.getenv("ROBOT_LEAGUE_KEY", "469.l.12479")
LHF_LEAGUE_KEY = os.getenv("LHF_LEAGUE_KEY", "469.l.15622")


class AddDropRequest(BaseModel):
    league_key: str
    team_key: str
    add_player_key: str
    drop_player_key: str
    faab_bid: int | None = None


class ExchangeRequest(BaseModel):
    code: str


def _team_is_owned_by_current_login(team_data: list | dict) -> bool:
    for element in team_data if isinstance(team_data, list) else [team_data]:
        if isinstance(element, list):
            if _team_is_owned_by_current_login(element):
                return True
            continue
        if not isinstance(element, dict):
            continue
        if element.get("is_owned_by_current_login") in {1, "1", True, "true"}:
            return True
        managers = element.get("managers")
        if isinstance(managers, list):
            for row in managers:
                manager = row.get("manager") if isinstance(row, dict) else None
                if isinstance(manager, dict) and manager.get("is_current_login") in {1, "1", True, "true"}:
                    return True
    return False


def _extract_current_login_team_profiles(league_key: str, teams_data: dict) -> list[dict]:
    profiles: list[dict] = []
    try:
        teams_obj = teams_data.get("teams", teams_data)
        for key, value in teams_obj.items():
            if not key.isdigit():
                continue
            team_data = value.get("team", [])
            if not _team_is_owned_by_current_login(team_data):
                continue
            team_key = None
            name = "My Team"
            for element in team_data:
                if isinstance(element, list):
                    for item in element:
                        if not isinstance(item, dict):
                            continue
                        if "team_key" in item:
                            team_key = item["team_key"]
                        elif "name" in item:
                            name = item["name"]
                elif isinstance(element, dict):
                    if "team_key" in element:
                        team_key = element["team_key"]
                    elif "name" in element:
                        name = element["name"]
            if team_key:
                profiles.append(
                    {
                        "label": name,
                        "league_key": league_key,
                        "team_key": team_key,
                    }
                )
    except (KeyError, TypeError, AttributeError):
        return []
    return profiles


def _configured_team_profiles() -> list[dict]:
    """
    Parse optional local saved team profiles.

    Format:
        LOCAL_TEAM_PROFILES=Label|league_key|team_key;Other Team|league_key|team_key
    """
    raw = os.getenv("LOCAL_TEAM_PROFILES", "").strip()
    profiles: list[dict] = []
    if not raw:
        return profiles

    for chunk in raw.split(";"):
        value = chunk.strip()
        if not value:
            continue
        parts = [part.strip() for part in value.split("|")]
        if len(parts) != 3:
            logger.warning("Ignoring invalid LOCAL_TEAM_PROFILES entry: %s", value)
            continue
        label, league_key, team_key = parts
        if not label or not league_key or not team_key:
            logger.warning("Ignoring incomplete LOCAL_TEAM_PROFILES entry: %s", value)
            continue
        if ".t." not in team_key:
            logger.warning("Ignoring LOCAL_TEAM_PROFILES entry with invalid team key: %s", value)
            continue
        derived_league_key = team_key.split(".t.")[0]
        if derived_league_key != league_key:
            logger.warning(
                "Ignoring LOCAL_TEAM_PROFILES entry with mismatched league/team: %s",
                value,
            )
            continue
        profiles.append(
            {
                "label": label,
                "league_key": league_key,
                "team_key": team_key,
            }
        )
    return profiles


def _configured_league_entries() -> list[dict]:
    profiles = _configured_team_profiles()
    if profiles:
        entries: list[dict] = []
        seen: set[str] = set()
        for profile in profiles:
            league_key = profile["league_key"].strip()
            if not league_key or league_key in seen:
                continue
            seen.add(league_key)
            entries.append({"league_key": league_key, "label": profile["label"]})
        return entries

    entries = []
    robot_league_key = ROBOT_LEAGUE_KEY.strip()
    if robot_league_key:
        entries.append({"league_key": robot_league_key, "label": "Robot League"})
    lhf_league_key = LHF_LEAGUE_KEY.strip()
    if lhf_league_key:
        entries.append({"league_key": lhf_league_key, "label": "Low Hanging Fruit"})
    return entries


def _resolved_team_profiles(api: YahooFantasyAPI | None = None) -> list[dict]:
    profiles = _configured_team_profiles()
    if profiles or api is None:
        return profiles

    resolved: list[dict] = []
    seen_team_keys: set[str] = set()
    for entry in _configured_league_entries():
        league_key = entry.get("league_key", "").strip()
        if not league_key:
            continue
        teams_raw = api.get_league_teams(league_key)
        if not teams_raw:
            continue
        for profile in _extract_current_login_team_profiles(league_key, {"teams": teams_raw}):
            team_key = profile["team_key"]
            if team_key in seen_team_keys:
                continue
            seen_team_keys.add(team_key)
            resolved.append(profile)
    return resolved


def _get_api() -> YahooFantasyAPI:
    client_id = os.getenv("YAHOO_CLIENT_ID")
    client_secret = os.getenv("YAHOO_CLIENT_SECRET")
    redirect_uri = os.getenv("YAHOO_REDIRECT_URI", "http://localhost:8000/auth/callback")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Yahoo credentials not configured")
    return YahooFantasyAPI(client_id, client_secret, redirect_uri)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        logger.warning("Ignoring invalid %s value: %s", name, os.getenv(name))
        return default


def _lineup_weights() -> tuple[float | None, float | None]:
    def parse_weight(name: str) -> float | None:
        raw_value = os.getenv(name)
        if raw_value is None or raw_value.strip() == "":
            return None
        try:
            return float(raw_value)
        except ValueError:
            logger.warning("Ignoring invalid %s value: %s", name, raw_value)
            return None

    return parse_weight("LINEUP_WEIGHT_7D"), parse_weight("LINEUP_WEIGHT_30D")


def _scheduled_today(tz_name: str) -> str:
    try:
        return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _lineup_schedule_times() -> list[tuple[int, int]]:
    raw_times = os.getenv("LINEUP_SCHEDULE_TIMES", "").strip()
    parsed: list[tuple[int, int]] = []

    if raw_times:
        for chunk in raw_times.split(","):
            value = chunk.strip()
            if not value:
                continue
            try:
                hour_text, minute_text = value.split(":", 1)
                hour = int(hour_text)
                minute = int(minute_text)
            except ValueError:
                logger.warning("Ignoring invalid LINEUP_SCHEDULE_TIMES entry: %s", value)
                continue
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                logger.warning("Ignoring out-of-range LINEUP_SCHEDULE_TIMES entry: %s", value)
                continue
            parsed.append((hour, minute))

    if parsed:
        return parsed

    try:
        hour = int(os.getenv("LINEUP_SCHEDULE_HOUR", "").strip() or "11")
    except ValueError:
        hour = 11
    try:
        minute = int(os.getenv("LINEUP_SCHEDULE_MINUTE", "").strip() or "0")
    except ValueError:
        minute = 0
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return [(hour, minute)]
    return [(11, 0), (17, 0)]


def _scheduled_team_profiles(api: YahooFantasyAPI) -> list[dict]:
    """
    Resolve the scheduler's team targets.

    If local team profiles or discoverable owned teams exist, run all of them.
    Otherwise fall back to the legacy single-team LINEUP_TEAM_KEY path.
    """
    profiles = _resolved_team_profiles(api)
    if profiles:
        return profiles

    team_key = os.getenv("LINEUP_TEAM_KEY", "").strip()
    if not team_key:
        return []

    configured_league_key = os.getenv("LINEUP_LEAGUE_KEY", "").strip()
    derived_league_key = team_key.split(".t.")[0]
    if configured_league_key and configured_league_key != derived_league_key:
        logger.warning(
            "LINEUP_LEAGUE_KEY (%s) does not match derived league key (%s) for team %s",
            configured_league_key,
            derived_league_key,
            team_key,
        )

    return [
        {
            "label": "Scheduled Team",
            "league_key": derived_league_key,
            "team_key": team_key,
        }
    ]


def _league_label(league_key: str | None) -> str:
    league_key = (league_key or "").strip()
    if league_key and league_key == ROBOT_LEAGUE_KEY.strip():
        return "Robot League"
    if league_key and league_key == LHF_LEAGUE_KEY.strip():
        return "Low Hanging Fruit"
    return league_key


def _empty_pitcher_optimization_result() -> dict:
    """Shape aligned with optimize_lineup return for combined-result merging."""
    return {
        "changes": [],
        "details": [],
        "applied": False,
        "apply_result": None,
        "error": None,
    }


def _lineup_category_weight_context(
    api: YahooFantasyAPI,
    league_key: str,
    team_key: str,
    roster_date: str,
) -> tuple[dict[str, float] | None, dict]:
    settings = api.get_league_scoring_settings(league_key) or {}
    batting_categories = settings.get("batting_categories") or []
    if not batting_categories:
        return None, {}

    season_records = api.get_team_category_records(league_key, team_key)
    current_week_records = api.get_current_week_category_standings(league_key, team_key)
    season_weights = _derive_category_weights(season_records, batting_categories)
    current_week_weights = _derive_category_weights(current_week_records, batting_categories)
    batting_category_names_by_id = {
        str(row.get("stat_id")): str(row.get("name") or row.get("stat_id"))
        for row in batting_categories
        if row.get("stat_id")
    }

    requested_blend_factor = _week_blend_factor(roster_date)
    blend_factor = requested_blend_factor if current_week_records else 0.0
    category_weights = _blend_category_weights(
        season_weights,
        current_week_weights,
        blend_factor,
    )

    return category_weights, {
        "blend_factor": blend_factor,
        "requested_blend_factor": requested_blend_factor,
        "season_category_records": season_records,
        "current_week_category_records": current_week_records,
        "season_category_weights": season_weights,
        "current_week_category_weights": current_week_weights,
        "blended_category_weights": category_weights,
        "batting_category_names_by_id": batting_category_names_by_id,
    }


def _run_combined_optimization(
    api: YahooFantasyAPI,
    team_key: str,
    date: str | None = None,
    dry_run: bool = True,
    weight_7d: float | None = None,
    weight_30d: float | None = None,
    include_pitchers: bool = True,
    category_weights: dict[str, float] | None = None,
    category_weight_context: dict | None = None,
) -> dict:
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    if include_pitchers:
        pitcher_result = optimize_lineup(api, team_key, date=roster_date, dry_run=dry_run)
    else:
        pitcher_result = _empty_pitcher_optimization_result()
    batter_result = optimize_batting_lineup(
        api,
        team_key,
        date=roster_date,
        dry_run=dry_run,
        weight_7d=weight_7d,
        weight_30d=weight_30d,
        category_weights=category_weights,
        category_weight_context=category_weight_context,
    )

    pitcher_changes = pitcher_result.get("changes") or []
    pitcher_details = pitcher_result.get("details") or []
    batter_changes = batter_result.get("changes") or []
    batter_details = [
        detail
        for detail in (batter_result.get("details") or [])
        if detail.get("action") == "move"
    ]

    errors = [error for error in (pitcher_result.get("error"), batter_result.get("error")) if error]
    applied_components = []
    if pitcher_changes:
        applied_components.append(bool(pitcher_result.get("applied")))
    if batter_changes:
        applied_components.append(bool(batter_result.get("applied")))

    summary = dict(batter_result.get("summary") or {})
    summary.update(
        {
            "date": roster_date,
            "team_key": team_key,
            "pitcher_changes_count": len(pitcher_changes),
            "batter_changes_count": len(batter_changes),
            "total_changes_count": len(pitcher_changes) + len(batter_changes),
        }
    )

    return {
        "changes": [*pitcher_changes, *batter_changes],
        "details": [*pitcher_details, *batter_details],
        "summary": summary,
        "applied": any(applied_components),
        "error": "; ".join(errors) if errors else None,
        "pitcher_changes": pitcher_changes,
        "pitcher_details": pitcher_details,
        "pitcher_applied": bool(pitcher_result.get("applied")),
        "pitcher_apply_result": pitcher_result.get("apply_result"),
        "pitcher_error": pitcher_result.get("error"),
        "batter_changes": batter_changes,
        "batter_details": batter_details,
        "batter_summary": batter_result.get("summary"),
        "batter_applied": bool(batter_result.get("applied")),
        "batter_apply_result": batter_result.get("apply_result"),
        "batter_error": batter_result.get("error"),
    }


def _run_scheduled_optimization(
    include_pitchers: bool = True,
    include_waiver_analysis: bool = True,
) -> None:
    timezone_name = os.getenv("LINEUP_SCHEDULE_TZ", "US/Eastern")
    auto_apply = _env_bool("LINEUP_AUTO_APPLY", False)
    weight_7d, weight_30d = _lineup_weights()
    waiver_enabled = _env_bool("WAIVER_ANALYSIS_ENABLED", False) and include_waiver_analysis
    waiver_fa_count = _env_int("WAIVER_FA_COUNT", 25)
    waiver_top_n = _env_int("WAIVER_TOP_N", 10)

    try:
        api = _get_api()
        team_profiles = _scheduled_team_profiles(api)
        if not team_profiles:
            return

        roster_date = _scheduled_today(timezone_name)
        scheduled_results = []
        for profile in team_profiles:
            team_key = profile["team_key"]
            league_key = profile.get("league_key") or team_key.split(".t.")[0]
            category_weights, category_weight_context = _lineup_category_weight_context(
                api,
                league_key,
                team_key,
                roster_date,
            )
            result = _run_combined_optimization(
                api,
                team_key,
                date=roster_date,
                dry_run=not auto_apply,
                weight_7d=weight_7d,
                weight_30d=weight_30d,
                include_pitchers=include_pitchers,
                category_weights=category_weights,
                category_weight_context=category_weight_context,
            )
            if waiver_enabled:
                try:
                    result["waiver_suggestions"] = get_add_drop_suggestions(
                        api,
                        team_key,
                        date=roster_date,
                        fa_count_per_position=waiver_fa_count,
                        top_n=waiver_top_n,
                        weight_7d=weight_7d,
                        weight_30d=weight_30d,
                    )
                except Exception as exc:
                    logger.exception(
                        "Waiver analysis failed: team=%s label=%s",
                        team_key,
                        profile.get("label"),
                    )
                    result["waiver_error"] = str(exc)
            logger.info(
                "Scheduled lineup optimization finished: team=%s label=%s dry_run=%s include_pitchers=%s waiver_enabled=%s pitcher_changes=%d batter_changes=%d total_changes=%d error=%s",
                team_key,
                profile.get("label"),
                not auto_apply,
                include_pitchers,
                waiver_enabled,
                len(result.get("pitcher_changes") or []),
                len(result.get("batter_changes") or []),
                (result.get("summary") or {}).get("total_changes_count", 0),
                result.get("error"),
            )
            scheduled_results.append(
                {
                    "label": profile.get("label"),
                    "league_label": _league_label(profile.get("league_key")),
                    "league_key": profile.get("league_key"),
                    "team_key": team_key,
                    "result": result,
                }
            )
        try:
            if should_notify_optimization_results(scheduled_results):
                subject, body_html = format_optimization_email(
                    scheduled_results,
                    dry_run=not auto_apply,
                )
                send_notification(subject, body_html)
        except Exception:
            logger.exception("Failed to prepare lineup notification email")
    except Exception:
        logger.exception("Scheduled lineup optimization failed")


def _allowed_league_keys() -> frozenset[str]:
    keys = {
        entry["league_key"].strip()
        for entry in _configured_league_entries()
        if entry.get("league_key")
    }
    return frozenset(keys)


@app.get("/api/auth/status")
def auth_status():
    """Check if user is authenticated."""
    api = _get_api()
    return {"authenticated": api._ensure_valid_token()}


@app.get("/api/auth/url")
def get_auth_url():
    """Get Yahoo OAuth URL for redirect."""
    api = _get_api()
    return {"url": api.get_auth_url()}


@app.get("/auth/callback", response_class=HTMLResponse)
def auth_callback(code: str = Query(...)):
    """OAuth callback - exchange code for token."""
    api = _get_api()
    if api.exchange_code_for_token(code):
        return HTMLResponse(
            "<html><body><h1>Yahoo authentication complete</h1>"
            "<p>You can close this tab.</p></body></html>"
        )
    return HTMLResponse(
        "<html><body><h1>Yahoo authentication failed</h1>"
        "<p>Check the service logs and try again.</p></body></html>",
        status_code=400,
    )


@app.post("/api/auth/exchange")
def exchange_code(req: ExchangeRequest):
    """Exchange auth code for token (for SPA flow)."""
    api = _get_api()
    if api.exchange_code_for_token(req.code):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Failed to exchange code")


@app.get("/api/leagues/debug")
def leagues_debug():
    """Debug: return raw Yahoo API response for leagues."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"raw": api.get_user_leagues(game_key=None)}


@app.get("/api/debug/scoreboard")
def scoreboard_debug(league_key: str = Query(...), team_key: str = Query(...)):
    """
    Debug: fetch the live current-week scoreboard and show both the raw Yahoo
    response and what the category W/L/T parser extracts from it.

    Use this to verify what data Yahoo exposes for an in-progress matchup week
    before building features that depend on it.
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    current_week = api._league_current_week(league_key)

    settings = api.get_league_scoring_settings(league_key) or {}
    stat_names_by_id = {
        str(row.get("stat_id")): row.get("name")
        for group in (
            settings.get("batting_categories") or [],
            settings.get("pitching_categories") or [],
        )
        for row in group
        if isinstance(row, dict) and row.get("stat_id")
    }

    raw_response = api.make_api_request(f"/league/{league_key}/scoreboard;week={current_week}")
    parsed_rows = (
        api._team_category_results_from_scoreboard(raw_response, team_key, stat_names_by_id)
        if raw_response
        else []
    )

    category_outcomes: dict[str, str] = {}
    for row in parsed_rows:
        category = str(row.get("category") or row.get("stat_id") or "").strip()
        outcome = str(row.get("outcome") or "").upper()
        if category and outcome in {"W", "L", "T"}:
            category_outcomes[category] = outcome

    return {
        "current_week": current_week,
        "league_key": league_key,
        "team_key": team_key,
        "stat_names_by_id": stat_names_by_id,
        "parsed_category_outcomes": category_outcomes,
        "parsed_rows_raw": parsed_rows,
        "raw_scoreboard": raw_response,
    }


@app.get("/api/teams/{team_key}/roster")
def get_roster(team_key: str, date: str | None = None):
    """Get team roster, optionally for a date."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    roster = api.get_team_roster(team_key, date=roster_date)
    if roster is None:
        return {"players": []}

    players = []
    for key, value in roster.items():
        if key.isdigit():
            player = _parse_roster_player(value)
            if player:
                players.append(player)
    return {"players": players, "date": roster_date}


@app.get("/api/optimize-lineup")
def optimize_lineup_get(
    team_key: str = Query(...),
    date: str | None = None,
    dry_run: bool = Query(True),
):
    """Get or apply optimal lineup changes (SP focus)."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    return optimize_lineup(api, team_key, date=roster_date, dry_run=dry_run)


@app.get("/api/roster/trends")
def roster_trends_get(
    team_key: str = Query(...),
    date: str | None = None,
):
    """Evaluate roster batter trends without applying lineup changes."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    weight_7d, weight_30d = _lineup_weights()
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    return evaluate_roster_trends(
        api,
        team_key,
        date=roster_date,
        weight_7d=weight_7d,
        weight_30d=weight_30d,
    )


@app.post("/api/transactions/add-drop")
def add_drop(req: AddDropRequest):
    """Execute add/drop transaction."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    allowed_leagues = _allowed_league_keys()
    if req.league_key.strip() not in allowed_leagues:
        raise HTTPException(
            status_code=400,
            detail=f"league_key must be one of: {sorted(allowed_leagues)}",
        )

    resp = api.add_drop_players(
        req.league_key,
        req.team_key,
        req.add_player_key,
        req.drop_player_key,
        faab_bid=req.faab_bid,
    )
    if resp is None:
        raise HTTPException(status_code=400, detail="Transaction failed")
    return {"success": True}


@app.get("/last-email", response_class=HTMLResponse)
def last_email():
    """Display the most recently sent lineup notification email."""
    cache_path = get_last_email_cache_path()
    if not cache_path.exists():
        return HTMLResponse(
            "<html><body><h1>No email sent yet</h1>"
            "<p>The scheduled job has not sent an email since this cache was added.</p>"
            "</body></html>"
        )

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("Failed to read last email cache: %s", cache_path)
        raise HTTPException(status_code=500, detail="Could not read last sent email") from exc

    subject = html.escape(str(payload.get("subject") or "Last sent email"))
    sent_at = html.escape(str(payload.get("sent_at") or "Unknown send time"))
    body_html = str(payload.get("html") or "")
    iframe_body = html.escape(body_html, quote=True)
    page = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<title>Last Fantasy Baseball Email</title>"
        "<style>"
        "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;color:#111;}"
        "header{padding:16px 20px;background:#111;color:#fff;}"
        "header h1{margin:0 0 6px;font-size:20px;}"
        "header p{margin:0;color:#d0d0d0;}"
        "iframe{display:block;width:100%;height:calc(100vh - 86px);border:0;background:#fff;}"
        "</style></head><body>"
        f"<header><h1>{subject}</h1><p>Sent at: {sent_at}</p></header>"
        f"<iframe title=\"Last sent email\" srcdoc=\"{iframe_body}\"></iframe>"
        "</body></html>"
    )
    return HTMLResponse(page)


@app.on_event("startup")
def startup_scheduler() -> None:
    global _scheduler
    team_key = os.getenv("LINEUP_TEAM_KEY", "").strip()
    if not team_key or _scheduler is not None:
        return
    timezone_name = os.getenv("LINEUP_SCHEDULE_TZ", "US/Eastern")
    schedule_times = _lineup_schedule_times()

    rest_slots_batters_only = _env_bool("LINEUP_REST_SLOTS_BATTERS_ONLY", False)
    _scheduler = BackgroundScheduler(timezone=timezone_name)
    for index, (hour, minute) in enumerate(schedule_times):
        include_pitchers = (not rest_slots_batters_only) or (index == 0)
        job = partial(
            _run_scheduled_optimization,
            include_pitchers=include_pitchers,
            include_waiver_analysis=(index == 0),
        )
        _scheduler.add_job(
            job,
            trigger="cron",
            hour=hour,
            minute=minute,
            id=f"daily-lineup-optimization-{index}",
            replace_existing=True,
        )
    _scheduler.start()
    schedule_labels = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in schedule_times)
    logger.info(
        "Started daily lineup optimization scheduler for team=%s at %s %s (LINEUP_REST_SLOTS_BATTERS_ONLY=%s)",
        team_key,
        schedule_labels,
        timezone_name,
        rest_slots_batters_only,
    )


@app.on_event("shutdown")
def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
