"""
FastAPI backend for the Fantasy Baseball AI Co-Manager.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Import from parent package - run with: uvicorn api.main:app --reload (from fantasy_baseball dir)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from batter_optimizer import optimize_batting_lineup
from lineup_optimizer import _parse_roster_player, optimize_lineup
from yahoo_api import YahooFantasyAPI

logger = logging.getLogger(__name__)

app = FastAPI(title="Fantasy Baseball AI Co-Manager")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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


def _lineup_weights() -> tuple[float, float]:
    try:
        weight_7d = float(os.getenv("LINEUP_WEIGHT_7D", "0.6"))
    except ValueError:
        weight_7d = 0.6
    try:
        weight_30d = float(os.getenv("LINEUP_WEIGHT_30D", "0.4"))
    except ValueError:
        weight_30d = 0.4

    total = weight_7d + weight_30d
    if total <= 0:
        return 0.6, 0.4
    return weight_7d / total, weight_30d / total


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


def _run_combined_optimization(
    api: YahooFantasyAPI,
    team_key: str,
    date: str | None = None,
    dry_run: bool = True,
    weight_7d: float = 0.6,
    weight_30d: float = 0.4,
) -> dict:
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    pitcher_result = optimize_lineup(api, team_key, date=roster_date, dry_run=dry_run)
    batter_result = optimize_batting_lineup(
        api,
        team_key,
        date=roster_date,
        dry_run=dry_run,
        weight_7d=weight_7d,
        weight_30d=weight_30d,
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
        "applied": bool(applied_components) and all(applied_components),
        "error": "; ".join(errors) if errors else None,
        "pitcher_changes": pitcher_changes,
        "pitcher_details": pitcher_details,
        "pitcher_applied": bool(pitcher_result.get("applied")),
        "pitcher_error": pitcher_result.get("error"),
        "batter_changes": batter_changes,
        "batter_details": batter_details,
        "batter_summary": batter_result.get("summary"),
        "batter_applied": bool(batter_result.get("applied")),
        "batter_error": batter_result.get("error"),
    }


def _run_scheduled_optimization() -> None:
    timezone_name = os.getenv("LINEUP_SCHEDULE_TZ", "US/Eastern")
    auto_apply = _env_bool("LINEUP_AUTO_APPLY", False)
    weight_7d, weight_30d = _lineup_weights()

    try:
        api = _get_api()
        team_profiles = _scheduled_team_profiles(api)
        if not team_profiles:
            return

        roster_date = _scheduled_today(timezone_name)
        for profile in team_profiles:
            team_key = profile["team_key"]
            result = _run_combined_optimization(
                api,
                team_key,
                date=roster_date,
                dry_run=not auto_apply,
                weight_7d=weight_7d,
                weight_30d=weight_30d,
            )
            logger.info(
                "Scheduled lineup optimization finished: team=%s label=%s dry_run=%s pitcher_changes=%d batter_changes=%d total_changes=%d error=%s",
                team_key,
                profile.get("label"),
                not auto_apply,
                len(result.get("pitcher_changes") or []),
                len(result.get("batter_changes") or []),
                (result.get("summary") or {}).get("total_changes_count", 0),
                result.get("error"),
            )
    except Exception:
        logger.exception("Scheduled lineup optimization failed")


def _extract_leagues(fantasy_content: dict) -> list[dict]:
    """Parse Yahoo fantasy_content to extract user leagues (recursive search)."""
    leagues = []
    seen = set()

    def find_leagues(obj):
        if isinstance(obj, dict):
            if "league_key" in obj and "name" in obj:
                key = obj.get("league_key")
                if key and key not in seen:
                    seen.add(key)
                    leagues.append(
                        {
                            "league_key": key,
                            "name": obj.get("name", ""),
                            "num_teams": obj.get("num_teams"),
                        }
                    )
            for value in obj.values():
                find_leagues(value)
        elif isinstance(obj, list):
            for value in obj:
                find_leagues(value)

    find_leagues(fantasy_content)
    return leagues


def _extract_teams(teams_data: dict) -> list[dict]:
    """Parse Yahoo teams response to list of {team_key, name}."""
    teams = []
    try:
        teams_obj = teams_data.get("teams", teams_data)
        for key, value in teams_obj.items():
            if not key.isdigit():
                continue
            team_data = value.get("team", [])
            team_key = None
            name = "Unknown"
            for element in team_data:
                if isinstance(element, list):
                    for item in element:
                        if isinstance(item, dict):
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
                teams.append({"team_key": team_key, "name": name})
    except (KeyError, TypeError, AttributeError):
        pass
    return teams


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


@app.get("/auth/callback")
def auth_callback(code: str = Query(...)):
    """OAuth callback - exchange code for token. Redirect to frontend after."""
    api = _get_api()
    if api.exchange_code_for_token(code):
        return RedirectResponse(url="/?auth=success")
    return RedirectResponse(url="/?auth=failed")


@app.post("/api/auth/exchange")
def exchange_code(req: ExchangeRequest):
    """Exchange auth code for token (for SPA flow)."""
    api = _get_api()
    if api.exchange_code_for_token(req.code):
        return {"success": True}
    raise HTTPException(status_code=400, detail="Failed to exchange code")


@app.get("/api/leagues")
def list_leagues():
    """List user's MLB leagues."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    content = api.get_user_leagues(game_key=None)
    if not content:
        return {"leagues": []}
    return {"leagues": _extract_leagues(content)}


@app.get("/api/leagues/configured")
def configured_leagues():
    """Return the app-supported configured leagues for the UI."""
    return {"leagues": _configured_league_entries()}


@app.get("/api/teams/configured")
def configured_teams():
    """Return optional saved local team profiles for faster switching in the UI."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"teams": _resolved_team_profiles(api)}


def _run_all_configured_optimizations(
    api: YahooFantasyAPI,
    date: str | None = None,
    dry_run: bool = True,
) -> dict:
    profiles = _resolved_team_profiles(api)
    if not profiles:
        raise HTTPException(
            status_code=400,
            detail="No configured or discoverable local teams were found",
        )

    weight_7d, weight_30d = _lineup_weights()
    results: list[dict] = []
    errors: list[str] = []
    teams_with_changes = 0

    for profile in profiles:
        result = _run_combined_optimization(
            api,
            profile["team_key"],
            date=date,
            dry_run=dry_run,
            weight_7d=weight_7d,
            weight_30d=weight_30d,
        )
        if (result.get("summary") or {}).get("total_changes_count", 0) > 0:
            teams_with_changes += 1
        if result.get("error"):
            errors.append(f'{profile["label"]}: {result["error"]}')
        results.append(
            {
                "label": profile["label"],
                "league_key": profile["league_key"],
                "team_key": profile["team_key"],
                **result,
            }
        )

    return {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "dry_run": dry_run,
        "results": results,
        "teams_processed": len(results),
        "teams_with_changes": teams_with_changes,
        "errors": errors,
    }


@app.get("/api/leagues/debug")
def leagues_debug():
    """Debug: return raw Yahoo API response for leagues."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"raw": api.get_user_leagues(game_key=None)}


@app.get("/api/leagues/{league_key}/teams")
def list_teams(league_key: str):
    """List teams in a league."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    teams_raw = api.get_league_teams(league_key)
    if not teams_raw:
        return {"teams": []}
    return {"teams": _extract_teams({"teams": teams_raw})}


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


@app.post("/api/optimize-batting-lineup")
def optimize_batting_lineup_post(
    team_key: str = Query(...),
    date: str | None = None,
    dry_run: bool = Query(True),
):
    """Preview or apply combined pitcher and batter lineup optimization."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    weight_7d, weight_30d = _lineup_weights()
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    return _run_combined_optimization(
        api,
        team_key,
        date=roster_date,
        dry_run=dry_run,
        weight_7d=weight_7d,
        weight_30d=weight_30d,
    )


@app.post("/api/optimize-all-lineups")
def optimize_all_lineups_post(
    date: str | None = None,
    dry_run: bool = Query(True),
):
    """Preview or apply combined pitcher and batter optimization for all local teams."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    roster_date = date or datetime.now().strftime("%Y-%m-%d")
    return _run_all_configured_optimizations(
        api,
        date=roster_date,
        dry_run=dry_run,
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


@app.on_event("startup")
def startup_scheduler() -> None:
    global _scheduler
    team_key = os.getenv("LINEUP_TEAM_KEY", "").strip()
    if not team_key or _scheduler is not None:
        return
    timezone_name = os.getenv("LINEUP_SCHEDULE_TZ", "US/Eastern")
    schedule_times = _lineup_schedule_times()

    _scheduler = BackgroundScheduler(timezone=timezone_name)
    for index, (hour, minute) in enumerate(schedule_times):
        _scheduler.add_job(
            _run_scheduled_optimization,
            trigger="cron",
            hour=hour,
            minute=minute,
            id=f"daily-lineup-optimization-{index}",
            replace_existing=True,
        )
    _scheduler.start()
    schedule_labels = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in schedule_times)
    logger.info(
        "Started daily lineup optimization scheduler for team=%s at %s %s",
        team_key,
        schedule_labels,
        timezone_name,
    )


@app.on_event("shutdown")
def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
