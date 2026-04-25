# Fantasy Baseball AI Co-Manager

A Python/FastAPI app for managing Yahoo fantasy baseball rosters with a focused in-season workflow: authenticate with Yahoo, choose from locally configured leagues and teams, inspect rosters, and optimize lineups.

The app can also evaluate and update a roster automatically on a schedule. During each scheduled run, it checks the target team's roster for the configured date, identifies starting pitchers from Yahoo's roster-level `is_starting` signal, benches SPs who are not scheduled to start, activates confirmed starters into available `SP` or `P` slots, and then evaluates hitters with a weighted 7-day / 30-day scoring model to fill the best available batting slots. When `LINEUP_AUTO_APPLY=true`, those pitcher and batter moves are written back to Yahoo automatically; when it is `false`, the same evaluation still runs but only logs what would have changed.

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
LOCAL_TEAM_PROFILES=Robot League|469.l.12479|469.l.12479.t.7;Second League|469.l.15622|469.l.15622.t.4
LINEUP_TEAM_KEY=469.l.12479.t.7
LINEUP_LEAGUE_KEY=469.l.12479
LINEUP_SCHEDULE_TIMES=11:00,17:00
LINEUP_SCHEDULE_TZ=US/Eastern
LINEUP_AUTO_APPLY=false
LINEUP_WEIGHT_7D=0.6
LINEUP_WEIGHT_30D=0.4
NOTIFY_EMAIL_ENABLED=false
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password_here
NOTIFY_EMAIL_TO=recipient@example.com
NOTIFY_ON_NO_CHANGES=false
```

`LOCAL_TEAM_PROFILES` is the easiest way to save multiple local Yahoo teams in the browser UI. Each entry uses `Label|league_key|team_key`, separated by semicolons. The older `ROBOT_LEAGUE_KEY` and optional `LHF_LEAGUE_KEY` values still work as a fallback for league-level configuration.

`LINEUP_*` settings control the daily lineup optimizer for pitchers and batters. If `LINEUP_TEAM_KEY` is blank, no scheduled job runs. When the scheduler is enabled, it will run for all saved or auto-discovered local teams when available, and otherwise falls back to the legacy single `LINEUP_TEAM_KEY` target. `LINEUP_SCHEDULE_TIMES` accepts a comma-separated list of 24-hour `HH:MM` values in `LINEUP_SCHEDULE_TZ`, so you can run the optimizer more than once per day.

`NOTIFY_EMAIL_*` / `SMTP_*` settings enable email summaries for scheduled runs. The app sends one email after each scheduled run when at least one team has lineup changes or an error. Set `NOTIFY_ON_NO_CHANGES=true` if you also want an email when no changes are needed. For Gmail, use `smtp.gmail.com`, port `587`, your Gmail address as `SMTP_USER`, and a Google App Password as `SMTP_PASSWORD`.

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
- Configured league selector
- Saved local team profiles for fast switching between teams
- Team selector for the chosen league
- Combined pitcher + batter lineup optimization preview and apply flow
- Batch dry-run preview across all locally configured or auto-discovered teams

### API-only tools

- Roster inspection for a selected date
- Add/drop transactions
- Combined lineup optimization for a selected date
- Daily scheduled pitcher + batter optimization through APScheduler

Tokens are persisted to `yahoo_tokens.json` in the process working directory. With `./dev.sh` or `./start.sh`, that is the `fantasy_baseball/` project root.

---

## Local multi-team support

For local use, the app can keep a saved list of team profiles in `.env`:

```text
LOCAL_TEAM_PROFILES=Robot League|469.l.12479|469.l.12479.t.7;Second League|469.l.15622|469.l.15622.t.4
```

Each profile stores:

- a label for the UI
- the Yahoo `league_key`
- the Yahoo `team_key`

The browser UI uses those saved profiles to preselect the right team when you switch leagues.

If you prefer the old setup, the app still supports league-only configuration through:

- `ROBOT_LEAGUE_KEY`
- `LHF_LEAGUE_KEY` (optional)

`GET /api/leagues/configured` returns the locally configured leagues for the selector. `GET /api/teams/configured` returns saved local team profiles when present, or auto-discovers the current user's owned team in each configured league. The add/drop API continues to validate that `league_key` belongs to the configured local allowlist.

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
| `GET` | `/api/teams/configured` | Return saved or auto-discovered local teams for the current login |
| `GET` | `/api/leagues/{league_key}/teams` | List teams in a league |
| `GET` | `/api/teams/{team_key}/roster` | Get team roster for today or a supplied `date` |
| `GET` | `/api/optimize-lineup` | Preview or apply pitcher-only SP lineup optimization |
| `POST` | `/api/optimize-batting-lineup` | Preview or apply combined pitcher + batter lineup optimization |
| `POST` | `/api/optimize-all-lineups` | Preview or apply combined optimization for all local teams |
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

**Configured second league or team does not appear**  
If you use `LOCAL_TEAM_PROFILES`, verify the format is exactly `Label|league_key|team_key` with entries separated by semicolons, then restart the server. If you use legacy league settings instead, confirm `LHF_LEAGUE_KEY` is set and the account has access to that Yahoo league.

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
NOTIFY_EMAIL_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=your_app_password_here
NOTIFY_EMAIL_TO=recipient@example.com
NOTIFY_ON_NO_CHANGES=false
```

`LINEUP_TEAM_KEY` still acts as the scheduler enable switch. Once it is set, the Pi scheduler will prefer all saved or auto-discovered local teams and run each one on the configured times. If discovery is unavailable, it falls back to the single `LINEUP_TEAM_KEY`.

The scheduler also supports the older single-run `LINEUP_SCHEDULE_HOUR` and `LINEUP_SCHEDULE_MINUTE` vars for backward compatibility, but `LINEUP_SCHEDULE_TIMES` is the preferred format for the Pi deployment.

Email notifications use Python's built-in SMTP support, so there are no extra dependencies. If you use Gmail, enable 2-Step Verification on the Google account, create an App Password, and use that app password for `SMTP_PASSWORD`; your normal Google password will not work.

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

---

## Data sources

- [Yahoo Fantasy Sports API](https://developer.yahoo.com/fantasysports/guide/) for league, roster, player, and transaction data
- [MLB Stats API](https://statsapi.mlb.com) for schedule and player information
