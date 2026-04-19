# Fantasy Baseball AI Co-Manager

A Python/FastAPI web app that connects to the Yahoo Fantasy Sports API to help manage your fantasy baseball team. Includes a dark-themed browser UI for in-season tools (lineup optimization, call-ups, trade analysis). Draft- and keeper-planning helpers are still available **via the HTTP API** if you want to script against them.

---

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Register a Yahoo Developer App

1. Go to [developer.yahoo.com/apps/create](https://developer.yahoo.com/apps/create/)
2. Create a **Web Application** with **Fantasy Sports Read/Write** permissions
3. Set the redirect URI to `http://localhost:8000/auth/callback`
4. Copy your **Client ID** and **Client Secret**

### 3. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```
YAHOO_CLIENT_ID=your_client_id_here
YAHOO_CLIENT_SECRET=your_client_secret_here
YAHOO_REDIRECT_URI=http://localhost:8000/auth/callback
ROBOT_LEAGUE_KEY=469.l.12479
```

> `ROBOT_LEAGUE_KEY` is the Yahoo league key for the keeper-focused endpoints (trade analysis, keeper status, draft strategy API, etc.). Find yours by hitting `GET /api/leagues` after authenticating and locating the correct league.

Optional: `LHF_LEAGUE_KEY` pairs with `ROBOT_LEAGUE_KEY` in `GET /api/leagues/configured` and powers the **LHF** draft-helper endpoints below (API-only).

### 4. Run the App

```bash
./dev.sh
```

Or directly:

```bash
cd fantasy_baseball
uvicorn api.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) and click **Connect Yahoo** to authenticate.

> **Re-authentication (OAuth):** If your tokens expire completely and the refresh fails, use `./start.sh` instead — it spins up ngrok and walks you through re-auth.

---

## Dashboard (browser UI)

What you get after signing in:

### Authentication
- OAuth 2.0 flow with Yahoo — handled entirely in the browser
- Automatic token refresh; tokens persisted to **`yahoo_tokens.json`** in the **process working directory** (with `./dev.sh` or `./start.sh`, that is the `fantasy_baseball/` project root—not under `api/`)

### League & team
- Select a configured league and your team (same allowlist as the API: Robot League + optional LHF)

### Trade Analysis
- Post-draft / in-season: sell-high candidates, buy-low targets on other teams, suggested trade scenarios
- Uses Yahoo scoring categories for the selected league

### Lineup Optimizer
- Detects starting pitchers who are scheduled to pitch today but are sitting on the bench
- Preview suggested changes or apply them directly via the UI

### Call-Up Recommendations
- Surfaces free agents recently promoted to MLB (call-ups), cross-referenced against your league's available players

### NA slot — league intel
- **League NA stashes:** Every player in a Yahoo **NA** (Not Active) roster slot across all teams (who is stashing whom).
- **NA adds:** Free agents sorted by Yahoo OR, filtered to players Yahoo shows as **`display_position` = NA** (true minors / not-active stash), not active MLB listed as OF/SP/etc. (Yahoo’s `position=NA` API filter alone can include active players who still carry NA eligibility). Each row includes **`yahoo_rank`**, **`display_position`**, and **`callup_urgency`**. Response includes **`callup_urgency_count`**.

Roster inspection and add/drop transactions are available through the **API** (`GET /api/teams/{team_key}/roster`, `POST /api/transactions/add-drop`), not from the dashboard.

---

## Keeper / trade logic (API)

Endpoints support **up to two Yahoo leagues** from env: `ROBOT_LEAGUE_KEY` (default `469.l.12479`) and optionally `LHF_LEAGUE_KEY` (default `469.l.15622`). Pass **`league_key`** on each request; the server checks it against that allowlist and verifies **`team_key`** belongs to that league. Scoring categories come from Yahoo **`/league/{key}/settings`** (batting/pitching stats) and are returned as `scoring_categories` where applicable, with small **category nudges** on rankings for role vs league (e.g. QS leagues bump SP).

### Configured leagues (`GET /api/leagues/configured`)

Returns the configured `league_key` values and labels for the UI.

### Trade Analysis (`GET /api/robot-league/trade-analysis?team_key=...&league_key=...`)

**Used by the dashboard.** Evaluates your roster and every other team in the league. Returns:

- **Your Sell-High Candidates** — aging players with solid win-now value but low keeper value; trade early when hot
- **Buy-Low Targets** — young keeper-eligible players on other teams to target in trades
- **Suggested Trade Scenarios** — specific offer/receive pairings for teams with aging rosters (win-now motivated)

### Keeper Status (`GET /api/robot-league/keepers`) — API only

- Shows all designated keepers across every team in the league
- Reports which teams have set their keepers and which haven't
- Each keeper is enriched with MLB age data

### Draft Strategy (`GET /api/robot-league/draft-strategy?team_key=...&league_key=...`) — API only

Returns keeper-centric draft planning data: your keepers, sell-high keepers, trade targets, position scarcity, win-now and future keeper targets, plus Yahoo scoring categories. Use this from scripts or tools during keeper-draft prep; it is not exposed in the web UI.

### Draft Results (`GET /api/robot-league/draft-results?league_key=...`) — API only

Returns the pick-by-pick results from the league's most recent draft.

### LHF redraft helpers — API only

Low Hanging Fruit (non-keeper) league helpers: league config from `LHF_data.csv`, roster-as-picks sync, draft-state slot analysis, ranked recommendations, and AI context export. See `GET/POST` routes under `/api/lhf/*` in the table below.

---

## Scoring System

All player scoring uses age data fetched in a single bulk call from the MLB Stats API (`statsapi.mlb.com`) — no additional API key required.

| Score | What it measures |
|---|---|
| **Win-Now Score** | Yahoo rank adjusted for age-related decline risk (33+ players penalized slightly) |
| **Keeper Score** | Age × relevance — peaks for young high-ranked players, near zero for 33+ |
| **Sell-High Score** | `win_now_score × (1 − keeper_score/100)` — highest for productive players with no long-term keeper value |

**Age Tiers:**

| Tier | Age | Meaning |
|---|---|
| Prospect | ≤23 | Ceiling play, may underperform short-term |
| Ascending | 24–26 | Best keeper targets — good now AND young |
| Prime | 27–29 | Win now + still keepable |
| Veteran | 30–32 | Strong win-now, keeper value fading |
| Declining | 33+ | Win now only — sell-high candidates |

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/auth/status` | Check if Yahoo OAuth token is valid |
| `GET` | `/api/auth/url` | Get Yahoo OAuth redirect URL |
| `POST` | `/api/auth/exchange` | Exchange auth code for token (SPA / alternate client flow) |
| `GET` | `/auth/callback` | OAuth callback handler |
| `GET` | `/api/leagues` | List user's fantasy leagues |
| `GET` | `/api/leagues/debug` | Raw Yahoo leagues payload (diagnostics) |
| `GET` | `/api/leagues/configured` | Robot + LHF league keys and labels (allowlist) |
| `GET` | `/api/leagues/{league_key}/teams` | List teams in a league |
| `GET` | `/api/leagues/{league_key}/na-stashes` | All players in NA slots, every team in the league |
| `GET` | `/api/leagues/{league_key}/na-adds` | NA-eligible FA sorted by OR; `?days=7&count=50`; call-up overlay |
| `GET` | `/api/teams/{team_key}/roster` | Get team roster (optional `?date=`) |
| `GET` | `/api/recommendations/callups` | Recently called-up players on waivers (`?league_key=` required; optional `days=`) |
| `GET` | `/api/optimize-lineup` | Preview or apply SP lineup optimization |
| `POST` | `/api/transactions/add-drop` | Execute an add/drop transaction |
| `GET` | `/api/robot-league/trade-analysis` | Trade analysis + scoring categories + category nudges |
| `GET` | `/api/robot-league/keepers` | Keeper status per team (`?league_key=` required) |
| `GET` | `/api/robot-league/draft-strategy` | Keeper draft strategy + Yahoo scoring categories |
| `GET` | `/api/robot-league/draft-results` | Last draft's pick-by-pick results |
| `GET` | `/api/lhf/league-config` | LHF league rules from `LHF_data.csv` |
| `GET` | `/api/lhf/roster-as-picks` | Yahoo roster as JSON picks (`?team_key=`) |
| `POST` | `/api/lhf/draft-state` | Slot analysis for a pick list |
| `GET` | `/api/lhf/recommendations` | Ranked next picks (`picks_json`, etc.) |
| `POST` | `/api/lhf/ai-context` | Markdown context for external AI tools |

---

## File Structure

```
fantasy_baseball/
├── api/
│   ├── main.py               # FastAPI app — all endpoints
│   └── __init__.py
├── frontend/
│   └── index.html            # Single-page browser UI
├── yahoo_api.py              # Yahoo Fantasy Sports API client + scoring logic
├── mlb_client.py             # MLB Stats API client (call-ups, player ages)
├── lineup_optimizer.py       # SP lineup optimization logic
├── recommendations.py        # Call-ups, league NA stashes, NA adds
├── lhf_draft.py              # LHF draft slot analysis + recommendations
├── lhf_league_config.py      # Loads LHF rules from LHF_data.csv
├── LHF_data.csv              # LHF roster slots / scoring categories
├── yahoo_tokens.json         # OAuth tokens at runtime if cwd is project root (gitignored)
├── requirements.txt
├── .env                      # Your credentials (gitignored)
├── .env.example              # Safe template to commit
├── dev.sh                    # Start uvicorn (daily use)
└── start.sh                  # Start uvicorn + ngrok (for re-auth)
```

---

## Troubleshooting

**"Invalid redirect URI"**
The URI in your Yahoo Developer app must exactly match `YAHOO_REDIRECT_URI` in `.env`. For local dev: `http://localhost:8000/auth/callback`.

**"Not authenticated" errors**
Tokens may have expired. Visit [http://localhost:8000](http://localhost:8000) and reconnect Yahoo, or run `./start.sh` if the refresh token is also stale.

**Rotating credentials**
Go to [developer.yahoo.com/apps](https://developer.yahoo.com/apps/), regenerate your client secret, update `.env`, restart the server, and re-authenticate once in the browser.

**Draft strategy API returns empty results**
If you call `GET /api/robot-league/draft-strategy` directly, ensure `ROBOT_LEAGUE_KEY` in `.env` matches an active league you're a member of. Use `GET /api/leagues` to find the correct key — higher game key prefix = more recent season.

---

## Data Sources

- [Yahoo Fantasy Sports API](https://developer.yahoo.com/fantasysports/guide/) — league, roster, player, and transaction data
- [MLB Stats API](https://statsapi.mlb.com) — player ages, transactions, call-ups (free, no key required)
