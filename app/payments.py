import os
import time
import base64
import json
from datetime import datetime, timedelta

import requests
from requests.auth import HTTPBasicAuth
from flask import Blueprint, request, jsonify

from app import db
from app.models import User, Payment  # assuming Payment already defined
from pytz import timezone

pay_bp = Blueprint('pay_bp', __name__)

# --- Environment / Config ---
MPESA_CONSUMER_KEY    = os.getenv("MPESA_CONSUMER_KEY")
MPESA_CONSUMER_SECRET = os.getenv("MPESA_CONSUMER_SECRET")
MPESA_TILL_NUMBER     = os.getenv("MPESA_TILL_NUMBER")   # or PAYBILL
MPESA_PASSKEY         = os.getenv("MPESA_PASSKEY")
PUBLIC_BASE_URL       = os.getenv("PUBLIC_BASE_URL", "https://your-ngrok-url.io")
MPESA_ENV             = os.getenv("MPESA_ENV", "sandbox")  # 'sandbox' or 'production'

EAT_TZ = timezone('Africa/Nairobi')

def _host():
    return "sandbox.safaricom.co.ke" if MPESA_ENV == "sandbox" else "api.safaricom.co.ke"

OAUTH_URL    = f"https://{_host()}/oauth/v1/generate?grant_type=client_credentials"
STK_PUSH_URL = f"https://{_host()}/mpesa/stkpush/v1/processrequest"

# --- Access Token Cache ---
_access_token_cache = {"token": None, "expiry": 0}

def get_access_token():
    now = time.time()
    if _access_token_cache["token"] and now < _access_token_cache["expiry"] - 60:
        return _access_token_cache["token"]
    try:
        r = requests.get(OAUTH_URL,
                         auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 3600))
        _access_token_cache["token"] = token
        _access_token_cache["expiry"] = now + expires_in
        return token
    except Exception as e:
        print(f"[M-Pesa] Failed to fetch access token: {e}")
        return None

# --- Helpers ---
def normalize_msisdn(msisdn: str) -> str:
    """Return phone in 2547XXXXXXXX format or raise ValueError."""
    s = msisdn.strip().replace(" ", "")
    if s.startswith('+'):
        s = s[1:]
    if s.startswith('07') or s.startswith('01'):
        s = '254' + s[1:]
    if not (s.isdigit() and len(s) == 12 and s.startswith('254')):
        raise ValueError("Invalid phone number. Use 2547xxxxxxxx or 2541xxxxxxxx.")
    return s

def subscription_success(user: User):
    """On successful payment extend subscription by 30 days."""
    base_date = user.next_payment_due if user.next_payment_due and user.next_payment_due > datetime.utcnow() else datetime.utcnow()
    user.next_payment_due = base_date + timedelta(days=30)
    user.subscription_status = 'ACTIVE'

def mark_user_expired_if_due(user: User):
    if user.next_payment_due and user.next_payment_due < datetime.utcnow():
        user.subscription_status = 'EXPIRED'

# --- ROUTES ---

@pay_bp.route('/api/pos/initiate-payment', methods=['POST'])
def initiate_pos_payment():
    """
    Body: { amount:int, phoneNumber:str, orderId:str (user_code) }
    """
    data = request.get_json(silent=True) or {}
    amount = data.get('amount')
    phone = data.get('phoneNumber')
    user_code = data.get('orderId')  # you are using this in frontend
    if not all([amount, phone, user_code]):
        return jsonify({"error": "Missing amount, phoneNumber or orderId"}), 400
    try:
        phone_norm = normalize_msisdn(phone)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    token = get_access_token()
    if not token:
        return jsonify({"error": "Auth with provider failed"}), 502

    now_eat = datetime.now(EAT_TZ)
    timestamp = now_eat.strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{MPESA_TILL_NUMBER}{MPESA_PASSKEY}{timestamp}".encode()).decode()

    payload = {
        "BusinessShortCode": MPESA_TILL_NUMBER,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",  # If Paybill use CustomerPayBillOnline
        "Amount": amount,
        "PartyA": phone_norm,
        "PartyB": MPESA_TILL_NUMBER,
        "PhoneNumber": phone_norm,
        "CallBackURL": f"{PUBLIC_BASE_URL}/payments/api/pos/callback",
        "AccountReference": str(user_code),
        "TransactionDesc": f"Subscription {user_code}"
    }

    headers = {"Authorization": f"Bearer {token}"}

    try:
        res = requests.post(STK_PUSH_URL, json=payload, headers=headers, timeout=25)
        # Even non-200 JSON (like validation error) might come with 400; we want JSON if present.
        res_json = {}
        try:
            res_json = res.json()
        except Exception:
            pass
        if res.status_code != 200:
            return jsonify({"error": "Provider rejected request", "provider": res_json}), 502

        # Expect ResponseCode == "0" for accepted STK push prompt
        if res_json.get("ResponseCode") != "0":
            return jsonify({"error": "STK push not accepted", "provider": res_json}), 502

        merchant_request_id = res_json['MerchantRequestID']
        checkout_request_id = res_json['CheckoutRequestID']

        # Create Payment record (minimal)
        payment = Payment(
            user_code=str(user_code),
            phone_number=phone_norm,
            amount=int(amount),
            status='PENDING',
            merchant_request_id=merchant_request_id,
            checkout_request_id=checkout_request_id
        )
        db.session.add(payment)
        db.session.commit()

        # Return the IDs so frontend can poll
        return jsonify({
            "message": "STK push initiated",
            "MerchantRequestID": merchant_request_id,
            "CheckoutRequestID": checkout_request_id,
            "ResponseCode": res_json.get("ResponseCode")
        }), 200

    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Network error to M-Pesa", "detail": str(e)}), 503
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Internal server error"}), 500


@pay_bp.route('/api/pos/callback', methods=['POST'])
def pos_callback():
    """
    Comprehensive M-Pesa callback handler with:
    - Cancelled payment support
    - Missing payment record handling
    - Detailed error logging
    - Subscription management
    """
    # 1. Log raw incoming request
    print("\n=== MPESA CALLBACK RECEIVED ===")
    print("Headers:", request.headers)
    print("Raw Data:", request.data.decode('utf-8', errors='replace'))
    
    # 2. Parse JSON with multiple fallbacks
    try:
        data = request.get_json(force=True)
        if not data:
            raise ValueError("Empty JSON data")
    except Exception as e:
        print(f"[ERROR] JSON parsing failed: {str(e)}")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted (parse error)"})

    # 3. Extract callback data with defensive programming
    callback = data.get('Body', data.get('body', {}))
    stk_cb = callback.get('stkCallback', callback.get('STKCallback', {}))
    
    # 4. Essential logging
    merchant_id = stk_cb.get('MerchantRequestID')
    result_code = stk_cb.get('ResultCode', 1)  # Default to failed
    result_desc = stk_cb.get('ResultDesc', 'No description')
    
    print("Processed Callback Structure:")
    print(json.dumps({
        'MerchantRequestID': merchant_id,
        'ResultCode': result_code,
        'ResultDesc': result_desc,
        'Metadata': stk_cb.get('CallbackMetadata', {})
    }, indent=2))

    # 5. Process payment
    try:
        if not merchant_id:
            print("[ERROR] Missing MerchantRequestID")
            return jsonify({"ResultCode": 0})

        # Get or create payment record
        payment = Payment.query.filter_by(merchant_request_id=merchant_id).first()
        if not payment:
            print(f"[WARNING] Creating new payment record for {merchant_id}")
            payment = Payment(
                merchant_request_id=merchant_id,
                status='FAILED',  # Default status
                amount=0,
                phone_number='',
                user_code='UNKNOWN'
            )
            db.session.add(payment)

        # Skip processing if already finalized
        if payment.status not in ['PENDING', 'UNKNOWN']:
            print(f"[INFO] Payment {payment.id} already in {payment.status} state")
            return jsonify({"ResultCode": 0})

        # Handle all possible statuses
        if result_code == 0:  # Success
            meta_items = stk_cb.get('CallbackMetadata', {}).get('Item', [])
            metadata = {item['Name']: item.get('Value') for item in meta_items if isinstance(item, dict)}
            
            payment.status = 'SUCCESS'
            payment.mpesa_receipt_number = metadata.get('MpesaReceiptNumber')
            payment.amount = metadata.get('Amount', payment.amount)
            payment.phone_number = metadata.get('PhoneNumber', payment.phone_number)
            payment.result_desc = result_desc
            
            # Update user subscription if user_code exists
            if payment.user_code and payment.user_code != 'UNKNOWN':
                user = User.query.filter_by(user_code=payment.user_code).first()
                if user:
                    subscription_success(user)
                    print(f"Updated subscription for user: {user.user_code}")

        elif result_code == 1032:  # User cancelled
            payment.status = 'CANCELLED'
            payment.result_desc = result_desc
            print(f"Payment cancelled by user: {merchant_id}")

        else:  # Other errors
            payment.status = 'FAILED'
            payment.result_desc = f"{result_desc} (Code: {result_code})"
            print(f"Payment failed: {result_desc}")

        db.session.commit()
        print(f"[SUCCESS] Payment {payment.id} updated to {payment.status}")

    except Exception as e:
        print(f"[CRITICAL] Processing error: {str(e)}")
        db.session.rollback()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted (processing error)"})

    return jsonify({"ResultCode": 0, "ResultDesc": "Successfully processed"})

@pay_bp.route('/api/pos/status/<merchant_request_id>', methods=['GET'])
def check_payment_status(merchant_request_id):
    payment = Payment.query.filter_by(merchant_request_id=merchant_request_id).first()
    if not payment:
        return jsonify({"error": "Payment not found"}), 404
    return jsonify({
        "status": payment.status,
        "receipt": payment.mpesa_receipt_number,
        "result_desc": payment.result_desc
    })

@pay_bp.route('/debug/payments', methods=['GET'])
def debug_payments():
    payments = Payment.query.order_by(Payment.created_at.desc()).all()
    data = []
    for p in payments:
        data.append({
            "id": p.id,
            "user_code": p.user_code,
            "phone_number": p.phone_number,
            "amount": p.amount,
            "status": p.status,
            "merchant_request_id": p.merchant_request_id,
            "checkout_request_id": p.checkout_request_id,
            "mpesa_receipt_number": p.mpesa_receipt_number,
            "result_desc": p.result_desc,
            "created_at": p.created_at.strftime("%Y-%m-%d %H:%M:%S")
        })
    return jsonify(data)
