import os
from datetime import datetime
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_caching import Cache
from flask_login import LoginManager
from dotenv import load_dotenv
from flask_wtf.csrf import generate_csrf

# Initialize extensions without app 
db = SQLAlchemy()
cache = Cache()
login_manager = LoginManager()

# Load environment variables once at startup
load_dotenv()

def create_app():
    """Application factory function to create and configure the Flask app"""
    app = Flask(__name__)
    
    # Load essential configuration
    app.config.update({
        'SECRET_KEY': os.environ.get('SECRET_KEY'),
        'SQLALCHEMY_DATABASE_URI': os.environ.get("DATABASE_URL"),
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'CACHE_TYPE': 'SimpleCache',
        'REMEMBER_COOKIE_DURATION': 30 * 24 * 3600,  # 30 days in seconds
        'TRIAL_HOURS': 72,
        'PENDING_GRACE_HOURS': 48,
        'SUBSCRIPTION_DAYS': 30
    })

    # Initialize extensions with app
    db.init_app(app)
    cache.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

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
        """Convert UTC datetime to East Africa Time"""
        from pytz import timezone, utc
        if not dt.tzinfo:
            dt = utc.localize(dt)
        return dt.astimezone(timezone("Africa/Nairobi"))
    
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
        
    @app.context_processor
    def inject_csrf_token():
        return dict(csrf_token=generate_csrf)    

    return app