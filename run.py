"""
run.py

The single entry point for the whole project. Run this instead of
any file inside app/ directly:

    python run.py

Because app/ is now a proper Python package (has __init__.py), every
file inside it can use clean imports like `from app.database import
get_connection`, regardless of which file starts the app.
"""

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(port=5000, debug=True)
