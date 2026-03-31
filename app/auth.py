# app/auth.py
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from urllib.parse import urlsplit
from app.forms import LoginForm, RegistrationForm
from app.models import User, Pharmacy
from app import db
from datetime import datetime
from zoneinfo import ZoneInfo
import logging

# Set up logging
logger = logging.getLogger(__name__) 

eat_tz = ZoneInfo('Africa/Nairobi')

auth = Blueprint('auth', __name__)

@auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = User.query.filter_by(username=form.username.data).first()
            
            # Security: Use constant time comparison to prevent timing attacks
            if user and user.check_password(form.password.data):
                login_user(user, remember=form.remember.data)
                
                # Update last login time and IP address (if available)
                user.last_login = datetime.now(eat_tz)
                user.last_login_ip = request.remote_addr
                db.session.commit()
                
                # Redirect based on role
                if user.role == 'GLOBAL_ADMIN':
                    flash('Admin login successful!', 'success')
                    return redirect(url_for('admin.dashboard'))
                elif user.role == 'CASHIER':
                    flash('Login successful!', 'success')
                    return redirect(url_for('main.sales_processing'))  # cashiers land on the POS
                else:
                    next_page = request.args.get('next')
                    if next_page and (urlsplit(next_page).netloc or urlsplit(next_page).scheme):
                        next_page = None
                    flash('Login successful!', 'success')
                    return redirect(next_page or url_for('main.dashboard'))
            else:
                # Security: Don't reveal whether username or password was wrong
                flash('Invalid credentials. Please try again.', 'danger')
                logger.warning(f'Failed login attempt for username: {form.username.data}')
                return redirect(url_for('auth.login')) 
        except Exception as e:
            db.session.rollback()
            flash('An error occurred during login. Please try again.', 'danger')
            logger.error(f'Login error: {str(e)}')
            return redirect(url_for('auth.login'))
    return render_template('authentication/login.html', form=form)

@auth.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have successfully logged out', 'info')
    return redirect(url_for('auth.login'))

@auth.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    # Your forgot password logic here
    return render_template('authentication/forgot_password.html')

@auth.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            # Step 1: Create the pharmacy with all fields
            pharmacy = Pharmacy(
                name=form.pharmacy_name.data,
                location=form.location.data,
                monthly_target=form.monthly_target.data
            )
            db.session.add(pharmacy)
            db.session.flush()  # Get pharmacy.id before commit
            
            # Generate user code and hash password
            user_code = User.generate_code(form.username.data)
            hashed_password = generate_password_hash(form.password.data)
            
            # Step 2: Create the user
            user = User(
                full_name=form.full_name.data,
                username=form.username.data,
                phone=form.phone.data,
                email=form.email.data or None,
                user_code=user_code,
                pharmacy_id=pharmacy.id,
                role='PHARMACY_ADMIN'
            )
            user.set_password(form.password.data)  # Use the updated method

            db.session.add(user)
            db.session.commit()

            flash(f'Registration successful! You can now log in. Your ID is {user_code}', 'success')
            return redirect(url_for('auth.login'))
            
        except Exception as e:
            db.session.rollback()
            flash('An error occurred during registration. Please try again.', 'danger')
            logger.error(f'Registration error: {str(e)}')
            return redirect(url_for('auth.register'))
    return render_template('authentication/register.html', form=form)