# Fantasy Baseball AI Co-Manager

A Python/FastAPI app for managing Yahoo fantasy baseball rosters with a focused in-season workflow: authenticate with Yahoo, choose one of up to two configured leagues, inspect teams, and optimize lineups.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Register a Yahoo developer app

1. Go to [developer.yahoo.com/apps/create](https://developer.yahoo.com/apps/create/)
2. Create a **Web Application** with **Fantasy Sports Read/Write** permissions
3. Set the redirect URI to `http://localhost:8000/auth/callback`
4. Copy your **Client ID** and **Client Secret**

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```text
YAHOO_CLIENT_ID=your_client_id_here
YAHOO_CLIENT_SECRET=your_client_secret_here
YAHOO_REDIRECT_URI=http://localhost:8000/auth/callback
ROBOT_LEAGUE_KEY=469.l.12479
LHF_LEAGUE_KEY=469.l.15622
LINEUP_TEAM_KEY=469.l.12479.t.7
LINEUP_LEAGUE_KEY=469.l.12479
LINEUP_SCHEDULE_TIMES=11:00,17:00
LINEUP_SCHEDULE_TZ=US/Eastern
LINEUP_AUTO_APPLY=false
LINEUP_WEIGHT_7D=0.6
LINEUP_WEIGHT_30D=0.4
```

`ROBOT_LEAGUE_KEY` is the primary configured league. `LHF_LEAGUE_KEY` is optional and keeps a second league available in the UI and API allowlist.

`LINEUP_*` settings control the daily lineup optimizer for pitchers and batters. If `LINEUP_TEAM_KEY` is blank, no scheduled job runs. `LINEUP_SCHEDULE_TIMES` accepts a comma-separated list of 24-hour `HH:MM` values in `LINEUP_SCHEDULE_TZ`, so you can run the optimizer more than once per day.

### 4. Run the app

```bash
./dev.sh
```

Or directly:

```bash
cd fantasy_baseball
uvicorn api.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) and click **Connect Yahoo** to authenticate.

> If your tokens expire completely and refresh no longer works, use `./start.sh` to walk through re-authentication with ngrok.

---

## What remains

### Browser UI

- Yahoo OAuth in the browser
- Configured league selector with support for up to two leagues
- Team selector for the chosen league
- Combined pitcher + batter lineup optimization preview and apply flow

### API-only tools

- Roster inspection for a selected date
- Add/drop transactions
- Combined lineup optimization for a selected date
- Daily scheduled pitcher + batter optimization through APScheduler

Tokens are persisted to `yahoo_tokens.json` in the process working directory. With `./dev.sh` or `./start.sh`, that is the `fantasy_baseball/` project root.

---

## Two-league support

The app still supports up to two configured Yahoo leagues:

- `ROBOT_LEAGUE_KEY`
- `LHF_LEAGUE_KEY` (optional)

`GET /api/leagues/configured` returns those configured league keys for the frontend selector. The add/drop API also validates that `league_key` belongs to that configured allowlist.

This keeps the core workflow multi-league even after removing draft, trade, and recommendation features.

---

## Batter optimizer scoring

The batter optimizer uses real recent Yahoo stat windows rather than Yahoo's opaque overall rank ordering.

### Data inputs

- Yahoo `lastweek` stats
- Yahoo `lastmonth` stats
- MLB daily schedule data
- League batting categories from Yahoo league settings

### Current Robot League categories

- `R`
- `HR`
- `RBI`
- `SB`
- `AVG`

`H/AB` may still appear in Yahoo responses for display, but the optimizer ignores it because `AVG` already captures the category signal.

### Score calculation

For each hitter:

1. Pull `lastweek` and `lastmonth` category stats from Yahoo
2. Normalize each category across roster hitters with z-scores
3. Sum category z-scores into `score_7d` and `score_30d`
4. Compute `composite_score = LINEUP_WEIGHT_7D * score_7d + LINEUP_WEIGHT_30D * score_30d`

Higher `composite_score` means stronger recent performance for the league format.

### Lineup behavior

- Pitchers are untouched by the batter optimizer
- Hitters without a scheduled game for the target date are benched
- Remaining active slots are filled with scarcity-first assignment so multi-position hitters do not block thinner positions
- The endpoint accepts an explicit `date`, so future editable dates can be optimized too

---

## API endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/auth/status` | Check if the Yahoo OAuth token is valid |
| `GET` | `/api/auth/url` | Get Yahoo OAuth redirect URL |
| `POST` | `/api/auth/exchange` | Exchange auth code for token |
| `GET` | `/auth/callback` | OAuth callback handler |
| `GET` | `/api/leagues` | List the user's fantasy leagues |
| `GET` | `/api/leagues/debug` | Return raw Yahoo leagues payload for diagnostics |
| `GET` | `/api/leagues/configured` | Return the configured one- or two-league allowlist |
| `GET` | `/api/leagues/{league_key}/teams` | List teams in a league |
| `GET` | `/api/teams/{team_key}/roster` | Get team roster for today or a supplied `date` |
| `GET` | `/api/optimize-lineup` | Preview or apply pitcher-only SP lineup optimization |
| `POST` | `/api/optimize-batting-lineup` | Preview or apply combined pitcher + batter lineup optimization |
| `POST` | `/api/transactions/add-drop` | Execute an add/drop transaction |

---

## File structure

```text
fantasy_baseball/
├── api/
│   ├── main.py               # FastAPI app and remaining API routes
│   └── __init__.py
├── frontend/
│   └── index.html            # Single-page browser UI
├── yahoo_api.py              # Yahoo Fantasy Sports API client
├── mlb_client.py             # MLB schedule and player data helpers
├── lineup_optimizer.py       # SP lineup optimization logic
├── batter_optimizer.py       # Daily batter lineup optimization logic
├── yahoo_tokens.json         # OAuth tokens at runtime if cwd is project root (gitignored)
├── fantasy-baseball.service  # Example systemd unit for Raspberry Pi deployment
├── requirements.txt
├── .env                      # Your credentials (gitignored)
├── .env.example              # Safe template to commit
├── dev.sh                    # Start uvicorn for local use
└── start.sh                  # Start uvicorn + ngrok for re-auth
```

---

## Troubleshooting

**"Invalid redirect URI"**  
The URI in your Yahoo developer app must exactly match `YAHOO_REDIRECT_URI` in `.env`. For local dev, use `http://localhost:8000/auth/callback`.

**"Not authenticated" errors**  
Tokens may have expired. Visit [http://localhost:8000](http://localhost:8000) and reconnect Yahoo, or run `./start.sh` if the refresh token is also stale.

**Rotating credentials**  
Go to [developer.yahoo.com/apps](https://developer.yahoo.com/apps/), regenerate your client secret, update `.env`, restart the server, and re-authenticate once.

**Configured second league does not appear**  
Set `LHF_LEAGUE_KEY` in `.env`, restart the server, and confirm the account has access to that Yahoo league.

**Daily lineup automation doesn't run**  
Confirm `LINEUP_TEAM_KEY` is set, the server is still running, and the scheduler timezone plus `LINEUP_SCHEDULE_TIMES` values are correct. Check `uvicorn` or `systemd` logs for failures.

**The API says lineup changes were applied, but a follow-up roster read still shows the old slots**  
Yahoo roster updates can be slightly delayed on readback. Treat roster reads as eventually consistent and retry after a short delay if you need confirmation.

---

## Raspberry Pi deployment

Use `fantasy-baseball.service` as a starting point for a long-running FastAPI service with the built-in daily combined lineup scheduler.

### 1. Copy the project

```bash
scp -r fantasy_baseball pi@your-pi:/home/pi/
```

### 2. Install Python dependencies

```bash
cd /home/pi/fantasy_baseball
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure `.env`

Set your Yahoo credentials plus lineup automation values:

```text
LINEUP_TEAM_KEY=469.l.12479.t.7
LINEUP_LEAGUE_KEY=469.l.12479
LINEUP_SCHEDULE_TIMES=11:00,17:00
LINEUP_SCHEDULE_TZ=US/Eastern
LINEUP_AUTO_APPLY=true
LINEUP_WEIGHT_7D=0.6
LINEUP_WEIGHT_30D=0.4
```

The scheduler also supports the older single-run `LINEUP_SCHEDULE_HOUR` and `LINEUP_SCHEDULE_MINUTE` vars for backward compatibility, but `LINEUP_SCHEDULE_TIMES` is the preferred format for the Pi deployment.

### 4. Install the systemd service

```bash
sudo cp fantasy-baseball.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable fantasy-baseball
sudo systemctl start fantasy-baseball
sudo systemctl status fantasy-baseball
```

The service reads `/home/pi/fantasy_baseball/.env` automatically through `EnvironmentFile`, so updating schedule or credential settings only requires editing `.env` and restarting the service.

### 5. Trigger on demand if needed

```bash
curl -X POST "http://your-pi:8000/api/optimize-batting-lineup?team_key=469.l.12479.t.7&dry_run=true"
```

Keep `dry_run=true` until you've reviewed the combined pitcher and batter results and are comfortable switching the scheduled job to `LINEUP_AUTO_APPLY=true`.

### 6. Use the deploy helper

After the first manual setup, you can redeploy code changes from your Mac with:

```bash
PI_HOST=raspberrypi.local ./deploy.sh
```

You can also pass the host as the first argument:

```bash
./deploy.sh raspberrypi.local
```

The script syncs the project (excluding `.env`, `.venv`, and `yahoo_tokens.json`), installs Python dependencies on the Pi, and restarts the `fantasy-baseball` systemd service.

---

## Next steps

Use this as a handoff block for a future chat.

### Current state

- Yahoo auth, roster reads, and roster writes are working
- `POST /api/optimize-batting-lineup` exists
- The app is intentionally trimmed to the core in-season workflow
- Up to two configured leagues are still supported through `ROBOT_LEAGUE_KEY` and optional `LHF_LEAGUE_KEY`
- The batter optimizer uses Yahoo `lastweek` and `lastmonth` hitter stats plus MLB schedule data
- The current scoring model is a category-based z-score blend across `R`, `HR`, `RBI`, `SB`, and `AVG`
- Future-date lineup writes work, but Yahoo roster reads can lag briefly after a successful write

### Recommended backlog

1. Add post-apply verification with retry/polling so successful Yahoo writes can be confirmed without relying on a manual app refresh.
2. Add support for more optimizer configuration in `.env`, including:
   - category include/exclude list
   - per-category weights
   - minimum playing-time threshold
   - optional star-player protection rules
3. Add a review endpoint that returns a compact hitter table for a target date:
   - current slot
   - proposed slot
   - opponent
   - score_7d
   - score_30d
   - composite_score
4. Add a multi-day preview/apply endpoint, for example `start_date` and `end_date`.
5. Improve schedule and lineup confidence by distinguishing:
   - team has game
   - hitter is in the announced starting lineup
   - hitter is only projected to be available
6. Add tests around:
   - category score calculation
   - scarcity-first slot assignment
   - date-specific roster updates
   - benching off-day hitters

### Copy/paste prompt for a new chat

```text
Continue work on the `fantasy_baseball` project.

Current status:
- Yahoo auth and roster editing are working.
- `POST /api/optimize-batting-lineup` exists.
- Up to two configured leagues are supported with `ROBOT_LEAGUE_KEY` and optional `LHF_LEAGUE_KEY`.
- The batter optimizer uses real Yahoo `lastweek` / `lastmonth` hitter stats plus MLB schedule data.
- It scores hitters with a category-based z-score blend over `R`, `HR`, `RBI`, `SB`, and `AVG`.
- Future-date writes succeed, but Yahoo roster reads may be eventually consistent right after a write.

Please read `README.md`, `batter_optimizer.py`, `yahoo_api.py`, `mlb_client.py`, and `api/main.py` first.

Then implement the next highest-value improvement from the README "Next steps" section. Before coding, briefly explain which item you are taking and why.
```

---

## Data sources

- [Yahoo Fantasy Sports API](https://developer.yahoo.com/fantasysports/guide/) for league, roster, player, and transaction data
- [MLB Stats API](https://statsapi.mlb.com) for schedule and player information
