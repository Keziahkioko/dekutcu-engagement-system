"""
app/services/guide_payments.py

Stage 14 step 2: what a Safaricom result MEANS for a study-guide purchase.
mpesa.py only talks to Safaricom; this decides and records. Design settled
with Keziah 2026-09-30 -- see PROJECT_LOG.md.

The rule everything follows: a payment only counts once Safaricom confirms
it through the status query. The callback (Safaricom calling our
/mpesa/callback/<secret> address) isn't signed, so anyone who learned the
address could post "paid". A callback is therefore only a TRIGGER: it must
name a purchase WE created that is still pending; what it claims is stored
separately (reported_*); then we ask Safaricom ourselves, and only its
answer settles the purchase. The receipt code is taken from the callback
only when Safaricom has confirmed the payment.

The safety net: every scheduler check, purchases still pending after 2
minutes are asked about directly -- in case a callback never arrived (a
deploy restarting the app mid-payment, a network error, Safaricom failing
to deliver). Render is kept awake by UptimeRobot, so this should be rare.
A payment confirmed this way has no receipt code on our side (the status
query doesn't return one, and an earlier callback's claim isn't adopted --
see _confirm); the member still has Safaricom's own SMS.
"""

from datetime import datetime, timezone, timedelta

from app.models.study_guide import (
    create_pending_purchase, attach_checkout, get_purchase, get_purchase_by_checkout,
    record_reported, settle_purchase, get_unsettled_purchases,
)
from app.services import mpesa

SAFETY_NET_AFTER = timedelta(minutes=2)
GIVE_UP_AFTER = timedelta(hours=24)
ACCOUNT_REFERENCE = "DeKUTCU"
DESCRIPTION = "Study guide"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)   # stored as UTC, like the rest of the app


def start_payment(member, guide, phone):
    """
    Records a pending purchase at the GUIDE's price (never an amount from a message) and sends the
    PIN prompt. Returns the purchase row. If Safaricom refuses, the purchase is settled as failed
    and MpesaError is raised -- the caller tells the member no prompt was sent.
    """
    purchase = create_pending_purchase(guide["id"], member["reg_number"], guide["price_kes"], phone, _now())
    try:
        ids = mpesa.stk_push(phone, guide["price_kes"], ACCOUNT_REFERENCE, DESCRIPTION)
    except mpesa.MpesaError as e:
        settle_purchase(purchase["id"], "failed", None, f"prompt not sent: {e}"[:200], None, _now())
        raise
    attach_checkout(purchase["id"], ids["checkout_request_id"], ids["merchant_request_id"])
    return get_purchase(purchase["id"])


def handle_callback(payload):
    """
    Safaricom's callback. Returns what happened, for logging: 'ignored' (not a pending purchase of
    ours), or the outcome of confirming it (see _confirm).
    """
    callback = (payload or {}).get("Body", {}).get("stkCallback", {})
    purchase = get_purchase_by_checkout(callback.get("CheckoutRequestID") or "")
    if not purchase or purchase["status"] != "pending":
        return "ignored"
    items = {i.get("Name"): i.get("Value") for i in callback.get("CallbackMetadata", {}).get("Item", [])}
    try:
        reported_code = int(callback.get("ResultCode"))
    except (TypeError, ValueError):
        reported_code = None
    receipt = items.get("MpesaReceiptNumber")
    record_reported(purchase["id"], reported_code, receipt)
    return _confirm(purchase, receipt if reported_code == mpesa.RESULT_PAID else None)


def _confirm(purchase, callback_receipt=None):
    """
    Asks Safaricom and settles the purchase on ITS answer. Returns the new status, or why it's still pending.

    callback_receipt: the receipt code from the callback that triggered THIS confirmation, used only if
    Safaricom confirms the payment now. A safety-net confirmation passes none and records the payment
    without a code -- it must not adopt a code from some earlier callback, which could have been forged
    while the real payment was still processing. (Remaining limit, stated honestly: a forger would need
    both our secret callback address AND Safaricom's unguessable checkout ID for a real payment.)
    """
    try:
        result = mpesa.query_status(purchase["checkout_request_id"])
    except mpesa.MpesaError:
        return "unconfirmed"      # try again on the next safety-net check
    if result is None:
        return "processing"       # the member hasn't finished yet
    code = result["result_code"]
    if code == mpesa.RESULT_PAID:
        settled = settle_purchase(purchase["id"], "paid", code, result["result_desc"], callback_receipt, _now())
    else:
        status = "cancelled" if code == mpesa.RESULT_CANCELLED else "failed"
        settled = settle_purchase(purchase["id"], status, code, result["result_desc"], None, _now())
    if settled is None:
        return "already settled"
    _on_settled(settled)
    return settled["status"]


def _on_settled(purchase):
    """
    Called exactly once per purchase, when its result is final. Step 3 fills this in (the member's
    receipt or an honest failure message; step 4 the collector's notice).
    """


def check_unsettled_purchases():
    """The safety net -- every scheduler check (safe to repeat)."""
    now = _now()
    for purchase in get_unsettled_purchases(now - SAFETY_NET_AFTER):
        if not purchase["checkout_request_id"]:
            # The app stopped between recording the purchase and Safaricom accepting the prompt --
            # nothing to ask Safaricom about, and no money can have moved.
            settle_purchase(purchase["id"], "failed", None, "prompt never confirmed as sent", None, now)
            continue
        outcome = _confirm(purchase)
        if outcome in ("unconfirmed", "processing") and now - purchase["requested_at"] > GIVE_UP_AFTER:
            settled = settle_purchase(purchase["id"], "failed", None, "no result from Safaricom after 24 hours", None, now)
            if settled:
                _on_settled(settled)
