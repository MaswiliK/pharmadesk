from datetime import datetime, timedelta
from flask import current_app
import pytz

EAT_TZ = pytz.timezone("Africa/Nairobi")

def to_eat_aware(dt):
    """Return a timezone-aware datetime in Africa/Nairobi."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # normalize any aware dt into EAT
        return dt.astimezone(EAT_TZ)
    return EAT_TZ.localize(dt)

def subscription_state(user, now=None):
    # normalize now
    now = to_eat_aware(now or datetime.now(EAT_TZ))

    trial_hours = current_app.config.get('TRIAL_HOURS', 72)
    grace_hours = current_app.config.get('PENDING_GRACE_HOURS', 48)

    # normalize model fields
    created_at = to_eat_aware(user.created_at) if user.created_at else None
    next_due   = to_eat_aware(user.next_payment_due) if user.next_payment_due else None

    # Trial window
    if created_at and now < (created_at + timedelta(hours=trial_hours)):
        return 'TRIAL'

    # Active subscription
    if user.subscription_status == 'ACTIVE' and next_due > now:
        return 'ACTIVE'

    # Grace / pending after receipt submission
    from .models import PaymentReceipt
    last_receipt = (
        PaymentReceipt.query
        .filter_by(user_code=user.user_code)
        .order_by(PaymentReceipt.created_at.desc())
        .first()
    )
    if last_receipt:
        last_ct = to_eat_aware(last_receipt.created_at)
        if last_ct and (now - last_ct) <= timedelta(hours=grace_hours):
            return 'PENDING'

    return 'EXPIRED'

def init_template_globals(app):
    app.jinja_env.globals['subscription_state'] = subscription_state
