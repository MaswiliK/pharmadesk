from datetime import timedelta, datetime
from werkzeug.security import generate_password_hash
import os
from zoneinfo import ZoneInfo
from app import create_app, db
from app.models import Pharmacy, User

app = create_app()
eat_tz = ZoneInfo("Africa/Nairobi")

def seed_admin_user():
    """Create or update the dedicated System Admin user."""
    username = os.getenv('ADMIN_USERNAME', 'admin')
    password = os.getenv('ADMIN_PASSWORD', 'admin123')
    phone    = os.getenv('ADMIN_PHONE', '0700000000')
    email    = os.getenv('ADMIN_EMAIL', 'admin@example.com')

    # Always ensure a dedicated System Admin Pharmacy exists
    admin_pharm = Pharmacy.query.filter_by(name='System Admin Pharmacy').first()
    if not admin_pharm:
        admin_pharm = Pharmacy(
            name='System Admin Pharmacy',
            location='Nairobi HQ',
            created_at=datetime.now(eat_tz)
        )
        db.session.add(admin_pharm)
        db.session.flush()
        print("ℹ️ Created System Admin Pharmacy.")

    admin = User.query.filter_by(username=username).first()
    if admin:
        admin.role = 'ADMIN'
        admin.pharmacy_id = admin_pharm.id
        if 'ADMIN_PASSWORD' in os.environ:
            admin.password = generate_password_hash(password)
    else:
        admin_code = User.generate_code(username)
        admin = User(
            user_code=admin_code,
            full_name='System Admin',
            username=username,
            password=generate_password_hash(password),
            phone=phone,
            email=email,
            pharmacy_id=admin_pharm.id,
            created_at=datetime.now(eat_tz),
            role='ADMIN',
            subscription_status='ACTIVE',
        )
        db.session.add(admin)

    db.session.commit()
    print(f"✅ Admin user ready.")

if __name__ == '__main__':
    with app.app_context():
        print("--- Starting Database Seed ---")
        print("Dropping and re-creating all tables...")
        db.drop_all()
        db.create_all()
        
        seed_admin_user()

        print("\n--- Database Seed Complete ---")
