"""
app/routes/mpesa.py

Stage 14: the address Safaricom calls with a payment's result --
/mpesa/callback/<MPESA_CALLBACK_SECRET>. The secret part means the address
can't be guessed; a wrong one gets 404, as if nothing were here. Even with
the right address, nothing a callback says is trusted until Safaricom
confirms it (see guide_payments.py).

Always answers Safaricom "accepted" quickly: if handling fails, the
safety-net check asks Safaricom directly a few minutes later, so nothing
is lost -- and Safaricom retrying the callback wouldn't help anyway.
"""

import hmac
import os

from flask import Blueprint, abort, jsonify, request

from app.services import guide_payments

mpesa_bp = Blueprint("mpesa", __name__, url_prefix="/mpesa")


@mpesa_bp.route("/callback/<secret>", methods=["POST"])
def callback(secret):
    expected = os.getenv("MPESA_CALLBACK_SECRET", "").strip()
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        abort(404)
    try:
        outcome = guide_payments.handle_callback(request.get_json(silent=True))
        print(f"M-Pesa callback: {outcome}")
    except Exception as e:
        print(f"M-Pesa callback handling failed (the safety net will retry): {e}")
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
