import requests
import urllib.parse
import json
import webbrowser
from datetime import datetime, timedelta
import os

from dotenv import load_dotenv
from mlb_client import get_team_schedule_map

load_dotenv()


def _ascii_normalize(name: str) -> str:
    """Lowercase and strip common accent characters for name matching."""
    replacements = {
        "á": "a", "à": "a", "ä": "a", "â": "a",
        "é": "e", "è": "e", "ê": "e", "ë": "e",
        "í": "i", "ì": "i", "î": "i", "ï": "i",
        "ó": "o", "ò": "o", "ô": "o", "ö": "o",
        "ú": "u", "ù": "u", "û": "u", "ü": "u",
        "ñ": "n", "ç": "c",
    }
    result = name.lower()
    for accented, plain in replacements.items():
        result = result.replace(accented, plain)
    return result


def _classify_strategy_tier(rank: int, age: int | None) -> dict:
    """
    Score a player on two strategy axes and return tier labels.

    Win-Now Score (0-100): How valuable is this player for winning THIS season?
      Driven by Yahoo rank, slightly penalized for age 33+ due to decline/injury risk.

    Keeper Score (0-100): How valuable is this player to keep for NEXT season?
      Driven by age (younger = higher) weighted by rank (must still be good).

    Age tiers:
      ≤23  prospect  — ascending ceiling, may underperform this year
      24-26 ascending — best keeper targets: good now AND young
      27-29 prime     — win now AND still keepable, slight decline risk by next year
      30-32 veteran   — strong win-now, keeper value fading
      33+  declining  — win now only, flag as bad keeper pick
    """
    if age is None:
        return {
            "win_now_score": None,
            "keeper_score": None,
            "sell_high_score": None,
            "age_tier": "unknown",
            "keeper_flag": "neutral",
        }

    # Win-now score: starts from rank position (lower rank = higher score)
    # Rank 1 = 100, rank 200 = 0, linearly scaled
    rank_score = max(0, 100 - (rank - 1) * 0.5)

    # Age penalty for win-now (33+ players carry more injury/decline risk)
    if age >= 36:
        win_now_penalty = 15
    elif age >= 34:
        win_now_penalty = 8
    elif age >= 32:
        win_now_penalty = 3
    else:
        win_now_penalty = 0
    win_now_score = max(0, round(rank_score - win_now_penalty, 1))

    # Keeper score: age is the primary driver, but player must still be relevant
    # A player ranked outside top 150 probably won't be a good keeper regardless of age
    relevance_factor = max(0.2, 1 - (rank - 1) / 150)
    if age <= 23:
        age_factor = 1.0      # ceiling play
    elif age <= 25:
        age_factor = 0.95
    elif age <= 27:
        age_factor = 0.85
    elif age <= 29:
        age_factor = 0.65
    elif age <= 31:
        age_factor = 0.40
    elif age <= 33:
        age_factor = 0.18
    else:
        age_factor = 0.05     # essentially no keeper value

    keeper_score = round(100 * age_factor * relevance_factor, 1)

    # Age tier labels
    if age <= 23:
        age_tier = "prospect"
    elif age <= 26:
        age_tier = "ascending"
    elif age <= 29:
        age_tier = "prime"
    elif age <= 32:
        age_tier = "veteran"
    else:
        age_tier = "declining"

    # Keeper flag
    if age_factor >= 0.85:
        keeper_flag = "strong"
    elif age_factor >= 0.55:
        keeper_flag = "good"
    elif age_factor >= 0.30:
        keeper_flag = "moderate"
    elif age_factor >= 0.12:
        keeper_flag = "low"
    else:
        keeper_flag = "avoid"

    # Sell-high score: peaks for players who are productive NOW but won't be worth keeping.
    # Formula: win_now_score * (1 - keeper_score/100)
    # A player who scores 90 win-now and 5 keeper = sell_high 85.5 (ideal trade chip)
    # A young star who scores 90 win-now and 90 keeper = sell_high 9 (don't trade, keep)
    # A washed player who scores 20 win-now and 5 keeper = sell_high 19 (not worth trading)
    sell_high_score = round(win_now_score * (1 - keeper_score / 100), 1)

    return {
        "win_now_score": win_now_score,
        "keeper_score": keeper_score,
        "sell_high_score": sell_high_score,
        "age_tier": age_tier,
        "keeper_flag": keeper_flag,
    }


def _extract_selected_position(player_obj) -> str | None:
    """Extract the selected Yahoo lineup slot from a player payload."""
    for element in player_obj or []:
        if not isinstance(element, dict) or "selected_position" not in element:
            continue
        selected = element["selected_position"]
        if isinstance(selected, list):
            for item in selected:
                if isinstance(item, dict) and item.get("position"):
                    return str(item["position"])
        elif isinstance(selected, dict) and selected.get("position"):
            return str(selected["position"])
    return None


def _extract_player_rank(player_obj) -> int | None:
    """Extract the first Yahoo rank value from a player payload."""
    for element in player_obj or []:
        if not isinstance(element, dict) or "player_ranks" not in element:
            continue
        ranks = element["player_ranks"]
        if isinstance(ranks, list):
            for rank_entry in ranks:
                if not isinstance(rank_entry, dict):
                    continue
                if "rank" in rank_entry:
                    try:
                        return int(rank_entry["rank"])
                    except (TypeError, ValueError):
                        continue
                player_rank = rank_entry.get("player_rank")
                if isinstance(player_rank, dict) and player_rank.get("rank_value"):
                    try:
                        return int(player_rank["rank_value"])
                    except (TypeError, ValueError):
                        continue
        elif isinstance(ranks, dict):
            if "rank" in ranks:
                try:
                    return int(ranks["rank"])
                except (TypeError, ValueError):
                    return None
            player_rank = ranks.get("player_rank")
            if isinstance(player_rank, dict) and player_rank.get("rank_value"):
                try:
                    return int(player_rank["rank_value"])
                except (TypeError, ValueError):
                    return None
    return None


def _extract_starting_status(player_obj) -> tuple[bool | None, str | None]:
    """Extract Yahoo starting-lineup status and batting order when present."""
    for element in player_obj or []:
        if not isinstance(element, dict):
            continue
        if "starting_status" not in element:
            continue
        is_starting = None
        batting_order = None
        starting_status = element.get("starting_status")
        if isinstance(starting_status, list):
            for item in starting_status:
                if isinstance(item, dict) and "is_starting" in item:
                    is_starting = bool(item["is_starting"])
                    break
        elif isinstance(starting_status, dict) and "is_starting" in starting_status:
            is_starting = bool(starting_status["is_starting"])

        order_block = element.get("batting_order")
        if isinstance(order_block, list):
            for item in order_block:
                if isinstance(item, dict) and item.get("order_num"):
                    batting_order = str(item["order_num"])
                    break
        elif isinstance(order_block, dict) and order_block.get("order_num"):
            batting_order = str(order_block["order_num"])

        return is_starting, batting_order
    return None, None


def _extract_player_core_info(player_obj) -> dict:
    """Parse a Yahoo player payload into a stable, app-friendly dict."""
    player_info = {
        "player_key": None,
        "name": "Unknown",
        "team": "Unknown",
        "display_position": None,
        "eligible_positions": [],
        "positions": [],
        "selected_position": None,
        "opponent": None,
        "has_game_today": None,
        "is_starting": None,
        "batting_order": None,
        "yahoo_rank": None,
    }

    for element in player_obj or []:
        if isinstance(element, list):
            for item in element:
                if not isinstance(item, dict):
                    continue
                if "player_key" in item:
                    player_info["player_key"] = item["player_key"]
                elif "name" in item:
                    player_info["name"] = item["name"].get("full", "Unknown")
                elif "editorial_team_abbr" in item:
                    player_info["team"] = item["editorial_team_abbr"]
                elif "display_position" in item:
                    player_info["display_position"] = item["display_position"]
                elif "eligible_positions" in item:
                    positions = item["eligible_positions"]
                    if isinstance(positions, list):
                        player_info["eligible_positions"] = [
                            pos.get("position", pos) if isinstance(pos, dict) else str(pos)
                            for pos in positions
                            if (isinstance(pos, dict) and pos.get("position")) or isinstance(pos, str)
                        ]

    player_info["selected_position"] = _extract_selected_position(player_obj)
    player_info["is_starting"], player_info["batting_order"] = _extract_starting_status(player_obj)
    player_info["yahoo_rank"] = _extract_player_rank(player_obj)

    if player_info["eligible_positions"]:
        player_info["positions"] = list(player_info["eligible_positions"])
    elif player_info["display_position"]:
        player_info["positions"] = [player_info["display_position"]]

    if player_info["display_position"] is None and player_info["positions"]:
        player_info["display_position"] = player_info["positions"][0]

    return player_info


def _extract_player_stats_map(player_obj) -> dict[str, str]:
    """Extract Yahoo stat_id -> raw value mappings from a player payload."""
    for element in player_obj or []:
        if not isinstance(element, dict) or "player_stats" not in element:
            continue
        player_stats = element["player_stats"]
        stats = player_stats.get("stats", []) if isinstance(player_stats, dict) else []
        stat_map: dict[str, str] = {}
        if isinstance(stats, list):
            for row in stats:
                if not isinstance(row, dict):
                    continue
                stat = row.get("stat")
                if not isinstance(stat, dict) or "stat_id" not in stat:
                    continue
                stat_map[str(stat["stat_id"])] = str(stat.get("value", ""))
        return stat_map
    return {}


class YahooFantasyAPI:
    """
    Yahoo Fantasy Sports API client with OAuth 2.0 authentication
    Based on: https://developer.yahoo.com/fantasysports/guide/
    """
    
    def __init__(self, client_id, client_secret, redirect_uri='https://259839fa5b6e.ngrok-free.app/callback'):
        """
        Initialize the Yahoo Fantasy API client
        
        Args:
            client_id (str): Your Yahoo app's client ID
            client_secret (str): Your Yahoo app's client secret  
            redirect_uri (str): Your app's redirect URI (must match Yahoo app settings)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        
        # OAuth URLs
        self.auth_url = 'https://api.login.yahoo.com/oauth2/request_auth'
        self.token_url = 'https://api.login.yahoo.com/oauth2/get_token'
        
        # Fantasy Sports API base URL
        self.base_url = 'https://fantasysports.yahooapis.com/fantasy/v2'
        
        # Token storage
        self.access_token = None
        self.refresh_token = None
        self.token_expires = None
        
        # Token file for persistence
        self.token_file = 'yahoo_tokens.json'
        
        # Load existing tokens if available
        self._load_tokens()
    
    def get_auth_url(self):
        """
        Generate the Yahoo OAuth authorization URL
        
        Returns:
            str: Authorization URL that user needs to visit
        """
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': 'fspt-w',  # Fantasy Sports Read/Write scope (matches your app permissions)
            'language': 'en-us'
        }
        
        auth_url = f"{self.auth_url}?{urllib.parse.urlencode(params)}"
        return auth_url
    
    def authenticate(self):
        """
        Start the OAuth authentication flow
        This will open a browser window for user authentication
        """
        auth_url = self.get_auth_url()
        
        print("Opening browser for Yahoo authentication...")
        print(f"If the browser doesn't open, visit this URL manually:")
        print(f"{auth_url}\n")
        
        # Open the auth URL in the user's browser
        webbrowser.open(auth_url)
        
        # Get the authorization code from user
        print("After authorizing the app, you'll be redirected to your redirect URI.")
        print("Copy the 'code' parameter from the URL and paste it here.")
        auth_code = input("Enter the authorization code: ").strip()
        
        if auth_code:
            return self.exchange_code_for_token(auth_code)
        else:
            print("No authorization code provided.")
            return False
    
    def exchange_code_for_token(self, auth_code):
        """
        Exchange authorization code for access token
        
        Args:
            auth_code (str): Authorization code from Yahoo
            
        Returns:
            bool: True if successful, False otherwise
        """
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
            'code': auth_code,
            'grant_type': 'authorization_code'
        }
        
        try:
            response = requests.post(self.token_url, data=data)
            response.raise_for_status()
            
            token_data = response.json()
            
            self.access_token = token_data['access_token']
            self.refresh_token = token_data['refresh_token']
            
            # Calculate token expiration time
            expires_in = token_data.get('expires_in', 3600)  # Default 1 hour
            self.token_expires = datetime.now() + timedelta(seconds=expires_in)
            
            print("✅ Authentication successful!")
            self._save_tokens()
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error exchanging code for token: {e}")
            if hasattr(e.response, 'text'):
                print(f"Response: {e.response.text}")
            return False
    
    def refresh_access_token(self):
        """
        Refresh the access token using the refresh token
        
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.refresh_token:
            print("❌ No refresh token available. Please re-authenticate.")
            return False
        
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token'
        }
        
        try:
            response = requests.post(self.token_url, data=data)
            response.raise_for_status()
            
            token_data = response.json()
            
            self.access_token = token_data['access_token']
            if 'refresh_token' in token_data:
                self.refresh_token = token_data['refresh_token']
            
            # Calculate token expiration time
            expires_in = token_data.get('expires_in', 3600)
            self.token_expires = datetime.now() + timedelta(seconds=expires_in)
            
            print("✅ Token refreshed successfully!")
            self._save_tokens()
            return True
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error refreshing token: {e}")
            return False
    
    def _is_token_expired(self):
        """Check if the current access token is expired"""
        if not self.token_expires:
            return True
        return datetime.now() >= self.token_expires
    
    def _ensure_valid_token(self):
        """Ensure we have a valid access token, refreshing if necessary"""
        if not self.access_token:
            print("❌ No access token. Please authenticate first.")
            return False
        
        if self._is_token_expired():
            print("🔄 Token expired, attempting to refresh...")
            if not self.refresh_access_token():
                print("❌ Token refresh failed. Please re-authenticate.")
                return False
        
        return True
    
    def _save_tokens(self):
        """Save tokens to file for persistence"""
        token_data = {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'token_expires': self.token_expires.isoformat() if self.token_expires else None
        }
        
        try:
            with open(self.token_file, 'w') as f:
                json.dump(token_data, f)
        except Exception as e:
            print(f"⚠️  Warning: Could not save tokens: {e}")
    
    def _load_tokens(self):
        """Load tokens from file if available"""
        try:
            if os.path.exists(self.token_file):
                with open(self.token_file, 'r') as f:
                    token_data = json.load(f)
                
                self.access_token = token_data.get('access_token')
                self.refresh_token = token_data.get('refresh_token')
                
                if token_data.get('token_expires'):
                    self.token_expires = datetime.fromisoformat(token_data['token_expires'])
                
                print("📂 Loaded existing tokens from file")
        except Exception as e:
            print(f"⚠️  Warning: Could not load tokens: {e}")
    
    def make_api_request(self, endpoint, method='GET', data=None):
        """
        Make an authenticated API request to Yahoo Fantasy Sports API
        
        Args:
            endpoint (str): API endpoint (without base URL)
            method (str): HTTP method ('GET', 'POST', etc.)
            data (dict): Request data for POST requests (JSON)
            
        Returns:
            dict: JSON response or None if error
        """
        if not self._ensure_valid_token():
            return None
        
        sep = '&' if '?' in endpoint else '?'
        url = f"{self.base_url}{endpoint}{sep}format=json"
        
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/json'
        }
        
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers)
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, json=data)
            else:
                print(f"❌ Unsupported HTTP method: {method}")
                return None
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            print(f"❌ API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response status: {e.response.status_code}")
                print(f"Response content: {e.response.text}")
            return None
    
    def make_api_request_xml(self, endpoint, method='PUT', xml_body=None):
        """
        Make an authenticated API request with XML body (for roster PUT, transactions POST)
        
        Args:
            endpoint (str): API endpoint (without base URL)
            method (str): HTTP method ('PUT' or 'POST')
            xml_body (str): XML string for request body
            
        Returns:
            dict: JSON response or None if error
        """
        if not self._ensure_valid_token():
            return None
        
        url = f"{self.base_url}{endpoint}"
        
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Content-Type': 'application/xml'
        }
        
        try:
            if method.upper() == 'PUT':
                response = requests.put(url, headers=headers, data=xml_body.encode('utf-8'))
            elif method.upper() == 'POST':
                response = requests.post(url, headers=headers, data=xml_body.encode('utf-8'))
            else:
                print(f"❌ Unsupported HTTP method for XML request: {method}")
                return None
            
            response.raise_for_status()
            if response.text:
                try:
                    return response.json()
                except ValueError:
                    pass  # Yahoo may return XML for write operations
            return {"success": True} if response.status_code == 200 else {}
            
        except requests.exceptions.RequestException as e:
            print(f"❌ API request failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response status: {e.response.status_code}")
                print(f"Response content: {e.response.text}")
            return None
    
    def get_user_games(self):
        """Get all games for the authenticated user"""
        result = self.make_api_request('/users;use_login=1/games')
        if result:
            return result.get('fantasy_content', {}).get('users', {}).get('0', {}).get('user', [{}])[1].get('games', {})
        return None
    
    def get_user_leagues(self, game_key=None):
        """Get all leagues for the authenticated user. If game_key is None, fetches from all games (MLB, etc.)."""
        if game_key:
            endpoint = f'/users;use_login=1/games;game_keys={game_key}/leagues'
        else:
            endpoint = '/users;use_login=1/games/leagues'
        result = self.make_api_request(endpoint)
        if result:
            return result.get('fantasy_content', {})
        return None
    
    def get_league_info(self, league_key):
        """Get information about a specific league"""
        result = self.make_api_request(f'/league/{league_key}')
        if result:
            return result.get('fantasy_content', {}).get('league', [{}])[0]
        return None
    
    def get_league_teams(self, league_key):
        """Get all teams in a specific league"""
        result = self.make_api_request(f'/league/{league_key}/teams')
        if result:
            return result.get('fantasy_content', {}).get('league', [{}])[1].get('teams', {})
        return None

    def get_league_teams_list(self, league_key) -> list[dict]:
        """Return [{team_key, name}, ...] for a league (convenience for roster iteration)."""
        raw = self.get_league_teams(league_key)
        if not raw:
            return []
        teams: list[dict] = []
        for k, v in raw.items():
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
        return teams

    def get_league_scoring_settings(self, league_key: str) -> dict | None:
        """
        Fetch /league/{league_key}/settings and parse scoring stat categories.

        Returns:
            dict with keys:
              batting_categories: list of {stat_id, name, position_type}
              pitching_categories: same
              roster_positions: optional list of {position, count} if present
            or None on failure.
        """
        result = self.make_api_request(f'/league/{league_key}/settings')
        if not result:
            return None
        try:
            fc = result.get('fantasy_content', {})
            league_block = fc.get('league', [])
            settings_obj: dict = {}
            if isinstance(league_block, list) and len(league_block) > 1:
                second = league_block[1]
                if isinstance(second, dict):
                    settings_obj = second
            elif isinstance(league_block, dict):
                settings_obj = league_block
            if not settings_obj and isinstance(league_block, list):
                for item in league_block:
                    if isinstance(item, dict) and item.get('settings'):
                        settings_obj = item
                        break

            settings_inner = settings_obj.get('settings', [])
            if isinstance(settings_inner, list) and settings_inner:
                settings_root = settings_inner[0] if isinstance(settings_inner[0], dict) else {}
            elif isinstance(settings_inner, dict):
                settings_root = settings_inner
            else:
                settings_root = {}

            batting: list[dict] = []
            pitching: list[dict] = []
            seen: set[tuple[str, str]] = set()

            def add_stat(stat_dict: dict) -> None:
                if not isinstance(stat_dict, dict):
                    return
                sid = stat_dict.get('stat_id')
                if sid is None:
                    return
                name = (
                    stat_dict.get('display_name')
                    or stat_dict.get('name')
                    or str(sid)
                )
                pos_type = (stat_dict.get('position_type') or "").upper()
                key = (str(sid), name)
                if key in seen:
                    return
                seen.add(key)
                row = {"stat_id": str(sid), "name": str(name), "position_type": pos_type}
                if pos_type == "B":
                    batting.append(row)
                elif pos_type == "P":
                    pitching.append(row)
                else:
                    # Some leagues omit position_type on stats — bucket by common IDs
                    # Default: treat unknown as both lists omitted; push to batting if looks like hitting stat
                    low = str(name).lower()
                    if any(x in low for x in ('era', 'whip', 'save', 'strikeout', 'quality', 'inn', 'win', 'hold')):
                        pitching.append(row)
                    else:
                        batting.append(row)

            def walk(obj) -> None:
                if isinstance(obj, dict):
                    if 'stat' in obj and isinstance(obj['stat'], dict):
                        add_stat(obj['stat'])
                    for v in obj.values():
                        walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)

            sc_block = settings_root.get('stat_categories')
            if sc_block is not None:
                walk(sc_block)
            else:
                walk(settings_root)

            roster_positions: list[dict] = []
            rp = settings_root.get('roster_positions')
            if isinstance(rp, list):
                for item in rp:
                    if isinstance(item, dict) and 'roster_position' in item:
                        rpos = item['roster_position']
                        if isinstance(rpos, dict):
                            roster_positions.append({
                                'position': rpos.get('position'),
                                'count': rpos.get('count'),
                            })

            return {
                'batting_categories': batting,
                'pitching_categories': pitching,
                'roster_positions': roster_positions,
            }
        except (KeyError, TypeError, IndexError) as e:
            print(f"⚠️  Could not parse league settings for {league_key}: {e}")
            return None

    def get_team_roster(self, team_key, date=None):
        """Get roster for a specific team, optionally for a specific date (MLB)"""
        endpoint = f'/team/{team_key}/roster'
        if date:
            endpoint += f';date={date}'
        result = self.make_api_request(endpoint)
        if result:
            return result.get('fantasy_content', {}).get('team', [{}])[1].get('roster', {}).get('0', {}).get('players', {})
        return None

    def get_team_roster_details(self, team_key, date=None) -> list[dict]:
        """Return parsed roster players with slot, eligibility, and game-day metadata."""
        roster = self.get_team_roster(team_key, date=date)
        if not roster:
            return []
        schedule_map = get_team_schedule_map(date=date)

        players: list[dict] = []
        for key, val in roster.items():
            if not key.isdigit():
                continue
            player_obj = val.get("player", [])
            parsed = _extract_player_core_info(player_obj)
            if parsed.get("player_key"):
                team_abbr = parsed.get("team")
                matchup = schedule_map.get(team_abbr or "")
                if matchup:
                    parsed["has_game_today"] = bool(matchup.get("has_game_today"))
                    parsed["opponent"] = matchup.get("opponent")
                elif parsed.get("has_game_today") is None:
                    parsed["has_game_today"] = False
                players.append(parsed)
        return players

    def get_team_roster_stats(self, team_key, date=None, stat_type="lastweek", season=None) -> list[dict]:
        """Return parsed roster players enriched with Yahoo stat window data."""
        endpoint = f'/team/{team_key}/roster'
        if date:
            endpoint += f';date={date}'
        endpoint += f'/players/stats;type={stat_type}'
        if season:
            endpoint += f';season={season}'
        result = self.make_api_request(endpoint)
        if not result:
            return []

        try:
            roster = result.get('fantasy_content', {}).get('team', [{}])[1].get('roster', {}).get('0', {}).get('players', {})
        except (KeyError, TypeError, IndexError):
            return []

        schedule_map = get_team_schedule_map(date=date)
        players: list[dict] = []
        for key, val in roster.items():
            if not key.isdigit():
                continue
            player_obj = val.get("player", [])
            parsed = _extract_player_core_info(player_obj)
            if not parsed.get("player_key"):
                continue
            team_abbr = parsed.get("team")
            matchup = schedule_map.get(team_abbr or "")
            if matchup:
                parsed["has_game_today"] = bool(matchup.get("has_game_today"))
                parsed["opponent"] = matchup.get("opponent")
            elif parsed.get("has_game_today") is None:
                parsed["has_game_today"] = False
            parsed["stats_map"] = _extract_player_stats_map(player_obj)
            parsed["stat_type"] = stat_type
            players.append(parsed)
        return players
    
    def get_player_stats(self, player_key, stat_type="season", date=None):
        """Get stats for a specific player"""
        endpoint = f'/player/{player_key}/stats'
        if date:
            endpoint += f';date={date}'
        elif stat_type:
            endpoint += f';type={stat_type}'
        
        result = self.make_api_request(endpoint)
        if result:
            return result.get('fantasy_content', {}).get('player', [{}])[1].get('player_stats', {})
        return None
    
    def is_player_starting(self, player_key, date, verbose=True):
        """
        Check if a player is starting on a specific date
        
        Args:
            player_key (str): Yahoo player key
            date (str): Date in YYYY-MM-DD format
            verbose (bool): If True, print status to stdout
            
        Returns:
            dict: Starting status information
        """
        if verbose:
            print(f"\n🔍 Checking if player {player_key} is starting on {date}")
        
        # Get player stats for the specific date
        stats = self.get_player_stats(player_key, date=date)
        
        if not stats:
            if verbose:
                print("❌ Could not retrieve player stats")
            return None
        
        # Look for Games Started stat (stat_id: 25)
        games_started = None
        stats_data = stats.get('stats', [])
        
        for stat in stats_data:
            if stat.get('stat', {}).get('stat_id') == '25':  # Games Started
                games_started = stat.get('stat', {}).get('value', '-')
                break
        
        is_starting = games_started == '1' if games_started != '-' else False
        
        if verbose:
            print(f"📊 Games Started Value: {games_started}")
            print(f"🎯 Starting Status: {'✅ STARTING' if is_starting else '❌ NOT STARTING'}")
        
        return {
            'player_key': player_key,
            'date': date,
            'games_started_value': games_started,
            'is_starting': is_starting
        }
    
    def is_player_starting_with_lineup(self, player_key, team_key, date):
        """
        Enhanced version that shows both starting status and fantasy lineup assignment
        
        Args:
            player_key (str): Yahoo player key
            team_key (str): Yahoo team key
            date (str): Date in YYYY-MM-DD format
            
        Returns:
            dict: Complete player status information
        """
        print(f"\n🔍 Checking starting status with fantasy assignment for player {player_key} on {date}")
        
        # Get basic starting status
        starting_info = self.is_player_starting(player_key, date)
        
        # Get fantasy assignment
        fantasy_assignment = self.get_player_fantasy_assignment(player_key, team_key, date)
        
        # Get player basic info
        player_info = self.get_player_info(player_key)
        
        if starting_info and player_info:
            result = {
                'player_key': player_key,
                'player_name': player_info.get('name', 'Unknown'),
                'positions': player_info.get('eligible_positions', []),
                'team': player_info.get('team', 'Unknown'),
                'date': date,
                'is_starting': starting_info['is_starting'],
                'games_started_value': starting_info['games_started_value'],
                'fantasy_assignment': fantasy_assignment
            }
            
            # Display formatted result
            print(f"\n🏟️  {result['player_name']} ({','.join(result['positions'])}) - {result['team']}")
            print(f"📅 Date: {date}")
            print(f"🎯 Starting Status: {'✅ STARTING' if result['is_starting'] else '❌ NOT STARTING'}")
            print(f"📊 Games Started Value: {result['games_started_value']}")
            print(f"🎯 Fantasy Assignment: {self._format_fantasy_position(fantasy_assignment)}")
            print(f"📋 Eligible Positions: {', '.join(result['positions'])}")
            
            return result
        
        print("❌ Could not retrieve complete player information")
        return None
    
    def _format_fantasy_position(self, position):
        """Format fantasy position with emoji"""
        position_emojis = {
            'BN': '🪑 BN',
            'C': '🥎 C',
            '1B': '1️⃣ 1B',
            '2B': '2️⃣ 2B',
            '3B': '3️⃣ 3B',
            'SS': '🔹 SS',
            'OF': '🌟 OF',
            'Util': '🔧 Util',
            'SP': '🎯 SP',
            'RP': '🔥 RP',
            'P': '⚾ P',
            'DL': '🏥 DL',
            'NA': '❓ NA'
        }
        return position_emojis.get(position, f"📍 {position}")
    
    def get_team_starting_status(self, team_key, date):
        """
        Get starting status for all players on a team
        
        Args:
            team_key (str): Yahoo team key
            date (str): Date in YYYY-MM-DD format
        """
        print(f"\n📊 Getting starting status for team {team_key} on {date}")
        
        # Get team roster
        roster = self.get_team_roster(team_key)
        
        if not roster:
            print("❌ Could not retrieve team roster")
            return
        
        starting_players = []
        not_starting_players = []
        
        for player_num, player_info in roster.items():
            if player_num.isdigit():
                try:
                    # Get player key and basic info
                    player_key = None
                    player_name = "Unknown"
                    position = "Unknown"
                    selected_position = "Unknown"
                    
                    # Check all elements in the player array
                    for element in player_info['player']:
                        if isinstance(element, list):
                            # This is the player info array (element [0])
                            for item in element:
                                if isinstance(item, dict):
                                    if 'player_key' in item:
                                        player_key = item['player_key']
                                    elif 'name' in item:
                                        player_name = item['name']['full']
                                    elif 'display_position' in item:
                                        position = item['display_position']
                        elif isinstance(element, dict) and 'selected_position' in element:
                            # This is the selected_position element
                            selected_pos = element['selected_position']
                            if isinstance(selected_pos, list):
                                for pos_item in selected_pos:
                                    if isinstance(pos_item, dict) and 'position' in pos_item:
                                        selected_position = pos_item['position']
                                        break
                            elif isinstance(selected_pos, dict) and 'position' in selected_pos:
                                selected_position = selected_pos['position']
                    
                    if player_key:
                        # Check starting status
                        starting_info = self.is_player_starting(player_key, date)
                        
                        player_data = {
                            'name': player_name,
                            'position': position,
                            'selected_position': selected_position,
                            'player_key': player_key,
                            'is_starting': starting_info['is_starting'] if starting_info else False,
                            'games_started_value': starting_info['games_started_value'] if starting_info else '-'
                        }
                        
                        if player_data['is_starting']:
                            starting_players.append(player_data)
                        else:
                            not_starting_players.append(player_data)
                
                except Exception as e:
                    print(f"⚠️  Error processing player {player_num}: {e}")
                    continue
        
        # Display results
        print(f"\n✅ Starting Players ({len(starting_players)}):")
        for player in starting_players:
            print(f"  🎯 {player['name']} ({player['position']}) - {self._format_fantasy_position(player['selected_position'])}")
        
        print(f"\n❌ Not Starting Players ({len(not_starting_players)}):")
        for player in not_starting_players:
            print(f"  🪑 {player['name']} ({player['position']}) - {self._format_fantasy_position(player['selected_position'])}")
    
    def get_sp_games_started(self, team_key, date):
        """
        Get games started specifically for Starting Pitchers on a team
        
        Args:
            team_key (str): Yahoo team key
            date (str): Date in YYYY-MM-DD format
        """
        print(f"\n🎯 Getting SP games started for team {team_key} on {date}")
        
        # Get team roster
        roster = self.get_team_roster(team_key)
        
        if not roster:
            print("❌ Could not retrieve team roster")
            return
        
        sp_players = []
        
        for player_num, player_info in roster.items():
            if player_num.isdigit():
                try:
                    # Get player key and basic info
                    player_key = None
                    player_name = "Unknown"
                    position = "Unknown"
                    selected_position = "Unknown"
                    
                    # Check all elements in the player array
                    for element in player_info['player']:
                        if isinstance(element, list):
                            # This is the player info array (element [0])
                            for item in element:
                                if isinstance(item, dict):
                                    if 'player_key' in item:
                                        player_key = item['player_key']
                                    elif 'name' in item:
                                        player_name = item['name']['full']
                                    elif 'display_position' in item:
                                        position = item['display_position']
                        elif isinstance(element, dict) and 'selected_position' in element:
                            # This is the selected_position element
                            selected_pos = element['selected_position']
                            if isinstance(selected_pos, list):
                                for pos_item in selected_pos:
                                    if isinstance(pos_item, dict) and 'position' in pos_item:
                                        selected_position = pos_item['position']
                                        break
                            elif isinstance(selected_pos, dict) and 'position' in selected_pos:
                                selected_position = selected_pos['position']
                    
                    # Only check SP players
                    if player_key and ('SP' in position or selected_position == 'SP'):
                        starting_info = self.is_player_starting(player_key, date)
                        
                        sp_players.append({
                            'name': player_name,
                            'position': position,
                            'selected_position': selected_position,
                            'player_key': player_key,
                            'is_starting': starting_info['is_starting'] if starting_info else False,
                            'games_started_value': starting_info['games_started_value'] if starting_info else '-'
                        })
                
                except Exception as e:
                    print(f"⚠️  Error processing player {player_num}: {e}")
                    continue
        
        # Display results
        print(f"\n🎯 Starting Pitchers Analysis ({len(sp_players)} SP found):")
        for player in sp_players:
            status = "✅ STARTING" if player['is_starting'] else "❌ NOT STARTING"
            print(f"  {status} - {player['name']} ({player['position']}) - {self._format_fantasy_position(player['selected_position'])}")
            print(f"    📊 Games Started Value: {player['games_started_value']}")
    
    def get_player_fantasy_assignment(self, player_key, team_key, date):
        """Get just the fantasy lineup assignment for a player"""
        try:
            result = self.make_api_request(f'/team/{team_key}/roster;date={date}')
            
            if not result:
                print("❌ Could not retrieve roster data")
                return "Unknown"
                
            roster_data = result.get('fantasy_content', {})
            
            if 'team' not in roster_data:
                print("❌ No team data found in roster")
                return "Unknown"
                
            team = roster_data['team']
            if len(team) < 2 or 'roster' not in team[1]:
                print("❌ No roster data found")
                return "Unknown"
                
            roster = team[1]['roster']
            if '0' not in roster or 'players' not in roster['0']:
                print("❌ No players found in roster")
                return "Unknown"
                
            players = roster['0']['players']
            
            # Find the player in the roster
            for player_num, player_info in players.items():
                if player_num.isdigit():  # Only process numbered player entries
                    # Check all elements in the player array
                    for element in player_info['player']:
                        if isinstance(element, list):
                            # This is the player info array (element [0])
                            for item in element:
                                if isinstance(item, dict) and 'player_key' in item:
                                    if item['player_key'] == player_key:
                                        # Found our player, now look for selected_position
                                        for pos_element in player_info['player']:
                                            if isinstance(pos_element, dict) and 'selected_position' in pos_element:
                                                selected_pos = pos_element['selected_position']
                                                if isinstance(selected_pos, list):
                                                    for pos_item in selected_pos:
                                                        if isinstance(pos_item, dict) and 'position' in pos_item:
                                                            return pos_item['position']
                                                elif isinstance(selected_pos, dict) and 'position' in selected_pos:
                                                    return selected_pos['position']
                        elif isinstance(element, dict) and 'selected_position' in element:
                            # This might be the selected_position directly
                            selected_pos = element['selected_position']
                            if isinstance(selected_pos, list):
                                for pos_item in selected_pos:
                                    if isinstance(pos_item, dict) and 'position' in pos_item:
                                        return pos_item['position']
                            elif isinstance(selected_pos, dict) and 'position' in selected_pos:
                                return selected_pos['position']
            
            print("❌ Player not found in roster")
            return "Unknown"
            
        except Exception as e:
            print(f"❌ Error getting fantasy assignment: {e}")
            return "Unknown"
    
    def get_player_info(self, player_key):
        """Get basic player information"""
        result = self.make_api_request(f'/player/{player_key}')
        
        if not result:
            return None
        
        player_data = result.get('fantasy_content', {}).get('player', [{}])[0]
        
        # Extract player information
        player_info = {
            'player_key': player_key,
            'name': 'Unknown',
            'team': 'Unknown',
            'eligible_positions': []
        }
        
        for item in player_data:
            if isinstance(item, dict):
                if 'name' in item:
                    player_info['name'] = item['name']['full']
                elif 'editorial_team_abbr' in item:
                    player_info['team'] = item['editorial_team_abbr']
                elif 'eligible_positions' in item:
                    positions = item['eligible_positions']
                    if isinstance(positions, list):
                        player_info['eligible_positions'] = [pos['position'] for pos in positions if isinstance(pos, dict) and 'position' in pos]
        
        return player_info
    
    def get_league_players(self, league_key, status='FA', position=None, sort=None, sort_type='season', count=25, start=0):
        """
        Get available players in a league (waiver wire / free agents)
        
        Args:
            league_key (str): Yahoo league key (e.g. 458.l.3694)
            status (str): 'FA' (free agents), 'W' (waivers), 'A' (all available), 'T' (taken), 'K' (keepers)
            position (str): Filter by position (e.g. 'SP', '1B', 'OF')
            sort (str): Stat ID or 'NAME', 'OR', 'AR', 'PTS' for sorting
            sort_type (str): 'season', 'date', 'week', 'lastweek', 'lastmonth'
            count (int): Number of players to return
            start (int): Pagination offset
            
        Returns:
            dict: Parsed players data or None
        """
        endpoint = f'/league/{league_key}/players;status={status};count={count}'
        if start:
            endpoint += f';start={start}'
        if position:
            endpoint += f';position={position}'
        if sort:
            endpoint += f';sort={sort};sort_type={sort_type}'
        
        result = self.make_api_request(endpoint)
        if not result:
            return None
        
        try:
            league_data = result.get('fantasy_content', {}).get('league', [{}])
            if isinstance(league_data, list) and len(league_data) > 1:
                players_data = league_data[1].get('players', {})
            else:
                players_data = league_data.get('players', {}) if isinstance(league_data, dict) else {}
            
            return self._parse_players_collection(players_data)
        except (KeyError, TypeError):
            return None

    def get_roster_with_rankings(self, team_key, league_key, date=None, sort_type='lastweek', count=400):
        """
        Return the team's roster enriched with Yahoo ranks for the requested sort window.

        This fetches the daily roster for slot assignment / opponent context, then pages
        through the league's taken players sorted by the requested window and merges the
        ranks back onto the roster by player_key.
        """
        roster_players = self.get_team_roster_details(team_key, date=date)
        if not roster_players:
            return []

        roster_keys = {player["player_key"] for player in roster_players if player.get("player_key")}
        rank_by_key: dict[str, int | None] = {}

        start = 0
        batch_size = 25
        while True:
            taken_players = self.get_league_players(
                league_key,
                status='T',
                sort='OR',
                sort_type=sort_type,
                count=batch_size,
                start=start,
            )
            if not taken_players:
                break

            for index, player in enumerate(taken_players, start=1):
                player_key = player.get("player_key")
                if player_key in roster_keys:
                    rank_by_key[player_key] = start + index

            if len(rank_by_key) >= len(roster_keys) or len(taken_players) < batch_size:
                break
            start += batch_size

        enriched: list[dict] = []
        for player in roster_players:
            enriched.append({
                **player,
                "rank_sort_type": sort_type,
                "yahoo_rank": rank_by_key.get(player["player_key"]),
            })
        return enriched
    
    def _parse_players_collection(self, players_data):
        """Parse Yahoo players collection into a list of player dicts"""
        players = []
        if not players_data:
            return players
        
        for key, val in players_data.items():
            if not key.isdigit():
                continue
            player_obj = val.get('player', [{}])
            if not player_obj:
                continue
            
            player_info = _extract_player_core_info(player_obj)
            if player_info['player_key']:
                if player_info['yahoo_rank'] is None:
                    try:
                        player_info['yahoo_rank'] = int(key) + 1
                    except (TypeError, ValueError):
                        pass
                players.append(player_info)
        
        return players
    
    def get_league_keepers(self, league_key):
        """
        Get all players designated as keepers in the league, grouped by team.

        Uses Yahoo's status=K player filter (one efficient API call) rather than
        fetching every team's roster. The is_keeper field returns:
          { "status": true, "cost": false, "kept": true, "ik_tid": "4" }
        where ik_tid is the team number within the league.

        Returns:
            dict with keys:
              - keepers_by_team: { team_name: [player_dict, ...] }
              - keepers_by_team_key: { team_key: [player_dict, ...] }
              - all_keepers: [player_dict, ...]  (flat list)
              - teams_with_keepers: int
              - teams_total: int
        """
        # Build team ID -> team info map from league teams
        teams_raw = self.get_league_teams(league_key)
        if not teams_raw:
            return None

        team_by_tid = {}   # tid (string like "4") -> { team_key, name }
        teams_total = 0
        for k, v in teams_raw.items():
            if not k.isdigit():
                continue
            teams_total += 1
            t = v.get('team', [])
            team_key = None
            team_name = 'Unknown'
            team_id = None
            for elem in t:
                if isinstance(elem, list):
                    for item in elem:
                        if isinstance(item, dict):
                            if 'team_key' in item:
                                team_key = item['team_key']
                                # team_key format: "469.l.12479.t.4" → tid is last segment
                                team_id = item['team_key'].split('.')[-1]
                            elif 'name' in item:
                                team_name = item['name']
                elif isinstance(elem, dict):
                    if 'team_key' in elem:
                        team_key = elem['team_key']
                        team_id = elem['team_key'].split('.')[-1]
                    elif 'name' in elem:
                        team_name = elem['name']
            if team_key and team_id:
                team_by_tid[team_id] = {'team_key': team_key, 'name': team_name}

        # Fetch all keepers in one call — paginate in batches of 25 if needed
        all_keepers = []
        start = 0
        batch = 25
        while True:
            endpoint = f'/league/{league_key}/players;status=K;count={batch};start={start}'
            result = self.make_api_request(endpoint)
            if not result:
                break
            league_data = result.get('fantasy_content', {}).get('league', [{}])
            players_data = {}
            if isinstance(league_data, list) and len(league_data) > 1:
                players_data = league_data[1].get('players', {})

            fetched = 0
            for key, val in players_data.items():
                if not key.isdigit():
                    continue
                fetched += 1
                player_obj = val.get('player', [{}])
                player_key = None
                name = 'Unknown'
                mlb_team = 'Unknown'
                positions = []
                keeper_tid = None
                keeper_cost = None
                keeper_kept = False

                for element in player_obj:
                    if isinstance(element, list):
                        for item in element:
                            if isinstance(item, dict):
                                if 'player_key' in item:
                                    player_key = item['player_key']
                                elif 'name' in item:
                                    name = item['name'].get('full', 'Unknown')
                                elif 'editorial_team_abbr' in item:
                                    mlb_team = item['editorial_team_abbr']
                                elif 'display_position' in item:
                                    positions = [item['display_position']]
                                elif 'eligible_positions' in item:
                                    pos_list = item['eligible_positions']
                                    if isinstance(pos_list, list):
                                        positions = [p.get('position', '') for p in pos_list if isinstance(p, dict)]
                                elif 'is_keeper' in item:
                                    ik = item['is_keeper']
                                    if isinstance(ik, dict):
                                        keeper_tid = str(ik.get('ik_tid', ''))
                                        keeper_cost = ik.get('cost')
                                        keeper_kept = bool(ik.get('kept', False))
                                    else:
                                        keeper_kept = bool(ik)

                if player_key:
                    team_info = team_by_tid.get(keeper_tid, {})
                    all_keepers.append({
                        'player_key': player_key,
                        'name': name,
                        'team': mlb_team,
                        'positions': positions,
                        'fantasy_team': team_info.get('name', 'Unknown'),
                        'fantasy_team_key': team_info.get('team_key', ''),
                        'keeper_cost': keeper_cost,
                        'keeper_kept': keeper_kept,
                    })

            if fetched < batch:
                break  # no more pages
            start += batch

        # Group by team
        keepers_by_team: dict = {info['name']: [] for info in team_by_tid.values()}
        keepers_by_team_key: dict = {info['team_key']: [] for info in team_by_tid.values()}
        for keeper in all_keepers:
            fname = keeper['fantasy_team']
            fkey = keeper['fantasy_team_key']
            if fname in keepers_by_team:
                keepers_by_team[fname].append(keeper)
            if fkey in keepers_by_team_key:
                keepers_by_team_key[fkey].append(keeper)

        teams_with_keepers = sum(1 for v in keepers_by_team.values() if v)

        return {
            'keepers_by_team': keepers_by_team,
            'keepers_by_team_key': keepers_by_team_key,
            'all_keepers': all_keepers,
            'teams_with_keepers': teams_with_keepers,
            'teams_total': teams_total,
        }

    def get_player_rankings(self, league_key, count=200, sort='OR', age_lookup: dict | None = None):
        """
        Get top players by Yahoo's overall rank (OR) or average rank (AR),
        optionally enriched with age data from an MLB Stats API age_lookup dict.

        Args:
            league_key (str): Yahoo league key
            count (int): Number of players to return (max ~250 per request)
            sort (str): 'OR' = overall rank, 'AR' = average draft rank
            age_lookup (dict): Optional name->age dict from mlb_client.get_player_age_lookup()

        Returns:
            list of player dicts with rank, age, birth_date, and strategy tier info
        """
        endpoint = f'/league/{league_key}/players;sort={sort};sort_type=season;count={count};status=A'
        result = self.make_api_request(endpoint)
        if not result:
            return []

        try:
            league_data = result.get('fantasy_content', {}).get('league', [{}])
            if isinstance(league_data, list) and len(league_data) > 1:
                players_data = league_data[1].get('players', {})
            else:
                players_data = {}

            players = []
            for key, val in players_data.items():
                if not key.isdigit():
                    continue
                player_obj = val.get('player', [{}])
                player_info = {
                    'player_key': None,
                    'name': 'Unknown',
                    'team': 'Unknown',
                    'positions': [],
                    'rank': int(key) + 1,
                    'age': None,
                    'birth_date': None,
                }
                for element in player_obj:
                    if isinstance(element, list):
                        for item in element:
                            if isinstance(item, dict):
                                if 'player_key' in item:
                                    player_info['player_key'] = item['player_key']
                                elif 'name' in item:
                                    player_info['name'] = item['name'].get('full', 'Unknown')
                                elif 'editorial_team_abbr' in item:
                                    player_info['team'] = item['editorial_team_abbr']
                                elif 'display_position' in item:
                                    player_info['positions'] = [item['display_position']]
                    elif isinstance(element, dict):
                        if 'player_ranks' in element:
                            ranks = element['player_ranks']
                            if isinstance(ranks, list):
                                for r in ranks:
                                    if isinstance(r, dict) and 'rank' in r:
                                        try:
                                            player_info['rank'] = int(r['rank'])
                                        except (ValueError, TypeError):
                                            pass

                if player_info['player_key']:
                    # Enrich with age if lookup provided
                    if age_lookup and player_info['name'] != 'Unknown':
                        age_entry = (
                            age_lookup.get(player_info['name'].lower())
                            or age_lookup.get(_ascii_normalize(player_info['name']))
                        )
                        if age_entry:
                            player_info['age'] = age_entry['age']
                            player_info['birth_date'] = age_entry['birth_date']

                    player_info['strategy_tier'] = _classify_strategy_tier(
                        player_info['rank'], player_info['age']
                    )
                    players.append(player_info)

            return players
        except (KeyError, TypeError):
            return []

    def get_draft_results(self, league_key):
        """
        Get draft results from the most recent draft for this league.

        Returns:
            list of { round, pick, team_key, player_key, player_name }
        """
        result = self.make_api_request(f'/league/{league_key}/draftresults')
        if not result:
            return []

        try:
            draft_data = result.get('fantasy_content', {}).get('league', [{}])
            picks_raw = {}
            if isinstance(draft_data, list):
                for elem in draft_data:
                    if isinstance(elem, dict) and 'draft_results' in elem:
                        picks_raw = elem['draft_results']
                        break
            elif isinstance(draft_data, dict):
                picks_raw = draft_data.get('draft_results', {})

            picks = []
            # Yahoo may return draft_results as a dict keyed by "0","1",… or as a list of entries.
            if isinstance(picks_raw, dict):
                for key, val in picks_raw.items():
                    if not key.isdigit():
                        continue
                    pick = val.get('draft_result', {})
                    picks.append({
                        'pick': pick.get('pick'),
                        'round': pick.get('round'),
                        'team_key': pick.get('team_key'),
                        'player_key': pick.get('player_key'),
                    })
            elif isinstance(picks_raw, list):
                for val in picks_raw:
                    if not isinstance(val, dict):
                        continue
                    pick = val.get('draft_result', val)
                    if not isinstance(pick, dict):
                        continue
                    picks.append({
                        'pick': pick.get('pick'),
                        'round': pick.get('round'),
                        'team_key': pick.get('team_key'),
                        'player_key': pick.get('player_key'),
                    })

            picks.sort(key=lambda x: (x.get('pick') or 0))
            return picks
        except (KeyError, TypeError):
            return []

    def edit_roster(self, team_key, date, position_changes):
        """
        Edit lineup by moving players to new positions (PUT roster)
        
        Args:
            team_key (str): Yahoo team key
            date (str): Date in YYYY-MM-DD format (MLB uses date, not week)
            position_changes (list): List of (player_key, position) tuples
            
        Returns:
            dict: API response or None
        """
        players_xml = '\n'.join(
            f'      <player><player_key>{pk}</player_key><position>{pos}</position></player>'
            for pk, pos in position_changes
        )
        xml_body = f'''<?xml version="1.0"?>
<fantasy_content>
  <roster>
    <coverage_type>date</coverage_type>
    <date>{date}</date>
    <players>
{players_xml}
    </players>
  </roster>
</fantasy_content>'''
        
        return self.make_api_request_xml(f'/team/{team_key}/roster', method='PUT', xml_body=xml_body)

    def edit_roster_safe(self, team_key, date, position_changes):
        """
        Edit lineup one player at a time so one locked player does not block every move.

        Yahoo rejects a roster PUT if any included player is no longer editable. Sending
        individual PUTs lets editable players move while locked players fail independently.
        """
        result = {
            "applied": [],
            "failed": [],
        }
        ordered_changes = sorted(
            position_changes,
            key=lambda change: 0 if str(change[1]).upper() == "BN" else 1,
        )
        for player_key, position in ordered_changes:
            response = self.edit_roster(team_key, date, [(player_key, position)])
            move = {"player_key": player_key, "position": position}
            if response is None:
                result["failed"].append(move)
            else:
                result["applied"].append(move)
        result["success"] = bool(result["applied"]) and not result["failed"]
        result["partial_success"] = bool(result["applied"]) and bool(result["failed"])
        return result
    
    def add_drop_players(self, league_key, team_key, add_player_key, drop_player_key, faab_bid=None):
        """
        Add one player and drop another (add/drop transaction)
        
        Args:
            league_key (str): Yahoo league key
            team_key (str): Yahoo team key (your team)
            add_player_key (str): Player key to add
            drop_player_key (str): Player key to drop
            faab_bid (int): Optional FAAB bid amount for waiver leagues
            
        Returns:
            dict: API response or None
        """
        faab_line = f'    <faab_bid>{faab_bid}</faab_bid>\n' if faab_bid is not None else ''
        xml_body = f'''<?xml version="1.0"?>
<fantasy_content>
  <transaction>
    <type>add/drop</type>
{faab_line}    <players>
      <player>
        <player_key>{add_player_key}</player_key>
        <transaction_data>
          <type>add</type>
          <destination_team_key>{team_key}</destination_team_key>
        </transaction_data>
      </player>
      <player>
        <player_key>{drop_player_key}</player_key>
        <transaction_data>
          <type>drop</type>
          <source_team_key>{team_key}</source_team_key>
        </transaction_data>
      </player>
    </players>
  </transaction>
</fantasy_content>'''
        
        return self.make_api_request_xml(f'/league/{league_key}/transactions', method='POST', xml_body=xml_body)
    
    def add_player(self, league_key, team_key, player_key):
        """
        Add a free agent player (no drop)
        
        Args:
            league_key (str): Yahoo league key
            team_key (str): Yahoo team key (your team)
            player_key (str): Player key to add
            
        Returns:
            dict: API response or None
        """
        xml_body = f'''<?xml version="1.0"?>
<fantasy_content>
  <transaction>
    <type>add</type>
    <player>
      <player_key>{player_key}</player_key>
      <transaction_data>
        <type>add</type>
        <destination_team_key>{team_key}</destination_team_key>
      </transaction_data>
    </player>
  </transaction>
</fantasy_content>'''
        
        return self.make_api_request_xml(f'/league/{league_key}/transactions', method='POST', xml_body=xml_body)
    
    def drop_player(self, league_key, team_key, player_key):
        """
        Drop a player from your team
        
        Args:
            league_key (str): Yahoo league key
            team_key (str): Yahoo team key (your team)
            player_key (str): Player key to drop
            
        Returns:
            dict: API response or None
        """
        xml_body = f'''<?xml version="1.0"?>
<fantasy_content>
  <transaction>
    <type>drop</type>
    <player>
      <player_key>{player_key}</player_key>
      <transaction_data>
        <type>drop</type>
        <source_team_key>{team_key}</source_team_key>
      </transaction_data>
    </player>
  </transaction>
</fantasy_content>'''
        
        return self.make_api_request_xml(f'/league/{league_key}/transactions', method='POST', xml_body=xml_body)


def main():
    """Main function to run the Yahoo Fantasy API client"""
    print("Yahoo Fantasy Sports API Client")
    print("=" * 40)

    CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
    CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")
    REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI", "http://localhost:3000/callback")

    if not CLIENT_ID or not CLIENT_SECRET:
        print("Error: YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET must be set in .env")
        print("Copy .env.example to .env and add your Yahoo app credentials.")
        return

    api = YahooFantasyAPI(CLIENT_ID, CLIENT_SECRET, REDIRECT_URI)
    
    # Check if we have valid tokens
    if not api._ensure_valid_token():
        print("Please authenticate first...")
        if not api.authenticate():
            print("Authentication failed. Exiting.")
            return
    
    while True:
        print("\n" + "="*50)
        print("Yahoo Fantasy Baseball API Menu")
        print("="*50)
        print("1. Get user games")
        print("2. Get user leagues")
        print("3. Get league info")
        print("4. Get league teams")
        print("5. Get team roster")
        print("6. Get player stats")
        print("7. Check if player is starting")
        print("8. Check if player is starting (with fantasy assignment)")
        print("9. Get team starting status")
        print("10. Get SP games started for team")
        print("11. Get league free agents (waiver wire)")
        print("0. Exit")
        print("="*50)

        choice = input("Enter your choice (0-11): ").strip()
        
        if choice == '0':
            print("Goodbye!")
            break
        elif choice == '1':
            print("\n📊 Getting user games...")
            games = api.get_user_games()
            if games:
                print(f"Found {len(games)} games:")
                for game in games:
                    print(f"- {game}")
            else:
                print("No games found or error occurred.")
                
        elif choice == '2':
            print("\n🏆 Getting user leagues...")
            leagues = api.get_user_leagues()
            if leagues:
                print(f"Found {len(leagues)} leagues:")
                for league in leagues:
                    print(f"- {league}")
            else:
                print("No leagues found or error occurred.")
                
        elif choice == '3':
            league_key = input("Enter league key: ").strip()
            if league_key:
                print(f"\n📋 Getting league info for {league_key}...")
                info = api.get_league_info(league_key)
                if info:
                    print(f"League info: {info}")
                else:
                    print("League not found or error occurred.")
                    
        elif choice == '4':
            league_key = input("Enter league key: ").strip()
            if league_key:
                print(f"\n👥 Getting teams for league {league_key}...")
                teams = api.get_league_teams(league_key)
                if teams:
                    print(f"Found {len(teams)} teams:")
                    for team in teams:
                        print(f"- {team}")
                else:
                    print("No teams found or error occurred.")
                    
        elif choice == '5':
            team_key = input("Enter team key: ").strip()
            if team_key:
                print(f"\n📝 Getting roster for team {team_key}...")
                roster = api.get_team_roster(team_key)
                if roster:
                    print(f"Found {len(roster)} players:")
                    for player in roster:
                        print(f"- {player}")
                else:
                    print("No roster found or error occurred.")
                    
        elif choice == '6':
            player_key = input("Enter player key: ").strip()
            if player_key:
                print(f"\n📈 Getting stats for player {player_key}...")
                stats = api.get_player_stats(player_key)
                if stats:
                    print(f"Player stats: {stats}")
                else:
                    print("No stats found or error occurred.")
                    
        elif choice == '7':
            player_key = input("Enter player key: ").strip()
            date = input("Enter date (YYYY-MM-DD): ").strip()
            if player_key and date:
                print(f"\n🎯 Checking if player {player_key} is starting on {date}...")
                result = api.is_player_starting(player_key, date)
                if result:
                    print(f"Starting status: {result}")
                else:
                    print("Could not determine starting status.")
                    
        elif choice == '8':
            player_key = input("Enter player key: ").strip()
            team_key = input("Enter team key: ").strip()
            date = input("Enter date (YYYY-MM-DD): ").strip()
            if player_key and team_key and date:
                print(f"\n🎯 Checking starting status with fantasy assignment...")
                result = api.is_player_starting_with_lineup(player_key, team_key, date)
                if result:
                    print(f"Result: {result}")
                else:
                    print("Could not determine starting status.")
                    
        elif choice == '9':
            team_key = input("Enter team key: ").strip()
            date = input("Enter date (YYYY-MM-DD): ").strip()
            if team_key and date:
                print(f"\n📊 Getting team starting status for {team_key} on {date}...")
                api.get_team_starting_status(team_key, date)
                
        elif choice == '10':
            team_key = input("Enter team key: ").strip()
            date = input("Enter date (YYYY-MM-DD): ").strip()
            if team_key and date:
                print(f"\n🎯 Getting SP games started for {team_key} on {date}...")
                api.get_sp_games_started(team_key, date)

        elif choice == '11':
            league_key = input("Enter league key: ").strip()
            status = input("Status (FA=free agents, W=waivers) [FA]: ").strip() or 'FA'
            if league_key:
                print(f"\n📋 Getting {status} players for league {league_key}...")
                players = api.get_league_players(league_key, status=status)
                if players:
                    print(f"Found {len(players)} players:")
                    for p in players[:20]:
                        print(f"  - {p.get('name')} ({','.join(p.get('positions', []))}) - {p.get('team')} [{p.get('player_key')}]")
                    if len(players) > 20:
                        print(f"  ... and {len(players) - 20} more")
                else:
                    print("No players found or error occurred.")

        else:
            print("Invalid choice. Please try again.")


if __name__ == "__main__":
    main() 