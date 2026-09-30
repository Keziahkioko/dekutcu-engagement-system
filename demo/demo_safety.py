"""
demo/demo_safety.py

The safety lock shared by every demo script. The demo history lives in a
SEPARATE Neon branch (DEMO_DATABASE_URL in .env, never on Render), so the
real database, the live bot and the real Objective 3 data are never touched.

demo_database_url() refuses to return anything unless DEMO_DATABASE_URL is
set AND points to a different database server from DATABASE_URL -- so a
copy-paste mistake in .env can't turn a demo script loose on real data.
"""

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def demo_database_url():
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    real = os.getenv("DATABASE_URL", "").strip()
    demo = os.getenv("DEMO_DATABASE_URL", "").strip()
    if not demo:
        sys.exit("Stopped: DEMO_DATABASE_URL isn't set in .env.")
    if not real:
        sys.exit("Stopped: DATABASE_URL isn't set, so I can't confirm the demo database is a different one.")
    if urlparse(demo).hostname == urlparse(real).hostname:
        sys.exit("Stopped: DEMO_DATABASE_URL points to the same database server as the real DATABASE_URL.")
    return demo
