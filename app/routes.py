# app/routes.py
from flask import Blueprint, render_template, redirect, url_for, request, flash, make_response, session, current_app, jsonify, Response, abort
from flask_login import login_required, current_user
from sqlalchemy import or_, and_, func, case, Date, desc, cast, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload, load_only
from .models import Product, Category, Sale, Expense, AlertType, Batch, PaymentReceipt
from . import db 
from app.forms import (
    ProductForm, 
    ExpenseForm,
    BatchForm,
    CategoryForm,
    SaleForm,
    PaymentForm  
)
from .enums import PaymentMethod
from datetime import datetime, timedelta, date
from functools import wraps
from collections import defaultdict
from app import cache 
import os
from zoneinfo import ZoneInfo
import io
from io import StringIO
import csv
import logging
from decimal import Decimal, ROUND_HALF_UP
from werkzeug.exceptions import NotFound, InternalServerError, HTTPException

eat_tz = ZoneInfo("Africa/Nairobi")

main_bp = Blueprint('main', __name__)

logger = logging.getLogger(__name__) 

def admin_required(f):
    """Allow only logged‑in users with role=='ADMIN'. Assumes @login_required runs before."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)  # normally unreachable if @login_required used
        if getattr(current_user, 'role', None) != 'ADMIN':
            abort(403)
        return f(*args, **kwargs)
    return wrapper

def subscription_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        
        # For admins bypass
        if getattr(current_user, 'role', None) == 'ADMIN':
            return f(*args, **kwargs)

        # Timezone handling
        eat_tz = timezone('Africa/Nairobi')
        now = datetime.now(eat_tz)  # Timezone-aware
        
        # Get created_at and ensure it's timezone-aware
        created_at = getattr(current_user, 'created_at', None)
        if created_at:
            if not created_at.tzinfo:  # If naive, localize it
                created_at = eat_tz.localize(created_at)

        # Configurable durations
        trial_hours = current_app.config.get('TRIAL_HOURS', 72)
        grace_hours = current_app.config.get('PENDING_GRACE_HOURS', 48)

        # 1. New-user trial check (now with proper timezone comparison)
        if created_at and now < (created_at + timedelta(hours=trial_hours)):
            return f(*args, **kwargs)

        # ---------------------------
        # 2. Active subscription check
        # ---------------------------
        next_due = getattr(current_user, 'next_payment_due', None)
        if (
            current_user.subscription_status == 'ACTIVE' and
            next_due and next_due > now
        ):
            return f(*args, **kwargs)

        # ---------------------------
        # 3. Grace period after receipt submission
        # ---------------------------
        from .models import PaymentReceipt  # Avoid circular imports
        last_receipt = (
            PaymentReceipt.query
            .filter_by(user_code=current_user.user_code)
            .order_by(PaymentReceipt.created_at.desc())
            .first()
        )

        if last_receipt and (now - last_receipt.created_at) <= timedelta(hours=grace_hours):
            flash("Payment receipt received. Temporary access granted during verification.", "info")
            return f(*args, **kwargs)

        # ---------------------------
        # 4. Mark expired & redirect
        # ---------------------------
        if current_user.subscription_status == 'ACTIVE' and (not next_due or next_due <= now):
            current_user.subscription_status = 'EXPIRED'
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        flash("Subscription expired. Please renew your subscription to continue.", "warning")
        return redirect(url_for('main.profile'))

    return wrapper

def handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        
        # Handle HTTP errors (like abort(404))
        except HTTPException as e:
            raise  # Let Flask's errorhandler process it
        
        # Handle database errors
        except SQLAlchemyError as e:
            db.session.rollback()
            logger.error(f"Database error in {f.__name__}: {e}")
            abort(500)  # Trigger 500.html
        
        # Handle all other exceptions
        except Exception as e:
            db.session.rollback()
            logger.exception(f"Unexpected error in {f.__name__}: {e}")
            abort(500)  # Trigger 500.html
    
    return wrapped

def db_transaction(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            result = f(*args, **kwargs)
            db.session.commit()
            return result
        except Exception as e:
            db.session.rollback()
            raise  # Re-raise for @handle_errors to catch
    return wrapped

#HOME PAGE
@main_bp.route('/')
def home():
    return render_template('homepage.html')

# DASHBOARD
@main_bp.route('/dashboard')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction         
def dashboard():
    return render_template('dashboard/main.html')

# Profile and Settings Routes
@main_bp.route('/profile', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@db_transaction
def profile():
    if request.method == 'POST':
        import re
        
        full_name = request.form.get('full_name', '').strip()
        phone     = request.form.get('phone', '').strip()
        email     = request.form.get('email', '').strip()
        
        # Length guards (match model column sizes)
        if full_name and len(full_name) > 150:
            flash('Full name must be 150 characters or fewer.', 'danger')
            return redirect(url_for('main.profile'))
        if phone and not re.fullmatch(r'^07\d{8}$', phone):
            flash('Enter a valid Safaricom number starting with 07.', 'danger')
            return redirect(url_for('main.profile'))
        if email and (len(email) > 120 or '@' not in email or '.' not in email.split('@')[-1]):
            flash('Enter a valid email address.', 'danger')
            return redirect(url_for('main.profile'))

        current_user.full_name = full_name or current_user.full_name
        current_user.phone     = phone     or current_user.phone
        current_user.email     = email     or current_user.email

        try:
            db.session.commit()
            flash('Profile updated successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating profile: {e}', 'danger')
        return redirect(url_for('main.profile'))

    # Sales progress calculation
    now_eat = datetime.now(eat_tz)
    today = now_eat.date()
    start_of_month = today.replace(day=1)

    monthly_sales = db.session.query(func.coalesce(func.sum(Sale.total_price), 0)).filter(
        Sale.pharmacy_id == current_user.pharmacy_id,
        cast(func.timezone('Africa/Nairobi', Sale.sale_time), Date) >= start_of_month
    ).scalar()

    monthly_target_value = current_user.pharmacy.monthly_target or Decimal('20000.00')
    progress_percent = 0
    if monthly_target_value > 0:
        progress_percent = int((monthly_sales / monthly_target_value) * 100)

    return render_template(
        'user_xp/profile.html',
        progress_percent=progress_percent,
        current_sales=monthly_sales,
        monthly_target=monthly_target_value
    )
    
@main_bp.route("/submit-receipt", methods=["POST"])
@login_required
def submit_receipt():
    receipt = (request.form.get("receipt") or "").strip().upper()  # Normalize input
    user_code = current_user.user_code

    if not receipt or not user_code:
        flash("Missing receipt number or user code.", "danger")
        return redirect(url_for("main.profile"))

    # Validate receipt number format
    import re
    if not re.match(r"^[A-Z0-9]{10,12}$", receipt):
        flash("Invalid M-Pesa receipt number format.", "danger")
        return redirect(url_for("main.profile"))

    # Check if receipt already exists
    existing = PaymentReceipt.query.filter_by(receipt=receipt).first()
    if existing:
        flash("This receipt number has already been submitted.", "warning")
        return redirect(url_for("main.profile"))

    try:
        # Save to database
        new_payment = PaymentReceipt(user_code=user_code, receipt=receipt)
        db.session.add(new_payment)
        db.session.commit()
        flash("Receipt submitted successfully! We'll verify and update your subscription.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("This receipt number already exists.", "warning")
    except Exception as e:
        db.session.rollback()
        flash(f"Error saving receipt: {str(e)}", "danger")

    return redirect(url_for("main.profile")) 

@main_bp.route('/update_profile', methods=['POST'])
@login_required
def update_profile():
    data = request.get_json(silent=True)
    if not data:
        return jsonify(success=False, message='Invalid request data.'), 400

    full_name = (data.get('full_name') or '').strip()
    phone = (data.get('phone') or '').strip()
    email = (data.get('email') or '').strip()

    import re

    if len(full_name) > 150:
        return jsonify(success=False, message='Full name must be 150 characters or fewer.'), 400
    if not re.fullmatch(r'^07\d{8}$', phone):
        return jsonify(success=False, message='Enter a valid Safaricom number starting with 07.'), 400
    if email and not re.fullmatch(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify(success=False, message='Invalid email address.'), 400
    if email and len(email) > 120:
        return jsonify(success=False, message='Email must be 120 characters or fewer.'), 400

    try:
        current_user.full_name = full_name
        current_user.phone = phone
        current_user.email = email
        db.session.commit()
        return jsonify(success=True, message='Profile updated successfully.')
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f'Failed to update profile: {e}'), 500

@main_bp.route('/update_pharmacy', methods=['POST'])
@login_required
def update_pharmacy():
    data = request.get_json(silent=True)
    if not data:
        return jsonify(success=False, message='Invalid request data.'), 400

    pharmacy_id = data.get('pharmacy_id')
    monthly_target = data.get('monthly_target')

    if not pharmacy_id or not monthly_target:
        return jsonify(success=False, message='Missing required fields.'), 400

    try:
        try:
            monthly_target_value = Decimal(str(monthly_target))
        except Exception:
            return jsonify(success=False, message='Monthly target must be a valid number.'), 400

        if monthly_target_value < 0 or monthly_target_value > Decimal('100000000'):
            return jsonify(success=False, message='Monthly target must be between 0 and 100,000,000.'), 400

        current_user.pharmacy.monthly_target = monthly_target_value
        db.session.commit()
        return jsonify(success=True, message='Pharmacy information updated successfully.')
    except ValueError:
        return jsonify(success=False, message='Invalid monthly target value.'), 400
    except Exception as e:
        db.session.rollback()
        return jsonify(success=False, message=f'Error updating pharmacy info: {e}'), 500
    
@main_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        # Handle settings updates
        try:
            # Update pharmacy settings
            monthly_target_raw = request.form.get('monthly_target', '').strip()
            if monthly_target_raw:
                try:
                    monthly_target_val = Decimal(monthly_target_raw)
                except Exception:
                    flash('Monthly target must be a valid number.', 'danger')
                    return redirect(url_for('main.settings'))
                if monthly_target_val < 0 or monthly_target_val > Decimal('100000000'):
                    flash('Monthly target must be between 0 and 100,000,000.', 'danger')
                    return redirect(url_for('main.settings'))
                current_user.pharmacy.monthly_target = monthly_target_val
            
            # In a real app, you would save all the toggle settings here
            # For example:
            # current_user.settings.expiry_alerts = 'expiryAlerts' in request.form
            # current_user.settings.low_stock_alerts = 'lowStockAlerts' in request.form
            
            db.session.commit()
            flash('Settings updated successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash('Error updating settings: ' + str(e), 'danger')
        
        return redirect(url_for('main.settings'))
    
    return render_template('user_xp/settings.html')

# INVENTORY SUMMARY 
@main_bp.route('/inventory')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def inventory_summary():
    from sqlalchemy import func

    today = date.today()
    cutoff_date = today + timedelta(days=365)
    expiry_threshold = today + timedelta(days=30)

    # Base queries
    q_base_prod = Product.query.filter(Product.pharmacy_id == current_user.pharmacy_id)
    q_base_batch = (
        Batch.query
        .join(Product, Batch.product_id == Product.id)
        .filter(
            Batch.pharmacy_id == current_user.pharmacy_id,
            Product.is_active.is_(True)
        )
    )

    # Product counts
    total_products = q_base_prod.with_entities(func.count(Product.id)).scalar() or 0
    active_products = (
        q_base_prod.filter(Product.is_active.is_(True))
        .with_entities(func.count(Product.id))
        .scalar() or 0
    )

    # Batch counts
    total_batches = q_base_batch.with_entities(func.count(Batch.id)).scalar() or 0
    expired_batches = (
        q_base_batch.filter(Batch.expiry_date <= today)
        .with_entities(func.count(Batch.id))
        .scalar() or 0
    )

    # Low-stock products (top 3 + count)
    low_stock_products = (
        q_base_prod
        .filter(Product.quantity <= 10)
        .order_by(Product.quantity.asc())
        .limit(3)
        .all()
    )
    low_stock_count = (
        q_base_prod.filter(Product.quantity <= 10)
        .with_entities(func.count(Product.id))
        .scalar() or 0
    )

    # Expiring soon batches (count only)
    expiring_soon_count = (
        q_base_batch
        .filter(Batch.expiry_date <= expiry_threshold)
        .with_entities(func.count(Batch.id))
        .scalar() or 0
    )

    # Expiry chart for next 6 months
    expiry_rows = (
        q_base_batch
        .filter(Batch.expiry_date >= today, Batch.expiry_date <= cutoff_date)
        .with_entities(
            func.extract('year', Batch.expiry_date).label('yyyy'),
            func.extract('month', Batch.expiry_date).label('mm'),
            func.count(Batch.id).label('ct')
        )
        .group_by('yyyy', 'mm')
        .order_by('yyyy', 'mm')
        .all()
    )

    expiry_map = {(int(r.yyyy), int(r.mm)): r.ct for r in expiry_rows}
    expiry_months, expiry_counts = [], []
    cur = date(today.year, today.month, 1)
    for _ in range(6):
        expiry_months.append(cur.strftime('%b %Y'))
        expiry_counts.append(expiry_map.get((cur.year, cur.month), 0))
        cur = date(cur.year + (cur.month // 12), (cur.month % 12) + 1, 1)

    return render_template(
        'overview/inventory.html',
        total_products=total_products,
        active_products=active_products,
        total_batches=total_batches,
        expired_batches=expired_batches,
        low_stock_products=low_stock_products,
        low_stock_count=low_stock_count,
        expiring_soon_count=expiring_soon_count,
        expiry_months=expiry_months,
        expiry_counts=expiry_counts,
        current_date=today,
        expiry_threshold=expiry_threshold,
    )

# --- PRODUCTS CRUD ---

@main_bp.route('/products')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def product_list():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    category_id = request.args.get('category', type=int)
    
    query = Product.query.filter_by(pharmacy_id=current_user.pharmacy_id)
    if search:
        query = query.filter(or_(
            Product.name.ilike(f'%{search}%'),
            Product.generic_name.ilike(f'%{search}%')
        ))
    if category_id:
        query = query.filter_by(category_id=category_id)

    products = query.order_by(Product.name.asc()).paginate(page=page, per_page=25)
    categories = Category.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Category.name).all()
    
    return render_template('products/list.html', products=products, categories=categories, search=search, selected_category=category_id)

@main_bp.route('/products/add', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def add_product():
    form = ProductForm()
    categories = Category.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Category.name).all()
    form.populate_categories(categories)

    if form.validate_on_submit():
        product = Product(
            pharmacy_id=current_user.pharmacy_id,
            name=form.name.data,
            generic_name=form.generic_name.data,
            dosage=form.dosage.data,
            reorder_level=form.reorder_level.data,
            cost_price=form.cost_price.data,
            selling_price=form.selling_price.data,
            max_discount=form.max_discount.data,
            category_id=form.category_id.data,
            is_active=form.is_active.data,
            prescription_required=form.prescription_required.data,
        )
        db.session.add(product)
        db.session.commit()
        flash('Product created successfully.', 'success')
        return redirect(url_for('main.product_list'))

    return render_template('products/form.html', form=form)


@main_bp.route('/products/edit/<int:id>', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def edit_product(id):
    product = Product.query.filter_by(id=id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    form = ProductForm(obj=product)
    categories = Category.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Category.name).all()
    form.populate_categories(categories)

    if form.validate_on_submit():
        form.populate_obj(product)
        db.session.commit()
        flash('Product updated successfully.', 'success')
        return redirect(url_for('main.product_list'))

    return render_template('products/form.html', form=form, product=product)


@main_bp.route('/products/delete/<int:id>', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def delete_product(id):
    product = Product.query.filter_by(id=id, pharmacy_id=current_user.pharmacy_id).first_or_404()
     # Check if there are any sales associated with this product
    if product.sales.first(): 
        flash('Cannot delete product: It has associated sales records. Consider deactivating it instead.', 'danger')
        return redirect(url_for('main.product_list'))
    db.session.delete(product)
    db.session.commit()
    flash('Product deleted.', 'success')
    return redirect(url_for('main.product_list'))

# --- BATCHES CRUD ---

@main_bp.route('/batches')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def batch_list():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    current_date = date.today()

    query = Batch.query.filter_by(pharmacy_id=current_user.pharmacy_id).join(Product)  # Join with Product for filtering by name
    if search:
        query = query.filter(or_(
            Product.name.ilike(f'%{search}%'),
            Product.generic_name.ilike(f'%{search}%')
        ))

    batches = query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Batch.expiry_date.asc()).paginate(page=page, per_page=25)

    return render_template(
        'batches/list.html',
        batches=batches,
        search=search,
        current_date=current_date
    )

@main_bp.route('/batches/add', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def add_batch():
    form = BatchForm(pharmacy_id=current_user.pharmacy_id)
    form.populate_products(Product.query.filter_by(is_active=True, pharmacy_id=current_user.pharmacy_id))  # Populate products
    if form.validate_on_submit():
        batch = Batch(
            pharmacy_id=current_user.pharmacy_id,
            product_id=form.product_id.data,  # Assuming form has a product selector
            batch_number=form.batch_number.data,
            manufacture_date=form.manufacture_date.data,
            expiry_date=form.expiry_date.data,
            order_quantity=form.order_quantity.data,
            stock_lvl=form.order_quantity.data,  # Initial stock level set to order quantity
            supplier=form.supplier.data,
            supplier_contact=form.supplier_contact.data,
        )
        db.session.add(batch)
        db.session.commit()
        flash('Batch added successfully.', 'success')
        return redirect(url_for('main.batch_list'))

    return render_template('batches/form.html', form=form)

@main_bp.route('/batches/edit/<int:batch_id>', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def edit_batch(batch_id):
    batch = Batch.query.filter_by(id=batch_id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    form = BatchForm(pharmacy_id=current_user.pharmacy_id,obj=batch)
    form.populate_products(Product.query.filter_by(is_active=True, pharmacy_id=current_user.pharmacy_id))  # Populate products
    if form.validate_on_submit():
        form.populate_obj(batch)
        batch.stock_lvl = form.order_quantity.data
        db.session.commit()
        flash('Batch updated.', 'success')
        return redirect(url_for('main.batch_list'))

    return render_template('batches/form.html', form=form, batch=batch)

@main_bp.route('/batches/delete/<int:batch_id>', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def delete_batch(batch_id):
    batch = Batch.query.filter_by(id=batch_id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    db.session.delete(batch)
    db.session.commit()
    flash('Batch deleted.', 'success')
    return redirect(url_for('main.batch_list'))

@main_bp.route('/categories')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def category_list():
    page = request.args.get('page', 1, type=int)
    categories = Category.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Category.name).paginate(page=page, per_page=25)
    return render_template('categories/list.html', categories=categories)

@main_bp.route('/categories/add', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def add_category():
    form = CategoryForm(pharmacy_id=current_user.pharmacy_id)
    if form.validate_on_submit():
        category = Category(
            pharmacy_id=current_user.pharmacy_id,
            name=form.name.data,
            description=form.description.data
        )
        db.session.add(category)
        db.session.commit()
        flash('Category created successfully', 'success')
        return redirect(url_for('main.category_list'))
    return render_template('categories/form.html', form=form)

@main_bp.route('/categories/edit/<int:id>', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def edit_category(id):
    category = Category.query.filter_by(id=id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    form = CategoryForm(pharmacy_id=current_user.pharmacy_id, obj=category)
    if form.validate_on_submit():
        form.populate_obj(category)
        db.session.commit()
        flash('Category updated successfully', 'success')
        return redirect(url_for('main.category_list'))
    return render_template('categories/form.html', form=form)

@main_bp.route('/categories/delete/<int:id>', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def delete_category(id):
    category = Category.query.filter_by(id=id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    if category.products:
        flash('Cannot delete category with associated products', 'danger')
    else:
        db.session.delete(category)
        db.session.commit()
        flash('Category deleted successfully', 'success')
    return redirect(url_for('main.category_list'))

@main_bp.route('/api/search_products')
@handle_errors
@login_required
@subscription_required
@db_transaction
def search_products():
    query = request.args.get('q', '').strip()
    
    # Validate query length
    if len(query) < 2:
        return jsonify({"error": "Query must be at least 2 characters"}), 400

    # Split query for multi-term search
    search_terms = [f"%{term}%" for term in query.split()]
    
    # Build conditions dynamically
    conditions = []
    for term in search_terms:
        term_condition = or_(
            Product.name.ilike(term),
            Product.dosage.ilike(term)
        )
        conditions.append(term_condition)
    
    # Execute query
    results = Product.query.filter(
        Product.pharmacy_id == current_user.pharmacy_id,
        and_(*conditions)
    ).limit(10).all()

    return jsonify({
        "results": [{
            "id": prod.id,
            "name": prod.name,
            "dosage": prod.dosage
        } for prod in results]
    })
    
@main_bp.route('/cart/partial')
def partial_cart():
    cart = session.get('cart', [])
    total = sum(item['total_price'] for item in cart)
    requires_prescription = any(item.get('requires_prescription') for item in cart)
    return render_template('sales/_cart_partial.html', 
                          current_sale_items=cart,
                          total_amount=total,
                          requires_prescription=requires_prescription)

@main_bp.route('/cart/total')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def cart_total():
    cart = session.get('cart', [])
    total_amount = sum(item.get('total_price', 0) for item in cart)
    return jsonify({"total": f"{total_amount:,.2f}"})  
    
@main_bp.route('/api/add_to_cart', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def api_add_to_cart():
    form = SaleForm(pharmacy_id=current_user.pharmacy_id)

    if form.validate_on_submit():
        product = Product.query.filter_by(id=form.product.data, pharmacy_id=current_user.pharmacy_id).first()
        if not product or not product.is_active:
            return jsonify({"status": "error", "message": "Product unavailable"}), 400

        try:
            quantity = int(form.quantity.data)
            unit_price = float(product.selling_price)
            total_price = round(quantity * unit_price, 2)
        except (ValueError, TypeError):
            return jsonify({"status": "error", "message": "Invalid quantity"}), 400

        # Setup cart
        if 'cart' not in session:
            session['cart'] = []
        cart = session['cart']

        # Check if item already exists in cart
        existing_item = next((item for item in cart if item['product_id'] == product.id), None)

        # Calculate total quantity already in cart
        quantity_in_cart = existing_item['quantity'] if existing_item else 0

        # Check inventory against total intended quantity
        available_stock = product.quantity - quantity_in_cart

        # Defensive: Ensure available_stock is not negative
        if available_stock < 0:
            available_stock = 0

        if quantity > available_stock:
            if available_stock == 0:
                return jsonify({
                    "status": "error",
                    "message": f"'{product.name}' is out of stock."
                }), 400
            else:
                return jsonify({
                    "status": "error",
                    "message": f"Only {available_stock} unit{'s' if available_stock != 1 else ''} available after current cart items."
                }), 400

        # Update cart
        if existing_item:
            existing_item['quantity'] += quantity
            existing_item['total_price'] = round(existing_item['quantity'] * unit_price, 2)
        else:
            cart.append({
                'product_id': product.id,
                'product_name': f"{product.name} ({product.dosage})",
                'quantity': quantity,
                'unit_price': unit_price,
                'total_price': total_price,
                'requires_prescription': product.prescription_required
            })

        session.modified = True
        return jsonify({"status": "success", "message": f"'{product.name}' added to cart."}), 200

    # Validation errors
    errors = [msg for msgs in form.errors.values() for msg in msgs]
    return jsonify({"status": "error", "errors": errors}), 400

@main_bp.route('/sales', methods=['GET', 'POST'])
@handle_errors
@login_required
@subscription_required
@db_transaction
def sales_processing():
    # initialize forms & cart
    sale_form    = SaleForm(pharmacy_id=current_user.pharmacy_id)
    cart         = session.setdefault('cart', [])

    # normalize cart data
    for item in cart:
        for key in ['quantity', 'unit_price', 'total_price']:
            if isinstance(item.get(key), str):
                try:
                    item[key] = float(item[key])
                except (ValueError, TypeError):
                    item[key] = 0.0
        session.modified = True

    # compute original total
    original_total = sum(
    (Decimal(str(item.get('total_price', 0))) for item in cart),
    start=Decimal('0.00')
    ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    # bind the editable Amount Due field
    payment_form = PaymentForm(amount_due=original_total)

    if request.method == 'POST' and 'process_payment' in request.form:
        # populate from POST and validate
        if payment_form.validate_on_submit():
            return handle_payment(payment_form, cart, original_total)
        # else fall through to re‑render with errors

    recent_transactions   = (
        Sale.query
            .filter_by(pharmacy_id=current_user.pharmacy_id)
            .order_by(Sale.sale_time.desc())
            .limit(3)
            .all()
    )
    requires_prescription = any(item.get('requires_prescription') for item in cart)

    return render_template(
        'sales/record.html',
        sale_form=sale_form,
        payment_form=payment_form,
        current_sale_items=cart,
        total_amount=original_total,
        requires_prescription=requires_prescription,
        recent_transactions=recent_transactions
    )

@main_bp.route('/record_sale', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def record_sale():
    """Handle adding items to the shopping cart"""
    form = SaleForm(pharmacy_id=current_user.pharmacy_id)
    form.product.choices = [(p.id, p.name) for p in Product.query.all()]
    
    if form.validate_on_submit():
        product = Product.query.get(form.product.data)
        if not product:
            flash('Product not found', 'danger')
            return redirect(url_for('main.sales_processing'))
            
        # Check available stock considering existing cart items
        cart = session.get('cart', [])
        cart_quantity = sum(item['quantity'] for item in cart if item['product_id'] == product.id)
        requested_quantity = form.quantity.data
        
        if product.quantity < (cart_quantity + requested_quantity):
            flash(f'Only {product.quantity - cart_quantity} available in stock', 'danger')
            return redirect(url_for('main.sales_processing'))
        
        # Add to cart
        cart_item = {
            'product_id': product.id,
            'product_name': product.name,
            'quantity': requested_quantity,
            'unit_price': float(product.selling_price),
            'total_price': float(product.selling_price * requested_quantity),
            'requires_prescription': product.requires_prescription
        }
        
        session.setdefault('cart', []).append(cart_item)
        session.modified = True
        flash('Item added to cart', 'success')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    
    return redirect(url_for('main.sales_processing')) 

def get_next_transaction_id():
    last_sale = Sale.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Sale.id.desc()).first()
    if not last_sale or not last_sale.transaction_id:
        return "TID1"
    
    # Extract number from last transaction_id, e.g. "TID23" -> 23
    import re
    match = re.search(r'TID(\d+)', last_sale.transaction_id)
    if match:
        last_num = int(match.group(1))
        next_num = last_num + 1
    else:
        next_num = 1

    return f"TID{next_num}"
   
    
@main_bp.route('/process_payment', methods=['POST'])
@handle_errors
@login_required
@subscription_required
@db_transaction
def handle_payment(form, cart, original_total):
    """Process payment with inventory locking and proration support"""
    # Validate cart exists
    if not cart:
        flash('Cart is empty', 'danger')
        return redirect(url_for('main.sales_processing'))
    
    # Check prescription requirements
    requires_rx = any(item.get('requires_prescription') for item in cart)
    customer = form.customer_name.data.strip() if form.customer_name.data else None
    if requires_rx and not customer:
        flash('Customer name required for prescription items', 'danger')
        return redirect(url_for('main.sales_processing'))
    
    # Validate price adjustment
    adjusted_total = form.amount_due.data
    if adjusted_total > original_total:
        flash('Adjusted amount cannot exceed original total', 'danger')
        return redirect(url_for('main.sales_processing'))
    
    # Pre-process payment method
    payment_method = form.payment_method.data.upper().replace('-', '')
    transaction_id = get_next_transaction_id()
    now_eat = datetime.now(eat_tz)
    pharmacy_id = int(current_user.pharmacy_id)
    
    # Calculate proration factor safely
    factor = (adjusted_total / original_total if original_total != Decimal('0.00') 
              else Decimal('1.00'))
    
    try:
        with db.session.begin_nested():
            # Process each cart item with batch locking
            for item in cart:
                # Lock batches FIFO with stock availability check
                batches = db.session.scalars(
                    select(Batch)
                    .filter_by(product_id=item['product_id'])
                    .with_for_update()  # Critical: Lock batches
                    .order_by(Batch.expiry_date)
                ).all()
                
                # Verify sufficient stock
                total_stock = sum(batch.stock_lvl for batch in batches)
                if total_stock < item['quantity']:
                    product_name = Product.query.get(item['product_id']).name
                    raise ValueError(f'Insufficient stock for {product_name} (needed: {item["quantity"]}, available: {total_stock})')
                
                # Deduct inventory FIFO
                qty_to_deduct = item['quantity']
                for batch in batches:
                    if qty_to_deduct <= 0:
                        break
                    
                    deduct_qty = min(qty_to_deduct, batch.stock_lvl)
                    batch.stock_lvl -= deduct_qty
                    qty_to_deduct -= deduct_qty
                
                # Calculate prorated price
                line_total = Decimal(str(item['total_price']))
                prorated_total = (line_total * factor).quantize(
                    Decimal('0.01'), rounding=ROUND_HALF_UP
                )
                
                # Record sale with batch reference
                sale = Sale(
                    product_id=item['product_id'],
                    batch_id=batch.id,  # Last batch used
                    quantity=item['quantity'],
                    unit_price=item['unit_price'],
                    total_price=prorated_total,
                    payment_method=payment_method,  # Pre-processed value
                    customer_name=customer,
                    sale_time=now_eat,
                    transaction_id=transaction_id,
                    pharmacy_id=pharmacy_id,
                    # Track processing user user_id=user_id  
                )
                db.session.add(sale)
            
            # Finalize transaction
            db.session.commit()
            session.pop('cart', None)
            flash(
                f'Payment processed: Original {original_total:.2f} → '
                f'Adjusted {adjusted_total:.2f} (ID: {transaction_id})',
                'success'
            )
    
    except ValueError as e:
        db.session.rollback()
        flash(str(e), 'danger')
        current_app.logger.error(f'Inventory error: {e}')
    
    except Exception as e:
        db.session.rollback()
        flash('Payment processing failed. Please try again.', 'danger')
        current_app.logger.exception(f'Payment error: {e}')
    
    return redirect(url_for('main.sales_processing'))


@main_bp.route('/remove_from_cart/<int:product_id>', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def remove_from_cart(product_id):
    cart = session.get('cart', [])
    updated_cart = [item for item in cart if item['product_id'] != product_id]

    if len(updated_cart) != len(cart):
        session['cart'] = updated_cart
        session.modified = True
        return jsonify({"status": "success", "message": "Item removed from cart"}), 200

    return jsonify({"status": "error", "message": "Item not found in cart"}), 404

@main_bp.route('/cart/clear', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def clear_cart():
    session['cart'] = []
    session.modified = True
    return jsonify({"status": "success", "message": "Cart cleared."}), 200

    
@main_bp.route('/sales/history')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def sales_history():
    """Display paginated sales history with filtering options"""
    page = request.args.get('page', 1, type=int)
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    product_id = request.args.get('product_id', type=int)
    
    # Base query
    query = Sale.query.filter_by(pharmacy_id=current_user.pharmacy_id).join(Product)
    
    # Date filtering
    if start_date:
        try:
            start_date = datetime.strptime(start_date, '%Y-%m-%d')
            query = query.filter(Sale.sale_time >= start_date)
        except ValueError:
            flash('Invalid start date format. Use YYYY-MM-DD.', 'warning')
            start_date = None
    
    if end_date:
        try:
            end_date = datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1)
            query = query.filter(Sale.sale_time <= end_date)
        except ValueError:
            flash('Invalid end date format. Use YYYY-MM-DD.', 'warning')
            end_date = None
    
    # Product filtering
    if product_id:
        query = query.filter(Sale.product_id == product_id)
    
    # Calculate total sales amount
    sales_total = db.session.query(func.sum(Sale.total_price)).filter(Sale.pharmacy_id == current_user.pharmacy_id).scalar() or 0
    
    # Get paginated results
    sales = query.order_by(Sale.sale_time.desc()).paginate(
        page=page, 
        per_page=25,
        error_out=False
    )
    
    # Get products for filter dropdown
    products = Product.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Product.name).all()
    
    selected_product_name = ''
    if product_id:
        product = Product.query.get(product_id)
        if product:
            selected_product_name = f"{product.name} ({product.dosage})"

    
    return render_template('sales/history.html',
                         sales=sales,
                         products=products,
                         sales_total=sales_total,
                         start_date=start_date.strftime('%Y-%m-%d') if start_date else '',
                         end_date=end_date.strftime('%Y-%m-%d') if end_date else '',
                         selected_product=product_id,
                         selected_product_name=selected_product_name) 
       
# New logic for view_receipt
@main_bp.route('/sales/<string:transaction_id>/receipt') # Change URL parameter to string:transaction_id
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def view_receipt(transaction_id): # Function parameter
    """
    View HTML version of the receipt.
    Retrieves all sales belonging to a specific transaction_id.
    """
    # Find all sales in this transaction
    group_sales = Sale.query.filter(
        Sale.pharmacy_id == current_user.pharmacy_id,
        Sale.transaction_id == transaction_id # Filter by the new transaction_id
    ).order_by(Sale.sale_time, Sale.id).all() # Order for consistent display

    if not group_sales:
        flash('Receipt not found or you do not have permission to view it.', 'danger')
        return redirect(url_for('main.sales_history'))

    # Get details from any sale in the group (e.g., the first one)
    # as they all share transaction details like time, customer, payment method
    first_sale_in_group = group_sales[0]

    # Calculate group totals
    total_transaction_price = sum(s.total_price for s in group_sales)
    total_transaction_quantity = sum(s.quantity for s in group_sales)

    # Prepare data for template
    return render_template(
        'sales/receipt.html',
        sales_items=group_sales, # List of individual Sale objects for iteration
        total=total_transaction_price,
        quantity=total_transaction_quantity,
        transaction_datetime=first_sale_in_group.sale_time, # Use datetime from first item
        customer_name=first_sale_in_group.customer_name or 'Walk-in Customer',
        payment_method_value=first_sale_in_group.payment_method.value, # Correctly get enum value
        transaction_id_display=transaction_id, # The transaction ID itself
        now=datetime.now(eat_tz)
    )
        
@main_bp.route('/expenses')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def expenses():
    # Get recent expenses (last 3)
    recent_expenses = Expense.query.filter_by(pharmacy_id=current_user.pharmacy_id).order_by(Expense.date.desc()).limit(3).all()
    
    # Get data for chart - group by category and sum amounts
    expense_data = db.session.query(
        Expense.category,
        func.sum(Expense.amount).label('total_amount')
    ).filter(
        Expense.pharmacy_id == current_user.pharmacy_id # Filter by pharmacy_id
    ).group_by(Expense.category).all()
    
    # Prepare data for Chart.js
    categories = [data[0] for data in expense_data]
    amounts = [float(data[1]) for data in expense_data]
    
    # Handle empty categories and amounts   
    if not categories:
        categories = ['No data']
        amounts = [0]
    
    return render_template('expenses/list.html',
                         recent_expenses=recent_expenses,
                         categories=categories,
                         amounts=amounts)

from flask import request

@main_bp.route('/expenses/manage')
@handle_errors
@login_required
@subscription_required
@db_transaction
def manage_expenses_list():
    # Pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = 10  # You can adjust this number as needed
    
    # Get paginated expenses
    expenses_pagination = Expense.query.filter_by(
        pharmacy_id=current_user.pharmacy_id
    ).order_by(
        Expense.date.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('expenses/manage.html', expenses=expenses_pagination)

@main_bp.route('/expenses/add', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def add_expense():
    form = ExpenseForm()
    
    if form.validate_on_submit():
        try:
            # Handle date conversion
            date_value = form.date.data or datetime.today().date()
            if isinstance(date_value, str):
                date_value = datetime.strptime(date_value, '%Y-%m-%d').date()
            
            # Create new expense
            expense = Expense(
                pharmacy_id=current_user.pharmacy_id,
                date=date_value,
                category=form.category.data,
                description=form.description.data,
                amount=form.amount.data
            )
            
            db.session.add(expense)
            db.session.commit()
            flash('Expense added successfully!', 'success')
            return redirect(url_for('main.expenses'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding expense: {str(e)}', 'danger')
    
    # For GET request or if there's an error
    return render_template('expenses/form.html', form=form)

@main_bp.route('/expenses/edit/<int:id>', methods=['GET', 'POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def edit_expense(id):
    expense = Expense.query.filter_by(id=id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    form = ExpenseForm(obj=expense)
    
    if form.validate_on_submit():
        try:
            # Handle date conversion
            date_value = form.date.data or datetime.today(eat_tz).date()
            if isinstance(date_value, str):
                date_value = datetime.strptime(date_value, '%Y-%m-%d').date()
            
            # Update expense
            expense.date = date_value
            expense.category = form.category.data
            expense.description = form.description.data
            expense.amount = form.amount.data
            
            db.session.commit()
            flash('Expense updated successfully!', 'success')
            return redirect(url_for('main.manage_expenses_list'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating expense: {str(e)}', 'danger')
    
    return render_template('expenses/form.html', form=form)

@main_bp.route('/expenses/delete/<int:id>', methods=['POST'])
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def delete_expense(id):
    expense = Expense.query.filter_by(id=id, pharmacy_id=current_user.pharmacy_id).first_or_404()
    try:
        db.session.delete(expense)
        db.session.commit()
        flash('Expense deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting expense: {str(e)}', 'danger')
    
    return redirect(url_for('main.manage_expenses_list'))

@main_bp.route('/reports')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def analytics_dashboard():
    """Render the main analytics dashboard"""

    now_eat = datetime.now(eat_tz)
    today = now_eat.date()
    start_of_month = today.replace(day=1)
    last_7_days = today - timedelta(days=6)
    
    start_datetime = eat_tz.localize(datetime.combine(last_7_days, datetime.min.time()))
    end_datetime = eat_tz.localize(datetime.combine(today, datetime.max.time()))
    
    # --- 7-day sales chart (PostgreSQL-only)
    sales_data = Sale.get_daily_sales(
        pharmacy_id=current_user.pharmacy_id,
        start_date=start_datetime,
        end_date=end_datetime
    ).all()

    chart_labels = [r.date.strftime('%a') for r in sales_data]  # e.g., ['Thu', 'Fri', ..., 'Wed']
    chart_values = [float(r.total or 0) for r in sales_data]
    
    # --- 7-day Cash and M-PESA sales
    def get_payment_data(method):
        return db.session.query(
            cast(func.timezone('Africa/Nairobi', Sale.sale_time), Date).label('day'),
            func.sum(Sale.total_price).label('total')
        ).filter(
            Sale.pharmacy_id == current_user.pharmacy_id,
            Sale.sale_time >= start_datetime,
            Sale.payment_method == method,
            Sale.sale_time >= start_datetime,
            Sale.sale_time <= end_datetime
        ).group_by('day').order_by('day').all()

    cash_data = get_payment_data('CASH')
    mpesa_data = get_payment_data('MPESA')

    def to_dict(data):
        return {r.day.strftime('%a'): float(r.total or 0) for r in data}

    cash_dict = to_dict(cash_data)
    mpesa_dict = to_dict(mpesa_data)

    chart_cash = [cash_dict.get(day, 0) for day in chart_labels]
    chart_mpesa = [mpesa_dict.get(day, 0) for day in chart_labels]
    
    # --- Today's sales (PostgreSQL)
    todays_sales = db.session.query(
        func.coalesce(func.sum(Sale.total_price), 0)
    ).filter(
        Sale.pharmacy_id == current_user.pharmacy_id,
        cast(func.timezone('Africa/Nairobi', Sale.sale_time), Date) == today
    ).scalar()

    # --- Monthly sales (PostgreSQL)
    monthly_sales = db.session.query(
        func.coalesce(func.sum(Sale.total_price), 0)
    ).filter(
        Sale.pharmacy_id == current_user.pharmacy_id,
        cast(func.timezone('Africa/Nairobi', Sale.sale_time), Date) >= start_of_month
    ).scalar()

    # --- Top performing products
    product_performance = db.session.query(
        Product.name,
        func.sum(Sale.quantity).label('units_sold'),
        func.sum(Sale.total_price).label('revenue'),
        func.avg(Sale.total_price / Sale.quantity).label('avg_price')
    ).join(Sale).filter(
        Sale.pharmacy_id == current_user.pharmacy_id,
        Sale.sale_time >= start_datetime,  
    ).group_by(Product.id).order_by(desc('units_sold')).limit(5).all()

    # --- Sales target logic
    monthly_target_value = current_user.pharmacy.monthly_target or Decimal('20000.00')
    target_percent = (monthly_sales / monthly_target_value * 100) if monthly_target_value > 0 else 0

    return render_template('reports/main.html',
        chart_labels=chart_labels,
        chart_values=chart_values,
        chart_cash=chart_cash,
        chart_mpesa=chart_mpesa,
        todays_sales=f"Ksh {todays_sales:,.0f}",
        target_percent=int(target_percent),
        product_performance=product_performance,
        monthly_target=monthly_target_value
    )

def get_product_performance(pharmacy_id,start=None, end=None):
    """Return product performance data, optionally filtered by date range."""
    query = db.session.query(
        Product.name,
        func.sum(Sale.quantity).label('units_sold'),
        func.sum(Sale.total_price).label('revenue'),
        func.avg(Sale.total_price / Sale.quantity).label('avg_price'),
        func.min(Sale.sale_time).label('first_sale'),
        func.max(Sale.sale_time).label('last_sale')
    ).join(Sale, Product.id == Sale.product_id).filter(
        Sale.pharmacy_id == pharmacy_id
    )

    if start:
        query = query.filter(Sale.sale_time >= start)
    if end:
        query = query.filter(Sale.sale_time <= end)

    query = query.group_by(Product.id).order_by(desc('units_sold'))

    # Return a list of objects with attributes for easier access in the CSV export
    class ProductPerf:
        def __init__(self, name, units_sold, revenue, avg_price, first_sale=None, last_sale=None):
            self.name = name
            self.units_sold = units_sold or 0  # Handle potential None values
            self.revenue = revenue or 0
            self.avg_price = avg_price or 0
            self.first_sale = first_sale  # Optional fields
            self.last_sale = last_sale

    return [ProductPerf(*row) for row in query.all()]
    
@main_bp.route('/reports/export')
@handle_errors          
@login_required         
@subscription_required  
@db_transaction
def export_sales_csv():
    start_date_str = request.args.get('start') # Renamed to avoid confusion with datetime object
    end_date_str = request.args.get('end')     # Renamed

    start_dt = None
    end_dt = None

    # Parse and validate dates from request args
    try:
        if start_date_str:
            start_dt = datetime.strptime(start_date_str, '%Y-%m-%d')
        if end_date_str:
            end_dt = datetime.strptime(end_date_str, '%Y-%m-%d')
    except ValueError:
        # If any date parsing fails, reset both to None to trigger default
        start_dt = None
        end_dt = None
        flash('Invalid date format. Using default last 7 days.', 'warning') # Optional: inform user

    # Apply timezone and set time to min/max for the day
    # Fallback to default last 7 days if no valid dates were provided
    if not start_dt and not end_dt:
        today = datetime.now(eat_tz).date()
        last_7_days = today - timedelta(days=6)
        start = eat_tz.localize(datetime.combine(last_7_days, datetime.min.time()))
        end = eat_tz.localize(datetime.combine(today, datetime.max.time()))
    else:
        # If at least one date was provided, ensure both are set and timezone aware
        # and represent the full day range.
        if start_dt:
            start = eat_tz.localize(datetime.combine(start_dt.date(), datetime.min.time()))
        else: # If start_dt was None but end_dt was provided, set start to beginning of time
            start = eat_tz.localize(datetime(1, 1, 1, 0, 0, 0)) # Or a very early date

        if end_dt:
            end = eat_tz.localize(datetime.combine(end_dt.date(), datetime.max.time()))
        else: # If end_dt was None but start_dt was provided, set end to current time
            end = datetime.now(eat_tz) # Or a very late date / end of day today

    # Get the product performance data, apply date filters if needed
    product_data = get_product_performance(current_user.pharmacy_id, start, end)
    
    # --- Calculate the overall total sales for the filtered period ---
    total_filtered_sales = db.session.query(
        func.coalesce(func.sum(Sale.total_price), 0)
    ).filter(
        Sale.pharmacy_id == current_user.pharmacy_id,
        Sale.sale_time >= start,
        Sale.sale_time <= end
    ).scalar()

    # Create CSV in memory
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['Product', 'Units Sold', 'Total Revenue (Ksh)', 'Average Price (Ksh)', 'First Sale', 'Last Sale'])

    for p in product_data:
        cw.writerow([
            p.name,
            p.units_sold,
            f"{p.revenue:,.2f}",
            f"{p.avg_price:,.2f}",
            p.first_sale.strftime('%Y-%m-%d') if p.first_sale else '',
            p.last_sale.strftime('%Y-%m-%d') if p.last_sale else ''
        ])
        # Add an empty row for separation before the total
    cw.writerow([]) 
    
    # Add the total sales row
    cw.writerow(['TOTAL SALES (Filtered Period)', '', '', '', '', f"{total_filtered_sales:,.2f}"])


    output = si.getvalue()
    si.close()

    return Response(
        output,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment;filename=product_performance_{start.strftime("%Y%m%d")}_to_{end.strftime("%Y%m%d")}.csv'} # Dynamic filename
    )
    
@main_bp.route("/about")
def about():
    return render_template("info_pages/about.html")

@main_bp.route("/features")
def features():
    return render_template("info_pages/features.html")

@main_bp.route("/privacy-policy")
def privacy_policy():
    return render_template("info_pages/privacy.html")

@main_bp.route("/terms-of-service")
def terms_of_service():
    return render_template("info_pages/terms.html")

@main_bp.route("/support")
def support():
    return render_template("info_pages/support.html")
    
@main_bp.app_errorhandler(404)
def page_not_found(e):
    return render_template('errors/404.html'), 404

@main_bp.app_errorhandler(500)
def internal_server_error(e):
    db.session.rollback()  # Safety measure
    logger.exception("500 Error: %s", str(e))
    return render_template('errors/500.html'), 500

    
