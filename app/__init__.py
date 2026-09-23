"""
app/__init__.py

The app factory. This is the ONE place that builds the Flask app:
loads environment variables, registers routes (blueprints), and
initializes the database tables.

Every other file (run.py, tests) should go through create_app()
instead of building a Flask app on its own.
"""

from flask import Flask
from dotenv import load_dotenv

from app.routes.webhook import webhook_bp
from app.models.member import init_members_table
from app.models.pending_registration import init_pending_registrations_table
from app.models.pending_action import init_pending_actions_table
from app.models.pending_leader_nomination import init_pending_leader_nominations_table


def create_app():
    load_dotenv()  # loads .env before anything else needs those values

    app = Flask(__name__)

    # Register route blueprints
    app.register_blueprint(webhook_bp)

    # Make sure all tables exist before the app starts serving requests
    init_members_table()
    init_pending_registrations_table()
    init_pending_actions_table()
    init_pending_leader_nominations_table()

    return app