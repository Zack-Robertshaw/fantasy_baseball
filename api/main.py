"""
FastAPI backend for the Fantasy Baseball AI Co-Manager.
"""

import json
import os
import re
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Import from parent package - run with: uvicorn api.main:app --reload (from fantasy_baseball dir)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yahoo_api import YahooFantasyAPI, _classify_strategy_tier, _ascii_normalize
from recommendations import (
    get_callup_recommendations,
    get_league_na_stashes,
    get_na_add_recommendations,
)
from lineup_optimizer import optimize_lineup, get_optimal_lineup_changes, _parse_roster_player
from mlb_client import get_player_age_lookup
from lhf_league_config import get_lhf_config
from lhf_draft import analyze_draft_picks, score_candidate, DraftPick

# Yahoo Fantasy player_key format, e.g. 469.p.12345
_YAHOO_PLAYER_KEY_RE = re.compile(r"^\d+\.p\.\d+$")

app = FastAPI(title="Fantasy Baseball AI Co-Manager")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AddDropRequest(BaseModel):
    league_key: str
    team_key: str
    add_player_key: str
    drop_player_key: str
    faab_bid: int | None = None


def _get_api() -> YahooFantasyAPI:
    client_id = os.getenv("YAHOO_CLIENT_ID")
    client_secret = os.getenv("YAHOO_CLIENT_SECRET")
    redirect_uri = os.getenv("YAHOO_REDIRECT_URI", "http://localhost:8000/auth/callback")
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Yahoo credentials not configured")
    return YahooFantasyAPI(client_id, client_secret, redirect_uri)


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
                    leagues.append({
                        "league_key": key,
                        "name": obj.get("name", ""),
                        "num_teams": obj.get("num_teams"),
                    })
            for v in obj.values():
                find_leagues(v)
        elif isinstance(obj, list):
            for v in obj:
                find_leagues(v)

    find_leagues(fantasy_content)
    return leagues


def _extract_teams(teams_data: dict) -> list[dict]:
    """Parse Yahoo teams response to list of {team_key, name}."""
    teams = []
    try:
        teams_obj = teams_data.get("teams", teams_data)
        for k, v in teams_obj.items():
            if not k.isdigit():
                continue
            t = v.get("team", [])
            team_key = None
            name = "Unknown"
            for elem in t:
                if isinstance(elem, list):
                    for item in elem:
                        if isinstance(item, dict):
                            if "team_key" in item:
                                team_key = item["team_key"]
                            elif "name" in item:
                                name = item["name"]
                elif isinstance(elem, dict):
                    if "team_key" in elem:
                        team_key = elem["team_key"]
                    elif "name" in elem:
                        name = elem["name"]
            if team_key:
                teams.append({"team_key": team_key, "name": name})
    except (KeyError, TypeError, AttributeError):
        pass
    return teams


@app.get("/api/auth/status")
def auth_status():
    """Check if user is authenticated."""
    api = _get_api()
    if api._ensure_valid_token():
        return {"authenticated": True}
    return {"authenticated": False}


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


class ExchangeRequest(BaseModel):
    code: str


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
    # Fetch from all games (no game_keys filter) to get MLB and other leagues
    content = api.get_user_leagues(game_key=None)
    if not content:
        return {"leagues": []}
    leagues = _extract_leagues(content)
    return {"leagues": leagues}


@app.get("/api/leagues/configured")
def configured_leagues():
    """
    The two app-supported Yahoo leagues (keys from env): Robot League and Low Hanging Fruit.
    Use these to populate the UI when filtering the full league list.
    """
    out = [
        {"league_key": ROBOT_LEAGUE_KEY.strip(), "label": "Robot League"},
    ]
    if LHF_LEAGUE_KEY.strip():
        out.append({"league_key": LHF_LEAGUE_KEY.strip(), "label": "Low Hanging Fruit"})
    return {"leagues": out}


@app.get("/api/leagues/debug")
def leagues_debug():
    """Debug: return raw Yahoo API response for leagues (helps diagnose empty leagues)."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    content = api.get_user_leagues(game_key=None)
    return {"raw": content}


@app.get("/api/leagues/{league_key}/teams")
def list_teams(league_key: str):
    """List teams in a league."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    teams_raw = api.get_league_teams(league_key)
    if not teams_raw:
        return {"teams": []}
    teams = _extract_teams({"teams": teams_raw})  # wrap for _extract_teams
    return {"teams": teams}


@app.get("/api/teams/{team_key}/roster")
def get_roster(team_key: str, date: str | None = None):
    """Get team roster, optionally for a date."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    date = date or datetime.now().strftime("%Y-%m-%d")
    roster = api.get_team_roster(team_key, date=date)
    if roster is None:
        return {"players": []}
    players = []
    for k, v in roster.items():
        if k.isdigit():
            p = _parse_roster_player(v)
            if p:
                players.append(p)
    return {"players": players, "date": date}


@app.get("/api/leagues/{league_key}/na-stashes")
def league_na_stashes(league_key: str):
    """
    All players currently in an NA (Not Active) roster slot, every team in the league.
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    stashes = get_league_na_stashes(api, league_key)
    return {"league_key": league_key, "stashes": stashes, "count": len(stashes)}


@app.get("/api/leagues/{league_key}/na-adds")
def league_na_adds(
    league_key: str,
    days: int = Query(7, ge=1, le=14, description="Days of MLB transactions for call-up urgency"),
    count: int = Query(50, ge=10, le=250, description="Max NA-eligible FA players to return"),
):
    """
    NA-eligible free agents sorted by Yahoo overall rank, with call-up urgency flags.
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    data = get_na_add_recommendations(api, league_key, days=days, count=count)
    return {"league_key": league_key, **data}


@app.get("/api/recommendations/callups")
def recommendations_callups(league_key: str = Query(...), days: int = Query(3, ge=1, le=7)):
    """Get call-up recommendations (players recently promoted, available on waivers)."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    recs = get_callup_recommendations(api, league_key, days=days)
    return {"recommendations": recs}


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
    date = date or datetime.now().strftime("%Y-%m-%d")
    result = optimize_lineup(api, team_key, date=date, dry_run=dry_run)
    return result


ROBOT_LEAGUE_KEY = os.getenv("ROBOT_LEAGUE_KEY", "469.l.12479")

# Low Hanging Fruit — default matches canonical league key (override in .env if needed)
LHF_LEAGUE_KEY = os.getenv("LHF_LEAGUE_KEY", "469.l.15622")


def _allowed_league_keys() -> frozenset[str]:
    keys = {ROBOT_LEAGUE_KEY.strip()}
    if LHF_LEAGUE_KEY.strip():
        keys.add(LHF_LEAGUE_KEY.strip())
    return frozenset(keys)


def _parse_league_key(league_key: str | None) -> str:
    """Resolve league_key query param; default to Robot League for backward compatibility."""
    if league_key and str(league_key).strip():
        lk = str(league_key).strip()
        if lk not in _allowed_league_keys():
            raise HTTPException(
                status_code=400,
                detail=f"league_key must be one of: {sorted(_allowed_league_keys())}",
            )
        return lk
    return ROBOT_LEAGUE_KEY.strip()


def _ensure_team_in_league(api: YahooFantasyAPI, league_key: str, team_key: str) -> None:
    teams_raw = api.get_league_teams(league_key)
    if teams_raw is None:
        raise HTTPException(status_code=502, detail="Could not fetch league teams")
    teams = _extract_teams({"teams": teams_raw})
    if not any(t["team_key"] == team_key for t in teams):
        raise HTTPException(
            status_code=400,
            detail="team_key does not belong to the selected league",
        )


def _category_role_nudge(
    positions: list[str],
    batting: list[dict],
    pitching: list[dict],
) -> float:
    """Phase B: small multiplier from league scoring categories vs player role."""
    pos_upper = {str(p).upper() for p in (positions or [])}
    has_pitch = bool(pos_upper & {"SP", "RP", "P"})
    has_hit = bool(
        pos_upper
        & {"C", "1B", "2B", "3B", "SS", "OF", "Util", "DH", "MI", "CI"}
    )

    def cat_text(cats: list[dict]) -> str:
        return " ".join(
            str(c.get("name", "")).lower() for c in (cats or []) if isinstance(c, dict)
        )

    bn, pn = cat_text(batting), cat_text(pitching)
    nudge = 1.0

    if has_pitch and not has_hit:
        if "qs" in pn or "quality" in pn:
            if "SP" in pos_upper:
                nudge += 0.04
        if "k/9" in pn or "k9" in pn.replace(" ", ""):
            if "SP" in pos_upper or "P" in pos_upper:
                nudge += 0.03
        if "hold" in pn or "hld" in pn:
            if "RP" in pos_upper:
                nudge += 0.05
        if "save" in pn and "hold" not in pn and "hld" not in pn:
            if "RP" in pos_upper:
                nudge += 0.03
    if has_hit or not has_pitch:
        if "obp" in bn or "on-base" in bn:
            nudge += 0.025
        if "sb" in bn or "stolen" in bn:
            nudge += 0.025
        if "slg" in bn or "slug" in bn:
            nudge += 0.02

    return max(0.95, min(1.08, nudge))


def _enrich_player_with_category_nudge(
    player: dict,
    batting: list[dict],
    pitching: list[dict],
) -> dict:
    nudge = _category_role_nudge(player.get("positions") or [], batting, pitching)
    st = player.get("strategy_tier")
    out = {**player, "category_nudge": round(nudge, 4)}
    if not isinstance(st, dict):
        return out
    ws, sh, ks = st.get("win_now_score"), st.get("sell_high_score"), st.get("keeper_score")
    if ws is not None:
        out["adjusted_win_now_score"] = round(ws * nudge, 2)
    if sh is not None:
        out["adjusted_sell_high_score"] = round(sh * nudge, 2)
    if ks is not None:
        out["adjusted_keeper_score"] = round(ks * nudge, 2)
    return out


def _require_lhf_league_key() -> str:
    if not LHF_LEAGUE_KEY.strip():
        raise HTTPException(
            status_code=400,
            detail="LHF_LEAGUE_KEY is not set. Add it to .env (Yahoo league key for your LHF league).",
        )
    return LHF_LEAGUE_KEY.strip()


class LHFPicksRequest(BaseModel):
    """Draft picks so far (player_key + positions from Yahoo)."""

    picks: list[dict] = []


class LHFAIContextRequest(BaseModel):
    """Paste-ready context for an external AI chat. Prefer team_key to load Yahoo roster."""

    team_key: str | None = None
    picks: list[dict] = []
    exclude_yahoo_drafted: bool = False
    count: int = 100


def _name_keys_for_match(name: str) -> set[str]:
    """Lowercased variants for matching manual names to Yahoo ranking rows."""
    if not name or not str(name).strip():
        return set()
    s = str(name).strip().lower()
    out = {s}
    out.add(_ascii_normalize(str(name)).strip().lower())
    return {x for x in out if x}


def _manual_pick_names_for_exclusion(raw: list[dict]) -> set[str]:
    """Names typed without a Yahoo player_key — exclude from recommendations by name match."""
    keys: set[str] = set()
    for p in raw:
        pk = str(p.get("player_key") or p.get("playerKey") or "").strip()
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        if _YAHOO_PLAYER_KEY_RE.match(pk):
            continue
        keys |= _name_keys_for_match(name)
    return keys


def _parse_lhf_picks(raw: list[dict]) -> list[DraftPick]:
    """Accept Yahoo picks (player_key) or manual rows (name + positions only)."""
    out: list[DraftPick] = []
    for i, p in enumerate(raw):
        pk = str(p.get("player_key") or p.get("playerKey") or "").strip()
        name = str(p.get("name") or "").strip()
        pos = p.get("positions") or []
        if isinstance(pos, str):
            pos = [x.strip() for x in pos.split(",") if x.strip()]
        if not isinstance(pos, list):
            pos = []
        if not pk:
            if not name:
                continue
            pk = f"lhf:manual:{i}"
        out.append(
            DraftPick(
                player_key=pk,
                name=name,
                positions=pos,
            )
        )
    return out


def _yahoo_roster_to_lhf_picks(api: YahooFantasyAPI, team_key: str) -> list[dict]:
    """Build LHF pick dicts from current Yahoo roster (eligible positions)."""
    roster_raw = api.get_team_roster(team_key)
    if not roster_raw:
        return []
    out: list[dict] = []
    for k, v in roster_raw.items():
        if not k.isdigit():
            continue
        p = _parse_roster_player_full(v)
        if not p:
            continue
        out.append(
            {
                "player_key": p["player_key"],
                "name": p["name"],
                "positions": p["positions"],
            }
        )
    return out


def _build_lhf_ai_markdown(
    cfg,
    picks_raw: list[dict],
    rec_payload: dict,
) -> str:
    """Single blob to paste into ChatGPT / Cursor / any LLM."""
    lines = [
        "# Fantasy baseball — LHF draft assistant context",
        "",
        "Use this to suggest the next pick or compare players. League is **categories** (not points).",
        "",
        "## Roster slots (active lineup)",
    ]
    r = cfg.roster_slots
    lines.append(
        f"- Hitters: C×{r.get('C', 0)} 1B×{r.get('1B', 0)} 2B×{r.get('2B', 0)} 3B×{r.get('3B', 0)} "
        f"SS×{r.get('SS', 0)} OF×{r.get('OF', 0)} Util×{r.get('Util', 0)}"
    )
    lines.append(
        f"- Pitchers: SP×{r.get('SP', 0)} RP×{r.get('RP', 0)} P×{r.get('P', 0)} · BN×{r.get('BN', 0)} IL×{r.get('IL', 0)}"
    )
    lines.extend(["", "## Scoring categories", ""])
    lines.append("**Pitching:** " + ", ".join(cfg.pitching_categories))
    lines.append("**Batting:** " + ", ".join(cfg.batting_categories))
    lines.extend(["", "## My players so far", ""])
    if not picks_raw:
        lines.append("(none — empty roster or sync failed)")
    else:
        for p in picks_raw:
            pos = ", ".join(p.get("positions") or [])
            pk = str(p.get("player_key") or "")
            if pk.startswith("lhf:manual:") or not pk:
                lines.append(f"- {p.get('name', '?')} — {pos} — *(manual entry)*")
            else:
                lines.append(f"- {p.get('name', '?')} — {pos} — `{pk}`")
    rem = rec_payload.get("remaining_slots") or {}
    lines.extend(["", "## Remaining lineup slots (approx.)", ""])
    lines.append(
        f"C {rem.get('C', 0)} · 1B {rem.get('1B', 0)} · 2B {rem.get('2B', 0)} · 3B {rem.get('3B', 0)} · "
        f"SS {rem.get('SS', 0)} · OF {rem.get('OF', 0)} · Util {rem.get('Util', 0)} · "
        f"SP {rem.get('SP', 0)} · RP {rem.get('RP', 0)} · P {rem.get('P', 0)} · BN {rem.get('BN', 0)} · IL {rem.get('IL', 0)}"
    )
    need = rec_payload.get("positions_of_need") or []
    if need:
        lines.extend(["", "## Positional needs (fill these first when possible)", ""])
        lines.append(", ".join(need[:40]) + (" …" if len(need) > 40 else ""))
    recs = rec_payload.get("recommendations") or []
    lines.extend(["", "## Suggested targets (Yahoo OR + positional need boost)", ""])
    if not recs:
        lines.append("(none — check LHF_LEAGUE_KEY and auth)")
    else:
        for i, p in enumerate(recs[:20], 1):
            pos = ", ".join(p.get("positions") or [])
            lines.append(
                f"{i}. **{p.get('name')}** (OR {p.get('rank')}) — {pos} — "
                f"combined_score {p.get('combined_score')} (base {p.get('base_score')} + need {p.get('positional_boost')})"
            )
    lines.extend(
        [
            "",
            "## Instructions for the assistant",
            "Recommend **one or two** next picks with a short rationale tied to positional needs and category balance.",
            "Do not assume keeper rules; this is a redraft league.",
        ]
    )
    return "\n".join(lines)


def _build_lhf_recommendations(
    api: YahooFantasyAPI,
    league_key: str,
    picks_raw: list[dict],
    exclude_yahoo_drafted: bool,
    count: int,
) -> dict:
    cfg = get_lhf_config()
    picks = _parse_lhf_picks(picks_raw)
    analysis = analyze_draft_picks(picks, cfg)
    positions_of_need = analysis["positions_of_need"]

    excluded_keys: set[str] = {p.player_key for p in picks}
    excluded_names = _manual_pick_names_for_exclusion(picks_raw)
    if exclude_yahoo_drafted:
        dr = api.get_draft_results(league_key)
        for row in dr:
            pk = row.get("player_key")
            if pk:
                excluded_keys.add(pk)

    rankings = api.get_player_rankings(league_key, count=count, age_lookup=None)
    candidates = []
    for row in rankings:
        pk = row.get("player_key")
        rname = str(row.get("name") or "").strip()
        if excluded_names:
            rk = _name_keys_for_match(rname)
            if rk & excluded_names:
                continue
        if not pk or pk in excluded_keys:
            continue
        positions = row.get("positions") or []
        if not isinstance(positions, list):
            positions = []
        rk = int(row.get("rank") or 999)
        sc = score_candidate(rk, positions, positions_of_need)
        candidates.append(
            {
                "player_key": pk,
                "name": row.get("name"),
                "team": row.get("team"),
                "positions": positions,
                "rank": rk,
                **sc,
            }
        )

    candidates.sort(key=lambda x: (-x["combined_score"], x["rank"]))
    return {
        "league_key": league_key,
        "pick_count": len(picks),
        "positions_of_need": positions_of_need,
        "remaining_slots": analysis["remaining_slots"],
        "summary": analysis["summary"],
        "recommendations": candidates[: min(30, len(candidates))],
    }

# Default roster slots (typical 12-team league). Used to compute positions of need from keepers.
DEFAULT_ROSTER_REQUIRED = {
    "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1,
    "OF": 3,
    "Util": 1,  # flexible batter slot
    "SP": 2, "RP": 2, "P": 1,  # P is flex pitcher
}


def _position_counts_from_keepers(keepers: list[dict]) -> dict:
    """Count filled positions from keeper list (primary position per keeper)."""
    counts = {}
    for k in keepers:
        positions = k.get("positions") or []
        if positions:
            primary = positions[0] if isinstance(positions[0], str) else "?"
            counts[primary] = counts.get(primary, 0) + 1
    return counts


def _positions_of_need(filled: dict) -> list[str]:
    """Return positions where we're under the required count."""
    need = []
    for pos, required in DEFAULT_ROSTER_REQUIRED.items():
        if pos == "Util":  # Util filled by excess batters; skip for now
            continue
        have = filled.get(pos, 0)
        if have < required:
            need.extend([pos] * (required - have))
    return need


def _build_league_draft_strategy(api: YahooFantasyAPI, league_key: str, team_key: str) -> dict:
    """Build full draft strategy data for draft-strategy endpoint (keeper leagues)."""
    scoring_raw = api.get_league_scoring_settings(league_key) or {}
    batting_cats = scoring_raw.get("batting_categories") or []
    pitching_cats = scoring_raw.get("pitching_categories") or []

    keeper_data = api.get_league_keepers(league_key)
    if keeper_data is None:
        raise HTTPException(status_code=502, detail="Could not fetch keeper data from Yahoo")

    age_lookup = get_player_age_lookup(season=2026)
    rankings = api.get_player_rankings(league_key, count=200, age_lookup=age_lookup)
    player_key_to_rank = {p["player_key"]: p["rank"] for p in rankings}

    all_keepers = keeper_data["all_keepers"]
    kept_keys = {k["player_key"] for k in all_keepers}

    enriched_keepers = []
    for keeper in all_keepers:
        age_entry = (
            age_lookup.get(keeper["name"].lower())
            or age_lookup.get(_ascii_normalize(keeper["name"]))
        )
        age = age_entry["age"] if age_entry else None
        birth_date = age_entry["birth_date"] if age_entry else None
        rank = player_key_to_rank.get(keeper["player_key"], 175)
        tier = _classify_strategy_tier(rank=rank, age=age)
        nudge = _category_role_nudge(keeper.get("positions") or [], batting_cats, pitching_cats)
        sell = tier["sell_high_score"]
        enriched_keepers.append({
            **keeper,
            "rank": rank,
            "age": age,
            "birth_date": birth_date,
            "age_tier": tier["age_tier"],
            "keeper_flag": tier["keeper_flag"],
            "win_now_score": tier["win_now_score"],
            "keeper_score": tier["keeper_score"],
            "sell_high_score": sell,
            "category_nudge": round(nudge, 4),
            "adjusted_sell_high_score": round((sell or 0) * nudge, 2) if sell is not None else None,
        })

    my_keepers = sorted(
        [k for k in enriched_keepers if k.get("fantasy_team_key") == team_key],
        key=lambda x: -(x.get("adjusted_sell_high_score") or x.get("sell_high_score") or 0),
    )

    my_sell_high = [
        k for k in my_keepers
        if k["keeper_flag"] in ("avoid", "low") and (k.get("win_now_score") or 0) > 40
    ]

    other_teams: dict = {}
    for k in enriched_keepers:
        fkey = k.get("fantasy_team_key", "")
        fname = k.get("fantasy_team", "Unknown")
        if fkey == team_key or not fkey:
            continue
        if fkey not in other_teams:
            other_teams[fkey] = {
                "team": fname,
                "team_key": fkey,
                "aging_keepers": [],
                "young_keepers": [],
            }
        if k["keeper_flag"] in ("avoid", "low"):
            other_teams[fkey]["aging_keepers"].append(k)
        elif k["keeper_flag"] in ("strong", "good", "moderate"):
            other_teams[fkey]["young_keepers"].append(k)

    trade_targets = []
    for entry in other_teams.values():
        if not entry["young_keepers"]:
            continue
        win_now_signal = len(entry["aging_keepers"])
        entry["young_keepers"].sort(key=lambda x: -(x.get("keeper_score") or 0))
        entry["aging_keepers"].sort(key=lambda x: -(x.get("age") or 0))
        entry["win_now_signal"] = win_now_signal
        trade_targets.append(entry)

    trade_targets.sort(key=lambda x: -x["win_now_signal"])

    position_counts: dict = {}
    for keeper in all_keepers:
        primary = keeper["positions"][0] if keeper["positions"] else "?"
        position_counts[primary] = position_counts.get(primary, 0) + 1

    available = [p for p in rankings if p["player_key"] not in kept_keys]

    win_now_raw = sorted(
        [p for p in available if p.get("strategy_tier", {}).get("win_now_score") is not None],
        key=lambda x: -(x["strategy_tier"]["win_now_score"] or 0)
    )[:30]
    win_now = [
        _enrich_player_with_category_nudge(p, batting_cats, pitching_cats) for p in win_now_raw
    ]
    win_now.sort(key=lambda x: -(x.get("adjusted_win_now_score") or x["strategy_tier"]["win_now_score"] or 0))

    future_raw = sorted(
        [p for p in available if p.get("strategy_tier", {}).get("keeper_flag") not in ("avoid", None)],
        key=lambda x: -(x["strategy_tier"].get("keeper_score") or 0)
    )[:30]
    future_keepers = [
        _enrich_player_with_category_nudge(p, batting_cats, pitching_cats) for p in future_raw
    ]
    future_keepers.sort(
        key=lambda x: -(x.get("adjusted_keeper_score") or x["strategy_tier"].get("keeper_score") or 0)
    )

    # Position need: from my keepers
    my_position_counts = _position_counts_from_keepers(my_keepers)
    positions_of_need = _positions_of_need(my_position_counts)

    return {
        "league_key": league_key,
        "scoring_categories": {
            "batting": batting_cats,
            "pitching": pitching_cats,
            "roster_positions": scoring_raw.get("roster_positions") or [],
        },
        "total_keepers": len(all_keepers),
        "my_keepers": my_keepers,
        "my_position_counts": my_position_counts,
        "positions_of_need": positions_of_need,
        "my_sell_high_keepers": my_sell_high,
        "trade_targets": trade_targets,
        "position_scarcity": dict(sorted(position_counts.items(), key=lambda x: -x[1])),
        "win_now_targets": win_now,
        "future_keeper_targets": future_keepers,
        "all_keepers": enriched_keepers,
    }


def _parse_roster_player_full(player_data: dict) -> dict | None:
    """Extract full player info from Yahoo roster entry for trade analysis."""
    player_key = None
    name = "Unknown"
    positions: list[str] = []
    team = "Unknown"

    for element in player_data.get("player", []):
        if isinstance(element, list):
            for item in element:
                if isinstance(item, dict):
                    if "player_key" in item:
                        player_key = item["player_key"]
                    elif "name" in item:
                        name = item["name"].get("full", "Unknown")
                    elif "editorial_team_abbr" in item:
                        team = item["editorial_team_abbr"]
                    elif "eligible_positions" in item:
                        pos_list = item["eligible_positions"]
                        if isinstance(pos_list, list):
                            positions = [p.get("position", p) if isinstance(p, dict) else str(p) for p in pos_list]
                    elif "display_position" in item and not positions:
                        positions = [item["display_position"]]

    if not player_key:
        return None
    if not positions:
        positions = ["?"]
    return {
        "player_key": player_key,
        "name": name,
        "positions": positions,
        "team": team,
    }


@app.get("/api/robot-league/keepers")
def robot_league_keepers(league_key: str | None = Query(None, description="Yahoo league key (Robot or LHF)")):
    """
    Get all keeper designations for the league, grouped by team.
    Also returns a status summary of which teams have set their keepers.
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    lk = _parse_league_key(league_key)
    data = api.get_league_keepers(lk)
    if data is None:
        raise HTTPException(status_code=502, detail="Could not fetch keeper data from Yahoo")

    # Build per-team status summary
    team_status = []
    for team_name, keepers in data["keepers_by_team"].items():
        team_status.append({
            "team": team_name,
            "keepers_set": len(keepers) > 0,
            "keeper_count": len(keepers),
            "keepers": keepers,
        })
    team_status.sort(key=lambda x: x["team"])

    return {
        "league_key": lk,
        "teams_total": data["teams_total"],
        "teams_with_keepers": data["teams_with_keepers"],
        "teams_without_keepers": data["teams_total"] - data["teams_with_keepers"],
        "all_keepers": data["all_keepers"],
        "team_status": team_status,
    }


@app.get("/api/robot-league/draft-strategy")
def robot_league_draft_strategy(
    team_key: str = Query(..., description="Your team key"),
    league_key: str | None = Query(None, description="Yahoo league key (Robot or LHF)"),
):
    """
    Build a dual-strategy draft guide for a keeper league:

    WIN NOW  — Target high-ranked players regardless of age. Best for winning this season.
               Older players (33+) flagged with a decline warning.

    FUTURE / KEEPER — Target younger players with high keeper potential for next season.
                      Sorted by keeper score (age × relevance). Older players scored low.

    Also audits all existing keeper picks league-wide and flags any that are poor
    keeper investments for next season (e.g. Robbie Ray, aging veterans).
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    lk = _parse_league_key(league_key)
    _ensure_team_in_league(api, lk, team_key)
    return _build_league_draft_strategy(api, lk, team_key)


@app.get("/api/robot-league/draft-results")
def robot_league_draft_results(league_key: str | None = Query(None, description="Yahoo league key (Robot or LHF)")):
    """Get last draft's pick-by-pick results for the league."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    lk = _parse_league_key(league_key)
    picks = api.get_draft_results(lk)
    return {"league_key": lk, "picks": picks}


def _build_trade_analysis(api: YahooFantasyAPI, league_key: str, team_key: str) -> dict:
    """Build sell-high / buy-low trade analysis for a configured league."""
    scoring_raw = api.get_league_scoring_settings(league_key) or {}
    batting_cats = scoring_raw.get("batting_categories") or []
    pitching_cats = scoring_raw.get("pitching_categories") or []

    teams_raw = api.get_league_teams(league_key)
    if not teams_raw:
        raise HTTPException(status_code=502, detail="Could not fetch league teams")

    teams = _extract_teams({"teams": teams_raw})
    age_lookup = get_player_age_lookup(season=2026)
    rankings = api.get_player_rankings(league_key, count=300, age_lookup=age_lookup)

    player_key_to_rank = {p["player_key"]: p["rank"] for p in rankings}
    player_key_to_info = {p["player_key"]: p for p in rankings}

    def enrich_roster(raw_roster: dict) -> list[dict]:
        out = []
        for k, v in raw_roster.items():
            if not k.isdigit():
                continue
            p = _parse_roster_player_full(v)
            if not p:
                continue
            rank = player_key_to_rank.get(p["player_key"]) or player_key_to_info.get(p["player_key"], {}).get("rank", 175)
            age_entry = (
                age_lookup.get(p["name"].lower())
                or age_lookup.get(_ascii_normalize(p["name"]))
            )
            age = age_entry["age"] if age_entry else None
            tier = _classify_strategy_tier(rank, age)
            nudge = _category_role_nudge(p["positions"], batting_cats, pitching_cats)
            sh = tier["sell_high_score"]
            ks = tier["keeper_score"]
            out.append({
                **p,
                "rank": rank,
                "age": age,
                "strategy_tier": tier,
                "sell_high_score": sh,
                "keeper_score": ks,
                "keeper_flag": tier["keeper_flag"],
                "category_nudge": round(nudge, 4),
                "adjusted_sell_high_score": round((sh or 0) * nudge, 2) if sh is not None else None,
                "adjusted_keeper_score": round((ks or 0) * nudge, 2) if ks is not None else None,
            })
        return out

    all_rosters: dict[str, list[dict]] = {}
    for t in teams:
        roster_raw = api.get_team_roster(t["team_key"])
        all_rosters[t["team_key"]] = enrich_roster(roster_raw) if roster_raw else []

    my_roster = all_rosters.get(team_key, [])
    my_sell_high = sorted(
        [p for p in my_roster if (p.get("sell_high_score") or 0) >= 30 and p.get("keeper_flag") in ("avoid", "low")],
        key=lambda x: -(x.get("adjusted_sell_high_score") or x.get("sell_high_score") or 0),
    )[:10]

    my_buy_low_targets: list[dict] = []
    trade_partners: list[dict] = []

    for t in teams:
        if t["team_key"] == team_key:
            continue
        roster = all_rosters.get(t["team_key"], [])
        aging = [p for p in roster if p.get("keeper_flag") in ("avoid", "low") and (p.get("sell_high_score") or 0) >= 25]
        young = [p for p in roster if p.get("keeper_flag") in ("strong", "good", "moderate") and (p.get("keeper_score") or 0) >= 40]
        young_sorted = sorted(
            young,
            key=lambda x: -(x.get("adjusted_keeper_score") or x.get("keeper_score") or 0),
        )

        for y in young_sorted[:5]:
            my_buy_low_targets.append({**y, "on_team": t["name"], "team_key": t["team_key"]})

        if young_sorted and aging:
            suggested = []
            for i, offer in enumerate(my_sell_high[:3]):
                if i < len(young_sorted):
                    recv = young_sorted[i]
                    suggested.append({
                        "offer": offer,
                        "receive": recv,
                        "rationale": (
                            f"Offer {offer['name']} (sell-high, age {offer.get('age', '?')}) for {recv['name']} "
                            f"(young keeper, age {recv.get('age', '?')}). They have aging roster — likely win-now motivated."
                        ),
                    })
            if suggested:
                trade_partners.append({
                    "team": t["name"],
                    "team_key": t["team_key"],
                    "aging_count": len(aging),
                    "young_targets": young_sorted[:5],
                    "suggested_trades": suggested,
                })

    trade_partners.sort(key=lambda x: -x["aging_count"])
    my_buy_low_targets.sort(
        key=lambda x: -(x.get("adjusted_keeper_score") or x.get("keeper_score") or 0)
    )

    return {
        "league_key": league_key,
        "scoring_categories": {
            "batting": batting_cats,
            "pitching": pitching_cats,
            "roster_positions": scoring_raw.get("roster_positions") or [],
        },
        "my_roster_count": len(my_roster),
        "my_sell_high": my_sell_high,
        "my_buy_low_targets": my_buy_low_targets[:15],
        "trade_partners": trade_partners,
    }


@app.get("/api/robot-league/trade-analysis")
def robot_league_trade_analysis(
    team_key: str = Query(..., description="Your team key"),
    league_key: str | None = Query(None, description="Yahoo league key (Robot or LHF)"),
):
    """
    Post-draft trade analysis: sell-high candidates on your roster and buy-low targets on other teams.
    Suggests optimal trade scenarios based on age/keeper strategy — teams with aging rosters
    are likely to accept your sell-high veterans for their young keeper assets.
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    lk = _parse_league_key(league_key)
    _ensure_team_in_league(api, lk, team_key)
    return _build_trade_analysis(api, lk, team_key)


@app.get("/api/lhf/league-config")
def lhf_league_config():
    """
    LHF league structure from LHF_data.csv: roster slots and category scoring lists.
    """
    cfg = get_lhf_config()
    return {
        "league_key": LHF_LEAGUE_KEY or None,
        "league_key_configured": bool(LHF_LEAGUE_KEY.strip()),
        "source_file": cfg.source_path,
        "roster_slots": cfg.roster_slots,
        "pitching_categories": cfg.pitching_categories,
        "batting_categories": cfg.batting_categories,
        "totals": {
            "active_hitter_slots": cfg.active_hitter_slots,
            "active_pitcher_slots": cfg.active_pitcher_slots,
            "total_roster_slots": cfg.total_roster_slots,
        },
    }


@app.get("/api/lhf/roster-as-picks")
def lhf_roster_as_picks(team_key: str = Query(..., description="Your Yahoo team key")):
    """
    Load your current Yahoo roster as LHF pick objects (player_key, name, positions).
    Use this instead of typing JSON — refresh during the draft after each pick.
    """
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
    picks = _yahoo_roster_to_lhf_picks(api, team_key)
    return {"team_key": team_key, "picks": picks, "count": len(picks)}


@app.post("/api/lhf/ai-context")
def lhf_ai_context(req: LHFAIContextRequest):
    """
    Build a markdown blob for ChatGPT / Cursor / any LLM: league rules, your roster,
    positional needs, and ranked recommendations. Pass `team_key` to load roster from Yahoo,
    or `picks` if you edited them locally.
    """
    league_key = _require_lhf_league_key()
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    picks_raw: list[dict] = []
    if req.team_key and req.team_key.strip():
        picks_raw = _yahoo_roster_to_lhf_picks(api, req.team_key.strip())
    elif req.picks:
        picks_raw = list(req.picks)
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide `team_key` (load Yahoo roster) or a non-empty `picks` array.",
        )

    cfg = get_lhf_config()
    count = max(25, min(300, int(req.count)))
    rec = _build_lhf_recommendations(
        api, league_key, picks_raw, req.exclude_yahoo_drafted, count
    )
    markdown = _build_lhf_ai_markdown(cfg, picks_raw, rec)
    return {
        "league_key": league_key,
        "team_key": req.team_key,
        "pick_count": len(picks_raw),
        "markdown": markdown,
        "picks": picks_raw,
        "remaining_slots": rec["remaining_slots"],
        "positions_of_need": rec["positions_of_need"],
        "recommendations": rec["recommendations"],
    }


@app.post("/api/lhf/draft-state")
def lhf_draft_state(req: LHFPicksRequest):
    """
    Compute remaining lineup slots and positional need from your picks so far.
    Each pick should include player_key and positions (Yahoo eligible positions).
    """
    cfg = get_lhf_config()
    picks = _parse_lhf_picks(req.picks)
    analysis = analyze_draft_picks(picks, cfg)
    return {
        "league_key": LHF_LEAGUE_KEY or None,
        "roster_slots": cfg.roster_slots,
        "pitching_categories": cfg.pitching_categories,
        "batting_categories": cfg.batting_categories,
        **analysis,
    }


@app.get("/api/lhf/recommendations")
def lhf_recommendations(
    picks_json: str | None = Query(
        None,
        description='JSON array of picks: [{"player_key":"...","name":"...","positions":["SS"]}]',
    ),
    exclude_yahoo_drafted: bool = Query(
        False,
        description="If true, exclude all player_keys from Yahoo draft results for this league.",
    ),
    count: int = Query(50, ge=1, le=300),
):
    """
    Next-pick suggestions: Yahoo OR rank + positional-need boost from LHF_data.csv slots.
    Pass picks_json (URL-encoded JSON) so positional need is computed; omit for empty-draft BPA.
    """
    league_key = _require_lhf_league_key()
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")

    picks_raw: list[dict] = []
    if picks_json:
        try:
            loaded = json.loads(picks_json)
            if not isinstance(loaded, list):
                raise ValueError("picks_json must be a JSON array")
            picks_raw = loaded
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid picks_json: {e}")

    return _build_lhf_recommendations(api, league_key, picks_raw, exclude_yahoo_drafted, count)


@app.post("/api/transactions/add-drop")
def add_drop(req: AddDropRequest):
    """Execute add/drop transaction."""
    api = _get_api()
    if not api._ensure_valid_token():
        raise HTTPException(status_code=401, detail="Not authenticated")
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


# Serve frontend
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
