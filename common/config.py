"""Shared settings, loaded once from .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN", "yellow-41160").strip()
CLIENT_ID = os.getenv("ZENDESK_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("ZENDESK_CLIENT_SECRET", "").strip()
# Hosting (Render sets PORT and RENDER_EXTERNAL_URL automatically)
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
HOST = os.getenv("HOST", "0.0.0.0" if os.getenv("RENDER") else "127.0.0.1")
CONCIERGE_PORT = int(os.getenv("PORT", "8100"))
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
REQUIRE_WEBHOOK_SIGNATURE = os.getenv("REQUIRE_WEBHOOK_SIGNATURE", "1" if os.getenv("RENDER") else "0") == "1"
REDIRECT_URI = (os.getenv("ZENDESK_REDIRECT_URI")
                or (f"{PUBLIC_URL}/oauth/callback" if PUBLIC_URL else "http://localhost:8100/oauth/callback")).strip()
TOKEN_FILE = Path(os.getenv("ZENDESK_TOKEN_FILE", str(ROOT / ".zendesk_token.json")))
FORCE_MOCK = os.getenv("ZENDESK_MOCK", "0").strip() == "1"
PACE = float(os.getenv("DEMO_PACE", "0.8"))

BASE_URL = f"https://{SUBDOMAIN}.zendesk.com"

PORTS = {"concierge": CONCIERGE_PORT, "triage": 8101, "order": 8102, "zendesk": 8103}
BUS_URL = f"http://127.0.0.1:{PORTS['concierge']}/bus"
