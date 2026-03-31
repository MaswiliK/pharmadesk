# app/admin.py
from __future__ import annotations  

from datetime import datetime, timedelta
from flask import Blueprint, render_template, flash, redirect, url_for, current_app, request, jsonify
from typing import Tuple, List, Dict, Any
import csv
import io
from flask_login import login_required, current_user
from .models import db, User, Pharmacy, PaymentReceipt
from .routes import admin_required
import pytz
from zoneinfo import ZoneInfo


eat_tz = ZoneInfo("Africa/Nairobi")

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

EAT_TZ = ZoneInfo('Africa/Nairobi')
VALID_STATUSES = {'pending', 'approved', 'rejected'}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _coerce_status(s: str | None) -> str:
    s = (s or '').lower().strip()
    return s if s in VALID_STATUSES else 'pending'


def _now_eat() -> datetime:
    return datetime.utcnow().replace(tzinfo=ZoneInfo('UTC')).astimezone(EAT_TZ)


def _subscription_days() -> int:
    return int(current_app.config.get('SUBSCRIPTION_DAYS', 30))


def _json_or_redirect(success: bool, message: str, redirect_endpoint: str = 'admin.reconciliation', **extra):
    """Return JSON if request expects it; else flash + redirect."""
    if request.accept_mimetypes['application/json'] or request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        payload = {'success': success, 'message': message}
        if extra:
            payload.update(extra)
        return jsonify(payload)
    # HTML fallback
    flash(message, 'success' if success else 'danger')
    return redirect(url_for(redirect_endpoint))


def _apply_user_subscription(user: User, now: datetime, sub_days: int) -> Tuple[datetime, datetime]:
    """Activate/extend subscription for *user* and return (old_due, new_due)."""
    old_due = user.next_payment_due
    if old_due is None or old_due < now:
        new_due = now + timedelta(days=sub_days)
    else:
        new_due = old_due + timedelta(days=sub_days)
    user.subscription_status = 'ACTIVE'
    user.next_payment_due = new_due
    return old_due, new_due


def _mark_receipt_status(receipt: PaymentReceipt, status: str, actor: str):
    status = _coerce_status(status)
    now = _now_eat()
    if status == 'approved':
        receipt.status = 'approved'
        receipt.approved_at = now
        receipt.approved_by = actor
    elif status == 'rejected':
        receipt.status = 'rejected'
        receipt.rejected_at = now
        receipt.rejected_by = actor
    else:
        receipt.status = 'pending'


def _parse_date(d: str | None) -> datetime | None:
    if not d:
        return None
    try:
        # date-only -> midnight local inclusive start
        dt = datetime.strptime(d, '%Y-%m-%d')
        return EAT_TZ.localize(dt)
    except ValueError:
        return None


def _parse_date_end(d: str | None) -> datetime | None:
    if not d:
        return None
    try:
        dt = datetime.strptime(d, '%Y-%m-%d')
        # inclusive through day end -> +1 day -1 microsecond (simplify: add 1 day; use < next_day)
        return EAT_TZ.localize(dt + timedelta(days=1))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# QUERY HELPERS
# ---------------------------------------------------------------------------

def _filtered_receipts_query():
    q = PaymentReceipt.query
    # filters from querystring
    start_d = request.args.get('start_date')
    end_d = request.args.get('end_date')
    status = _coerce_status(request.args.get('status')) if request.args.get('status') not in (None, 'all') else None
    user_code = request.args.get('user_code', '').strip() or None

    start_dt = _parse_date(start_d)
    end_dt_exclusive = _parse_date_end(end_d)

    if start_dt:
        q = q.filter(PaymentReceipt.created_at >= start_dt)
    if end_dt_exclusive:
        q = q.filter(PaymentReceipt.created_at < end_dt_exclusive)
    if status:
        q = q.filter(PaymentReceipt.status == status)
    if user_code:
        q = q.filter(PaymentReceipt.user_code == user_code)

    return q.order_by(PaymentReceipt.created_at.desc())


@admin_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    total_users = User.query.count()
    total_pharmacies = Pharmacy.query.count()
    recent_receipts = (PaymentReceipt.query
                       .order_by(PaymentReceipt.created_at.desc())
                       .limit(5).all())
    return render_template('admin/dashboard.html',
                           total_users=total_users,
                           total_pharmacies=total_pharmacies,
                           recent_receipts=recent_receipts)


@admin_bp.route('/users')
@login_required
@admin_required
def list_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)


@admin_bp.route('/pharmacies')
@login_required
@admin_required
def list_pharmacies():
    pharmacies = Pharmacy.query.order_by(Pharmacy.created_at.desc()).all()
    return render_template('admin/pharmacies.html', pharmacies=pharmacies)


@admin_bp.route('/reconciliation')
@login_required
@admin_required
def reconciliation():
    """Render reconciliation page OR return a partial for modal.

    Query params:
        start_date=YYYY-MM-DD
        end_date=YYYY-MM-DD
        status=pending|approved|rejected|all
        user_code=<code>
        receipt_id=<id> + partial=1 -> return HTML snippet for modal body
    """
    # If partial load for modal details
    receipt_id = request.args.get('receipt_id', type=int)
    partial = request.args.get('partial', type=int) == 1
    if partial and receipt_id is not None:
        receipt = PaymentReceipt.query.get_or_404(receipt_id)
        user = User.query.filter_by(user_code=receipt.user_code).first()
        return render_template('admin/_receipt_detail_partial.html', receipt=receipt, user=user)

    receipts = _filtered_receipts_query().all()
    return render_template('admin/reconciliation.html', receipts=receipts)


@admin_bp.route('/reconciliation/approve/<int:receipt_id>', methods=['POST'])
@login_required
@admin_required
def approve_receipt(receipt_id: int):
    receipt = PaymentReceipt.query.get_or_404(receipt_id)
    # Prevent double approval / reject
    cur_status = _coerce_status(receipt.status)
    if cur_status == 'approved':
        return _json_or_redirect(False, f'Receipt {receipt.receipt} already approved.')
    if cur_status == 'rejected':
        return _json_or_redirect(False, f'Receipt {receipt.receipt} already rejected; cannot approve.')

    user = User.query.filter_by(user_code=receipt.user_code).first()
    if not user:
        return _json_or_redirect(False, 'User not found for this receipt.')

    now = _now_eat()
    sub_days = _subscription_days()

    old_due, new_due = _apply_user_subscription(user, now, sub_days)
    _mark_receipt_status(receipt, 'approved', actor=current_user.full_name if hasattr(current_user,'full_name') else str(current_user.id))

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        current_app.logger.exception('Approve commit failed: %s', exc)
        db.session.rollback()
        return _json_or_redirect(False, 'Database error while approving receipt.')

    msg = f'Receipt {receipt.receipt} approved; {user.full_name} active until {user.next_payment_due:%Y-%m-%d}.'
    return _json_or_redirect(True, msg, updated={'receipt_id': receipt_id, 'status': 'approved', 'next_due': user.next_payment_due.isoformat()})


@admin_bp.route('/reconciliation/reject/<int:receipt_id>', methods=['POST'])
@login_required
@admin_required
def reject_receipt(receipt_id: int):
    receipt = PaymentReceipt.query.get_or_404(receipt_id)
    cur_status = _coerce_status(receipt.status)
    if cur_status == 'approved':
        return _json_or_redirect(False, f'Receipt {receipt.receipt} already approved; cannot reject.')
    if cur_status == 'rejected':
        return _json_or_redirect(False, f'Receipt {receipt.receipt} already rejected.')

    _mark_receipt_status(receipt, 'rejected', actor=current_user.full_name if hasattr(current_user,'full_name') else str(current_user.id))

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        current_app.logger.exception('Reject commit failed: %s', exc)
        db.session.rollback()
        return _json_or_redirect(False, 'Database error while rejecting receipt.')

    msg = f'Receipt {receipt.receipt} rejected.'
    return _json_or_redirect(True, msg, updated={'receipt_id': receipt_id, 'status': 'rejected'})


# ---------------------------------------------------------------------------
# BULK PROCESSING
# ---------------------------------------------------------------------------
@admin_bp.route('/reconciliation/bulk', methods=['POST'])
@login_required
@admin_required
def bulk_reconciliation():
    """Bulk approve or reject receipts.

    Accepts either JSON or multipart form-data w/ optional CSV file.
    JSON body: {"action":"approve"|"reject","receipt_ids":[1,2,...]}
    Multipart fields: bulk_action=approve|reject, csv_file=(file), receipt_ids=list? (optional)
    If CSV provided, it wins; else uses receipt_ids field(s).
    Ignores IDs that are already processed in a way that conflicts with the action.
    Returns summary counts.
    """
    action = None
    ids: List[int] = []

    if request.is_json:
        data = request.get_json(silent=True) or {}
        action = (data.get('action') or '').lower()
        ids = data.get('receipt_ids') or []
    else:
        action = (request.form.get('bulk_action') or '').lower()
        # gather explicit ids from form (can be comma-separated or multiple)
        form_ids = request.form.getlist('receipt_ids')
        if form_ids:
            for raw in form_ids:
                for piece in raw.split(','):
                    piece = piece.strip()
                    if piece.isdigit():
                        ids.append(int(piece))
        # parse CSV if uploaded
        csv_file = request.files.get('csv_file')
        if csv_file and csv_file.filename:
            try:
                # decode; assume UTF-8
                stream = io.StringIO(csv_file.stream.read().decode('utf-8'))
                reader = csv.reader(stream)
                for row in reader:
                    if not row:
                        continue
                    cell = row[0].strip()
                    if cell.isdigit():
                        ids.append(int(cell))
            except Exception as exc:  # pragma: no cover
                current_app.logger.exception('Bulk CSV parse error: %s', exc)
                return _json_or_redirect(False, 'Failed to parse CSV upload.')

    if action not in ('approve', 'reject'):
        return _json_or_redirect(False, 'Invalid or missing bulk action.')
    if not ids:
        return _json_or_redirect(False, 'No receipt IDs supplied.')

    # dedupe
    ids = sorted(set(int(i) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()))

    # gather receipts
    receipts = PaymentReceipt.query.filter(PaymentReceipt.id.in_(ids)).all()
    found_ids = {r.id for r in receipts}
    missing = [i for i in ids if i not in found_ids]

    now = _now_eat()
    sub_days = _subscription_days()

    processed = 0
    skipped_already = 0
    skipped_no_user = 0

    for r in receipts:
        cur_status = _coerce_status(r.status)
        if action == 'approve':
            if cur_status == 'approved':
                skipped_already += 1
                continue
            if cur_status == 'rejected':
                skipped_already += 1
                continue
            user = User.query.filter_by(user_code=r.user_code).first()
            if not user:
                skipped_no_user += 1
                continue
            _apply_user_subscription(user, now, sub_days)
            _mark_receipt_status(r, 'approved', actor=current_user.full_name if hasattr(current_user,'full_name') else str(current_user.id))
            processed += 1
        else:  # reject
            if cur_status == 'rejected':
                skipped_already += 1
                continue
            if cur_status == 'approved':
                skipped_already += 1
                continue
            _mark_receipt_status(r, 'rejected', actor=current_user.full_name if hasattr(current_user,'full_name') else str(current_user.id))
            processed += 1

    try:
        db.session.commit()
    except Exception as exc:  # pragma: no cover
        current_app.logger.exception('Bulk reconciliation commit failed: %s', exc)
        db.session.rollback()
        return _json_or_redirect(False, 'Database error during bulk reconciliation.')

    msg = f"Bulk {action} complete: {processed} processed, {skipped_already} skipped (already processed), {skipped_no_user} skipped (no user), {len(missing)} missing."
    payload = {
        'action': action,
        'processed': processed,
        'skipped_already': skipped_already,
        'skipped_no_user': skipped_no_user,
        'missing': missing,
    }
    return _json_or_redirect(True, msg, **payload)


# ---------------------------------------------------------------------------
# OPTIONAL: simple API to fetch a single receipt JSON (for debugging)
# ---------------------------------------------------------------------------
@admin_bp.route('/reconciliation/api/receipt/<int:receipt_id>')
@login_required
@admin_required
def reconciliation_receipt_api(receipt_id: int):
    receipt = PaymentReceipt.query.get_or_404(receipt_id)
    user = User.query.filter_by(user_code=receipt.user_code).first()
    data = {
        'id': receipt.id,
        'receipt': receipt.receipt,
        'user_code': receipt.user_code,
        'created_at': receipt.created_at.isoformat() if receipt.created_at else None,
        'status': _coerce_status(receipt.status),
        'approved_at': receipt.approved_at.isoformat() if getattr(receipt, 'approved_at', None) else None,
        'approved_by': getattr(receipt, 'approved_by', None),
        'rejected_at': receipt.rejected_at.isoformat() if getattr(receipt, 'rejected_at', None) else None,
        'rejected_by': getattr(receipt, 'rejected_by', None),
        'user': {
            'id': user.id if user else None,
            'full_name': user.full_name if user else None,
            'subscription_status': user.subscription_status if user else None,
            'next_payment_due': user.next_payment_due.isoformat() if user and user.next_payment_due else None,
        }
    }
    return jsonify(data)