#!/bin/bash
# ------------------------------------------------------------------
#  Fashionocracy Agent Mesh - one-click start (macOS)
#  Double-click this file in Finder. Close the window or press Ctrl+C to stop everything.
# ------------------------------------------------------------------
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.homebrew/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
mkdir -p logs

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✔\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
fail() { printf "  \033[31m✘\033[0m %s\n" "$1"; read -r -p "Press Enter to close…"; exit 1; }

PIDS=()
cleanup() {
  echo; bold "Stopping…"
  for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
  lsof -ti tcp:8100,8101,8102,8103 2>/dev/null | xargs kill 2>/dev/null
  ok "All stopped. You can close this window."
  exit 0
}
trap cleanup INT TERM HUP

clear
bold "🛍️  Fashionocracy Agent Mesh — A2A + MCP + Zendesk"
echo

# 1. Python environment -------------------------------------------------
bold "1/5  Python environment"
command -v python3 >/dev/null || fail "python3 not found - install Python 3.10+ from python.org"
[ -d .venv ] || python3 -m venv .venv || fail "Could not create .venv"
source .venv/bin/activate
if [ ! -f .venv/.deps_ok ] || [ requirements.txt -nt .venv/.deps_ok ]; then
  pip install -q -r requirements.txt > logs/pip.log 2>&1 || fail "pip install failed - see logs/pip.log"
  touch .venv/.deps_ok
fi
ok "Dependencies ready"
[ -f .env ] || { cp .env.example .env; warn ".env created from template - add ZENDESK_CLIENT_SECRET to it"; }
grep -q '^ZENDESK_CLIENT_SECRET=paste-the-secret-here' .env && warn "ZENDESK_CLIENT_SECRET not set in .env - demo will run in MOCK mode"

# 2. Free the ports from any previous run --------------------------------
lsof -ti tcp:8100,8101,8102,8103 2>/dev/null | xargs kill 2>/dev/null
sleep 1

# 3. Agent mesh ----------------------------------------------------------
bold "2/5  Starting the agent mesh"
python run_demo.py --no-browser > logs/mesh.log 2>&1 &
PIDS+=($!)
for i in {1..30}; do curl -s localhost:8100/api/status >/dev/null && break; sleep 0.5; done
curl -s localhost:8100/api/status >/dev/null || fail "Mesh did not start - see logs/mesh.log"
ok "Concierge + 3 A2A agents running (dashboard http://localhost:8100)"

# 4. Zendesk connection --------------------------------------------------
bold "3/5  Zendesk connection"
open "http://localhost:8100"
if [ ! -f .zendesk_token.json ]; then
  warn "Not connected yet → click 'Connect Zendesk' in the dashboard and approve (waiting up to 3 min)…"
  for i in {1..180}; do [ -f .zendesk_token.json ] && break; sleep 1; done
fi
if [ -f .zendesk_token.json ]; then ok "Zendesk connected (OAuth)"; else warn "Skipped - running in MOCK mode"; fi

# 5. ngrok tunnel --------------------------------------------------------
bold "4/5  Public tunnel for Zendesk webhooks"
NGROK_URL=""
if command -v ngrok >/dev/null; then
  pkill -f "ngrok http 8100" 2>/dev/null
  ngrok http 8100 --log=stdout > logs/ngrok.log 2>&1 &
  PIDS+=($!)
  for i in {1..20}; do
    NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels | python -c "import sys,json;t=[x['public_url'] for x in json.load(sys.stdin)['tunnels'] if x['public_url'].startswith('https')];print(t[0] if t else '')" 2>/dev/null)
    [ -n "$NGROK_URL" ] && break; sleep 0.5
  done
  if [ -n "$NGROK_URL" ]; then ok "ngrok → $NGROK_URL"; else warn "ngrok did not come up - see logs/ngrok.log (run 'ngrok config add-authtoken …' once if needed)"; fi
else
  warn "ngrok not installed (brew install ngrok) - Zendesk macros won't reach the mesh; dashboard demo still works"
fi

# 6. Zendesk webhook, triggers, macros ------------------------------------
bold "5/5  Zendesk webhook, triggers & macros"
if [ -n "$NGROK_URL" ] && [ -f .zendesk_token.json ]; then
  if python setup_zendesk.py --url "$NGROK_URL" > logs/setup.log 2>&1; then
    ok "Webhook → $NGROK_URL/zendesk/webhook · 4 triggers · 4 macros"
  else
    warn "Zendesk setup failed - see logs/setup.log"
  fi
else
  warn "Skipped (needs Zendesk connection + ngrok)"
fi

echo
bold "✅ Ready."
echo "   Dashboard : http://localhost:8100"
echo "   Zendesk   : open a ticket → Apply macro → 🔎 / 📍 / 💸 / 🎁 (Agent Mesh) → Submit"
echo "   Reset mock order data: delete data/oms_state.json"
echo
echo "   Leave this window open. Press Ctrl+C (or close the window) to stop everything."
echo "   ── live mesh log ──"
tail -n 0 -f logs/mesh.log &
PIDS+=($!)
wait
