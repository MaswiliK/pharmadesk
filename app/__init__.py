# app/__init__.py
import os
from datetime import datetime
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_caching import Cache
from flask_login import LoginManager
from dotenv import load_dotenv
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Initialize extensions without app 
db = SQLAlchemy()
cache = Cache()
login_manager = LoginManager()
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# Load environment variables once at startup
load_dotenv()

# Initialize CSRF protection
csrf = CSRFProtect()

def create_app():
    """Application factory function to create and configure the Flask app"""
    app = Flask(__name__)
    
    secret = os.environ.get('SECRET_KEY')
    if not secret:
        raise RuntimeError("SECRET_KEY is not set. Add it to your .env file.")
    
    # ── Validate required environment variables at startup ──
    _REQUIRED = [
        'SECRET_KEY',
        'DATABASE_URL',
        'MPESA_CONSUMER_KEY',
        'MPESA_CONSUMER_SECRET',
        'MPESA_TILL_NUMBER',
        'MPESA_PASSKEY',
    ]
    _missing = [v for v in _REQUIRED if not os.environ.get(v)]
    if _missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(_missing)}\n"
            "Copy .env.example to .env and fill in all values."
        )

    # ── Guard SECRET_KEY strength 
    secret = os.environ.get('SECRET_KEY')
    if len(secret) < 32:
        raise RuntimeError("SECRET_KEY is too short. Generate one with: "
                        "python -c \"import secrets; print(secrets.token_hex(32))\"")
        
    # Load essential configuration
    app.config.update({
        'SECRET_KEY': os.environ.get('SECRET_KEY'),
        'DEBUG': os.environ.get('FLASK_DEBUG', 'false').lower() == 'true',
        'SQLALCHEMY_DATABASE_URI': os.environ.get("DATABASE_URL"),
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'CACHE_TYPE': 'SimpleCache',
        'REMEMBER_COOKIE_DURATION': 30 * 24 * 3600,  # 30 days in seconds
        'TRIAL_HOURS': 72,
        'PENDING_GRACE_HOURS': 48,
        'SUBSCRIPTION_DAYS': 30,
        'WTF_CSRF_ENABLED': True,           # explicit 
        'WTF_CSRF_TIME_LIMIT': 3600,        # token expires after 1 hour
        # Rate limiter storage
        # Dev: in-memory (resets on restart, fine for local)
        # Production: swap to 'redis://localhost:6379/1' once you add Redis
        'RATELIMIT_STORAGE_URI': os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
        'RATELIMIT_HEADERS_ENABLED': True,   # sends X-RateLimit-* headers to clients
        'RATELIMIT_STRATEGY': 'fixed-window-elastic-expiry'
    })

    # Initialize extensions with app
    db.init_app(app)
    cache.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    csrf.init_app(app) 
    limiter.init_app(app)

    # Import and register template globals
    from .template_globals import init_template_globals
    from .models import User
    init_template_globals(app)

    # Register blueprints
    from .routes import main_bp
    from .auth import auth as auth_blueprint
    from .payments import pay_bp as payments_blueprint
    from .admin import admin_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_blueprint)
    app.register_blueprint(payments_blueprint, url_prefix='/payments')
    app.register_blueprint(admin_bp)
    
    # Exempt callback from limiter — it's called by Safaricom, not users
    limiter.exempt(payments_blueprint)
    
    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Template filters
    @app.template_filter('currency')
    def currency_format(value):
        try:
            num = float(value)
            return f'KES {num:,.2f}'
        except (ValueError, TypeError):
            return 'KES 0.00'
    
    @app.template_filter('utc_to_eat')
    def utc_to_eat(dt):
        from zoneinfo import ZoneInfo
        from datetime import timezone as dt_timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(ZoneInfo("Africa/Nairobi"))
    
    @app.template_filter('format_date')
    def format_date_filter(dt, fmt='%Y-%m-%d'):
        if not dt:
            return ""
        if isinstance(dt, str):
            dt = datetime.strptime(dt, '%Y-%m-%d')  
        return dt.strftime(fmt)

    @app.template_filter('time_ago')
    def time_ago_filter(dt):
        """Human-readable time difference"""
        if not dt:
            return ""
        diff = datetime.utcnow() - dt
        if diff.days > 365:
            return f"{diff.days // 365}y ago"
        if diff.days > 30:
            return f"{diff.days // 30}mo ago"
        if diff.days > 0:
            return f"{diff.days}d ago"
        if diff.seconds > 3600:
            return f"{diff.seconds // 3600}h ago"
        if diff.seconds > 60:
            return f"{diff.seconds // 60}m ago"
        return "just now"

    # Context processors
    @app.context_processor
    def inject_utilities():
        """Inject shared objects into templates"""
        from .models import AlertType, PaymentMethod
        return {
            'AlertType': AlertType,
            'PaymentMethod': PaymentMethod,
            'now': datetime.utcnow()
        }
    

    return app