"""
demo/run_demo.py

Runs the reports website on this laptop against the DEMO database, for
presenting. From the project folder:

    venv\\Scripts\\python demo\\run_demo.py

What it does, in order:
  1. Swaps DATABASE_URL for DEMO_DATABASE_URL for this process only
     (after demo_safety confirms it's a different database). The app's
     own load_dotenv() never overrides a variable already set, so the
     swap holds for everything the app does.
  2. Switches background workers OFF explicitly -- no scheduler, no
     message workers, nothing that could send a WhatsApp message.
  3. Sets DEMO_MODE, which puts the "DEMO DATA" banner on every page.
  4. Prints a one-time login link for the real (non-TEST) exec leader
     in the demo copy -- Keziah -- valid 15 minutes, same as on WhatsApp.
     Run it again for a fresh link.

Nothing here runs on Render; the live bot and real database are untouched.
"""

import os
import secrets
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from demo.demo_safety import demo_database_url

os.environ["DATABASE_URL"] = demo_database_url()
os.environ["RUN_BACKGROUND_WORKERS"] = "false"
os.environ.pop("RENDER", None)
os.environ.pop("RENDER_EXTERNAL_HOSTNAME", None)
os.environ["DEMO_MODE"] = "true"

from app import create_app
from app.database import get_connection
from app.models.dashboard_login import store_code
from app.services.dashboard import LINK_LIFETIME, is_enabled

PORT = 5000

if __name__ == "__main__":
    app = create_app()
    if not is_enabled():
        sys.exit("Stopped: DASHBOARD_SECRET_KEY isn't set in .env, so the website is switched off.")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""SELECT reg_number, name FROM members
                   WHERE is_leader AND reg_number NOT LIKE 'TEST-%' ORDER BY registered_at LIMIT 1""")
    viewer = cur.fetchone()
    cur.close()
    conn.close()
    if not viewer:
        sys.exit("Stopped: no real exec leader found in the demo database to log in as.")

    code = secrets.token_urlsafe(30)
    now = datetime.now(timezone.utc)
    store_code(code, viewer["reg_number"], now.isoformat(), (now + LINK_LIFETIME).isoformat())

    print("\n" + "=" * 70)
    print(" DEMO reports website -- demo database, background jobs OFF")
    print(f" Log in as {viewer['name']} (link works once, expires in 15 minutes):")
    print(f"\n   http://localhost:{PORT}/dashboard/login/{code}\n")
    print(" Stop with Ctrl+C.")
    print("=" * 70 + "\n")
    app.run(port=PORT, debug=False)   # no debug reloader -- it would start the app twice
