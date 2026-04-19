#!/bin/bash
# Start ngrok + uvicorn for Fantasy Baseball app
# Run from fantasy_baseball/ directory

cd "$(dirname "$0")"

echo "Starting ngrok (port 8000)..."
ngrok http 8000 &
NGROK_PID=$!
sleep 3

# Get ngrok URL from local API (ngrok runs a web UI at 127.0.0.1:4040)
NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'] if d.get('tunnels') else '')" 2>/dev/null)
if [ -n "$NGROK_URL" ]; then
  CALLBACK="${NGROK_URL}/auth/callback"
  echo ""
  echo "=========================================="
  echo "ngrok URL: $NGROK_URL"
  echo "Update .env: YAHOO_REDIRECT_URI=$CALLBACK"
  echo "Update Yahoo Developer Console redirect URI to match."
  echo "=========================================="
  echo ""
  # Auto-update .env if YAHOO_REDIRECT_URI exists
  if grep -q "YAHOO_REDIRECT_URI=" .env 2>/dev/null; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|YAHOO_REDIRECT_URI=.*|YAHOO_REDIRECT_URI=$CALLBACK|" .env
    else
      sed -i "s|YAHOO_REDIRECT_URI=.*|YAHOO_REDIRECT_URI=$CALLBACK|" .env
    fi
    echo "Updated .env with new redirect URI."
  fi
else
  echo "Could not fetch ngrok URL. Update .env manually after ngrok starts."
fi

echo ""
echo "Starting uvicorn..."
# Run uvicorn from project root (so .env is found)
trap "kill $NGROK_PID 2>/dev/null" EXIT
uvicorn api.main:app --reload --port 8000
