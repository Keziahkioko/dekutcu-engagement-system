"""
app/services/mpesa.py

Stage 14: the Safaricom Daraja client -- ONLY the conversation with
Safaricom, no database (guide_payments.py decides what a result means for
a purchase). Design settled with Keziah 2026-09-30 -- see PROJECT_LOG.md.

Three calls:
  - access token: proves who we are (consumer key + secret); valid ~1 hour,
    reused until just before it expires rather than fetched per payment.
  - STK push ("M-Pesa Express" / Lipa Na M-Pesa Online): asks Safaricom to
    show the member a PIN prompt for an amount, and to report the result to
    our callback address. The member enters their PIN with Safaricom; we
    never see it.
  - status query: asks Safaricom directly what happened to a push. This is
    how a payment is CONFIRMED -- callbacks aren't signed, so what arrives
    at the callback is never trusted on its own.

Settings (.env locally, Render's Environment in production):
  MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET   from the Daraja app
  MPESA_ENV                sandbox (default) | production
  MPESA_SHORTCODE          the Paybill/Till number (sandbox default 174379)
  MPESA_PASSKEY            (sandbox default: Safaricom's published test passkey)
  MPESA_TRANSACTION_TYPE   CustomerPayBillOnline (Paybill, default) | CustomerBuyGoodsOnline (Till)
  MPESA_CALLBACK_SECRET    long random value in the callback address
Going live is a change of settings, not of code.
"""

import base64
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

NAIROBI = ZoneInfo("Africa/Nairobi")
_BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
# Safaricom's PUBLISHED sandbox test values (Daraja documentation) -- not secrets.
SANDBOX_SHORTCODE = "174379"
SANDBOX_PASSKEY = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
_TIMEOUT = 30

# Result codes Safaricom reports for a push.
RESULT_PAID = 0
RESULT_CANCELLED = 1032          # the member cancelled the prompt
RESULT_DESCRIPTIONS = {
    0: "paid",
    1: "not enough money in the M-Pesa account",
    1032: "the prompt was cancelled",
    1037: "the phone couldn't be reached, or the prompt timed out",
    2001: "the M-Pesa PIN was wrong",
}
_STILL_PROCESSING = "500.001.1001"   # status-query error code: "the transaction is being processed"


class MpesaError(Exception):
    """Safaricom refused or couldn't be reached -- no prompt was sent / no answer obtained."""


def _env():
    env = os.getenv("MPESA_ENV", "sandbox").strip().lower()
    return env if env in _BASE_URLS else "sandbox"


def _shortcode():
    return os.getenv("MPESA_SHORTCODE", "").strip() or (SANDBOX_SHORTCODE if _env() == "sandbox" else "")


def _passkey():
    return os.getenv("MPESA_PASSKEY", "").strip() or (SANDBOX_PASSKEY if _env() == "sandbox" else "")


def is_configured():
    return all([os.getenv("MPESA_CONSUMER_KEY", "").strip(), os.getenv("MPESA_CONSUMER_SECRET", "").strip(),
                _shortcode(), _passkey(), os.getenv("MPESA_CALLBACK_SECRET", "").strip()])


def is_sandbox():
    return _env() == "sandbox"


def callback_url():
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    base = f"https://{host}" if host else os.getenv("DASHBOARD_BASE_URL", "http://localhost:5000").rstrip("/")
    return f"{base}/mpesa/callback/{os.getenv('MPESA_CALLBACK_SECRET', '').strip()}"


def normalise_phone(text):
    """
    A Kenyan mobile number in Safaricom's format ("2547XXXXXXXX" / "2541XXXXXXXX"),
    or None. Accepts 07.., 01.., +2547.., 2547.., with spaces or dashes.
    """
    digits = re.sub(r"[\s\-()]", "", text or "")
    digits = digits[1:] if digits.startswith("+") else digits
    if re.fullmatch(r"0[17]\d{8}", digits):
        return "254" + digits[1:]
    if re.fullmatch(r"254[17]\d{8}", digits):
        return digits
    return None


def _timestamp():
    return datetime.now(NAIROBI).strftime("%Y%m%d%H%M%S")


def password(timestamp):
    """Safaricom's required one-time password: base64(shortcode + passkey + timestamp)."""
    return base64.b64encode(f"{_shortcode()}{_passkey()}{timestamp}".encode()).decode()


_token_lock = threading.Lock()
_token = {"value": None, "expires": datetime.min.replace(tzinfo=timezone.utc)}


def _access_token():
    with _token_lock:
        now = datetime.now(timezone.utc)
        if _token["value"] and now < _token["expires"]:
            return _token["value"]
        key, secret = os.getenv("MPESA_CONSUMER_KEY", "").strip(), os.getenv("MPESA_CONSUMER_SECRET", "").strip()
        try:
            r = requests.get(f"{_BASE_URLS[_env()]}/oauth/v1/generate?grant_type=client_credentials",
                             headers={"Authorization": "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()},
                             timeout=_TIMEOUT)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise MpesaError(f"couldn't get an access token: {e}")
        if r.status_code != 200 or not body.get("access_token"):
            raise MpesaError(f"access token refused ({r.status_code})")
        _token["value"] = body["access_token"]
        # Renew a minute early so a token never expires mid-request.
        _token["expires"] = now + timedelta(seconds=int(body.get("expires_in", 3599)) - 60)
        return _token["value"]


def _post(path, payload):
    try:
        r = requests.post(f"{_BASE_URLS[_env()]}{path}", json=payload,
                          headers={"Authorization": f"Bearer {_access_token()}"}, timeout=_TIMEOUT)
        return r.status_code, r.json()
    except (requests.RequestException, ValueError) as e:
        raise MpesaError(f"Safaricom couldn't be reached: {e}")


def stk_push(phone, amount_kes, account_reference, description):
    """
    Sends the PIN prompt. Returns {"checkout_request_id", "merchant_request_id"}.
    Raises MpesaError if Safaricom didn't accept it (then no prompt was sent).
    """
    ts = _timestamp()
    status, body = _post("/mpesa/stkpush/v1/processrequest", {
        "BusinessShortCode": _shortcode(),
        "Password": password(ts),
        "Timestamp": ts,
        "TransactionType": os.getenv("MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline").strip(),
        "Amount": int(amount_kes),
        "PartyA": phone,
        "PartyB": _shortcode(),
        "PhoneNumber": phone,
        "CallBackURL": callback_url(),
        "AccountReference": account_reference[:12],
        "TransactionDesc": description[:13],
    })
    if status != 200 or str(body.get("ResponseCode")) != "0" or not body.get("CheckoutRequestID"):
        raise MpesaError(body.get("errorMessage") or body.get("ResponseDescription") or f"push refused ({status})")
    return {"checkout_request_id": body["CheckoutRequestID"], "merchant_request_id": body.get("MerchantRequestID")}


def query_status(checkout_request_id):
    """
    Asks Safaricom what happened to a push. Returns {"result_code": int, "result_desc": str},
    or None if it's still being processed. Raises MpesaError if Safaricom couldn't answer.
    """
    ts = _timestamp()
    status, body = _post("/mpesa/stkpushquery/v1/query", {
        "BusinessShortCode": _shortcode(),
        "Password": password(ts),
        "Timestamp": ts,
        "CheckoutRequestID": checkout_request_id,
    })
    if body.get("errorCode") == _STILL_PROCESSING:
        return None
    if status != 200 or "ResultCode" not in body:
        raise MpesaError(body.get("errorMessage") or f"status query failed ({status})")
    return {"result_code": int(body["ResultCode"]), "result_desc": body.get("ResultDesc", "")}


def describe(result_code):
    return RESULT_DESCRIPTIONS.get(result_code, "the payment didn't go through")
