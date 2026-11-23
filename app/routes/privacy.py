from flask import Blueprint, render_template


privacy_bp = Blueprint("privacy", __name__)


@privacy_bp.route("/privacy")
def privacy():
    return render_template("privacy.html")
