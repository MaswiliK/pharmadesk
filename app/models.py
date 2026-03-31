# app/models.py
from datetime import datetime, date, timedelta
from decimal import Decimal
from enum import Enum
from flask_login import UserMixin, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from . import db
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import deferred, validates, synonym
from sqlalchemy import UniqueConstraint, select, func, Date, desc, CheckConstraint, Index, text, cast
from zoneinfo import ZoneInfo

eat_tz = ZoneInfo("Africa/Nairobi")


class PaymentMethod(Enum):
    CASH = 'CASH'
    MPESA = 'MPESA'  

class AlertType(Enum):
    LOW_STOCK = 'low_stock'
    OUT_OF_STOCK = "out_of_stock"
    EXPIRING = "expiring_soon"
    EXPIRED = "expired"
    SLOW_MOVING = "slow_moving"
    RECALL = "recall"
    QUALITY_ISSUE = "quality_issue"
    PAYMENT = 'payment_reminder'
    REORDER = 'reorder'
    

# ------------------------------
# User Model
# ------------------------------
class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)

    # Identity
    user_code = db.Column(db.String(10), unique=True, index=True)  # e.g. PDU001
    full_name = db.Column(db.String(150), nullable=False)
    username = db.Column(db.String(100), unique=True, nullable=False)  # For login
    password = db.Column(db.String(256), nullable=False)  # Hashed password

    # Contact Info
    phone = db.Column(db.String(20), unique=True, nullable=False)  # Key for M-Pesa, alerts
    email = db.Column(db.String(120), unique=True, nullable=True)  # Optional
    
    next_payment_due = db.Column(db.DateTime, nullable=True)
    subscription_status = db.Column(db.String(20), default='INACTIVE')  # ACTIVE / EXPIRED
    
    role = db.Column(db.String(20), default='USER', index=True)  # ADMIN / USER / SUPPORT

    # Pharmacy Link
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)
    pharmacy = db.relationship('Pharmacy', back_populates='users')

    # Optional metadata
    created_at = db.Column(db.DateTime, default=datetime.now(eat_tz))
    last_login = db.Column(db.DateTime, nullable=True)
    last_login_ip = db.Column(db.String(45), nullable=True)  # IPv6 compatible
    
    @staticmethod
    def generate_code(username, max_attempts=5):
        for attempt in range(max_attempts):
            with db.session.begin_nested():
                db.session.execute(text("LOCK TABLE users IN EXCLUSIVE MODE"))
                user_count = db.session.query(User).count()
                next_number = user_count + 1
                padded_number = str(next_number).zfill(3)
                first_letter = username[0].upper() if username[0].isalpha() else 'X'
                candidate_code = f'PD{first_letter}{padded_number}'

                if not db.session.query(User).filter_by(user_code=candidate_code).first():
                    return candidate_code
        raise Exception("Unable to generate unique user code after multiple attempts")

    def set_password(self, raw_password: str) -> None:
        """Hash and store the password. Call this instead of hashing inline."""
        self.password = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        """Verify a plaintext password against the stored hash."""
        return check_password_hash(self.password, raw_password)

    def __repr__(self):
        return f"<User {self.username} ({self.full_name}) - Pharmacy: {self.pharmacy.name}>"
    
# ------------------------------
# Pharmacy Model
# ------------------------------
class Pharmacy(db.Model):
    __tablename__ = 'pharmacies'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    location = db.Column(db.String(255), nullable=True)  # Optional
    monthly_target = db.Column(db.Numeric(10, 2), nullable=True, default=Decimal('20000.00'))  # Monthly sales target
    
    created_at = db.Column(db.DateTime, default=datetime.now(eat_tz))

    # One-to-many relationship with user (pharmacist)
    users = db.relationship('User', back_populates='pharmacy', uselist=True)
    
    products = db.relationship('Product', backref='pharmacy', lazy=True)
    sales = db.relationship('Sale', backref='pharmacy', lazy=True)
    expenses = db.relationship('Expense', backref='pharmacy', lazy=True)
    batches = db.relationship('Batch', backref='pharmacy', lazy=True)
    
    def __repr__(self):
        return f"<Pharmacy {self.name}>"

# ------------------------------
# Category Model
# ------------------------------
class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)
    products = db.relationship('Product', back_populates='category')
    
    __table_args__ = (
        # A category name must be unique within a single pharmacy
        UniqueConstraint('name', 'pharmacy_id', name='_category_name_pharmacy_uc'),
    )

    def __repr__(self):
        return f"<Category {self.name}>"

# ------------------------------
# Product  and Batches Model (Combined Drug/Inventory)
# ------------------------------
class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    
    # Core Information
    name = db.Column(db.String(100), nullable=False, index=True)
    generic_name = db.Column(db.String(100), index=True)
    dosage = db.Column(db.String(50), index=True)
    
    # Flags
    prescription_required = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True, index=True)
    
    # Tracking
    date_added = db.Column(db.DateTime, default=lambda: datetime.now(eat_tz))
    last_restocked = db.Column(db.DateTime, default=lambda: datetime.now(eat_tz))
    
    # Pricing
    cost_price = db.Column(db.Numeric(10, 2), nullable=False)
    selling_price = db.Column(db.Numeric(10, 2), nullable=False)
    max_discount = db.Column(db.Numeric(5, 2), nullable=True, default=0)
    
    reorder_level = db.Column(db.Integer, default=5, nullable=False) 
     
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=False)
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)
    
    # Relationships
    category = db.relationship('Category', back_populates='products')
    batches = db.relationship('Batch', back_populates='product', cascade='all, delete-orphan')
    sales = db.relationship('Sale', back_populates='product', lazy='dynamic')
    
    __table_args__ = (
        CheckConstraint('selling_price >= cost_price', name='check_selling_price'),
        CheckConstraint('max_discount BETWEEN 0 AND 100', name='check_discount_range'),
        Index('ix_product_active_stock', 'is_active', 'reorder_level'),
    )
    
    @hybrid_property
    def stock_age(self):
        if not self.batches:
            return 0
        oldest = min(b.manufacture_date for b in self.batches)
        return (date.today() - oldest).days
    
    @stock_age.expression
    def stock_age(cls):
        return (
            select(func.coalesce(func.julianday(func.date()) - func.julianday(func.min(Batch.manufacture_date)), 0))
            .where(Batch.product_id == cls.id)
            .correlate_except(Batch)
            .label('stock_age')
        )
    
    @hybrid_property
    def quantity(self):
        # Returns the total quantity in all batches for this product
        return sum(batch.stock_lvl for batch in self.batches) if self.batches else 0

    @quantity.expression
    def quantity(cls):
        # Provides SQL expression for efficient querying
        return (
            select(func.coalesce(func.sum(Batch.stock_lvl), 0))
            .where(Batch.product_id == cls.id)
            .label('total_quantity')
        )
        
    @hybrid_property
    def margin(self):
        """Calculate profit margin for potential display"""
        if self.selling_price == 0:
            return 0
        return ((self.selling_price - self.cost_price) / self.cost_price) * 100
    
    @property
    def stock_status(self):
        """Text status for display (similar to your template logic)"""
        if not self.is_active:
            return "Inactive"
        if self.quantity <= 0:
            return "Out of Stock"
        if self.quantity <= self.reorder_level:
            return "Low Stock"
        return "In Stock"    
    
    @property
    def nearest_expiry(self):
        if not self.batches:
            return None
        return min(b.expiry_date for b in self.batches if b.stock_lvl > 0)
    
    @hybrid_property
    def total_sales(self):
        return sum(sale.quantity for sale in self.sales)

    @total_sales.expression
    def total_sales(cls):
        return (
            select(func.sum(Sale.quantity))
            .where(Sale.product_id == cls.id)
            .label('total_sales')
        )

    # Validation
    @validates('selling_price', 'cost_price')
    def validate_prices(self, key, value):
        if value < 0:
            raise ValueError("Prices cannot be negative")
        if key == 'selling_price' and self.cost_price and value < self.cost_price:
            raise ValueError("Selling price cannot be below cost price")
        return value

    def __repr__(self):
        return f"<Product {self.name} ({self.id})>"
    
class Batch(db.Model):
    __tablename__ = 'batches'
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    
    # Batch Information
    batch_number = db.Column(db.String(50), nullable=False)
    manufacture_date = db.Column(db.Date, default=date.today)
    expiry_date = db.Column(db.Date, nullable=False, index=True)
    order_quantity = db.Column(db.Integer, nullable=False)
    stock_lvl = db.Column(db.Integer, nullable=False, default=0)
    
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)
    
    # Supplier Information 
    supplier = db.Column(db.String(100))
    supplier_contact = db.Column(db.String(100))
    
    product = db.relationship('Product', back_populates='batches')
    
    __table_args__ = (
        UniqueConstraint('batch_number', 'pharmacy_id', name='_batch_number_pharmacy_uc'),
        CheckConstraint('expiry_date > manufacture_date', name='check_expiry_date'),
        CheckConstraint('order_quantity >= 0', name='check_order_quantity'),
        CheckConstraint('stock_lvl BETWEEN 0 AND order_quantity', name='check_stock_level'),
        Index('ix_batch_expiry_stock', 'expiry_date', 'stock_lvl'),
    )
    
    @hybrid_property
    def days_left(self):
        return (self.expiry_date - date.today()).days
    
    @hybrid_property
    def age(self):
        return (date.today() - self.manufacture_date).days
    
    @hybrid_property
    def utilization_rate(self):
        return ((self.order_quantity - self.stock_lvl) / self.order_quantity) * 100
    
    @hybrid_property
    def sales_velocity(self):
        """Units sold per day since batch creation"""
        days_in_stock = (datetime.now(eat_tz) - self.manufacture_date).days
        if days_in_stock == 0:
            return 0
        return (self.order_quantity - self.stock_lvl) / days_in_stock

    # Validation
    @validates('expiry_date')
    def validate_expiry_date(self, key, value):
        if value < self.manufacture_date:
            raise ValueError("Expiry date must be after manufacture date")
        return value

    @validates('stock_lvl')
    def validate_stock_level(self, key, value):
        if value < 0 or value > self.order_quantity:
            raise ValueError("Stock level must be between 0 and order quantity")
        return value

    def __repr__(self):
        return f"<Batch {self.batch_number} ({self.product.name})>"    

# ------------------------------
# Sale Model
# ------------------------------
class Sale(db.Model):
    __tablename__ = 'sales'
    id = db.Column(db.Integer, primary_key=True)
    
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    batch_id = db.Column(db.Integer, db.ForeignKey('batches.id'), nullable=False)
    transaction_id = db.Column(db.String(20), nullable=False)
    
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    total_price = db.Column(db.Numeric(10, 2), nullable=False)
    _payment_method = db.Column('payment_method', db.Enum('CASH', 'MPESA', name='paymentmethod'), nullable=False)
    sale_time = db.Column(db.DateTime, default=lambda: datetime.now(eat_tz))
    
    # Customer Info (optional)
    customer_name = db.Column(db.String(100), nullable=True)
    
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)
    
    # Relationships
    product = db.relationship('Product', back_populates='sales')
    batch = db.relationship('Batch')
    
    __table_args__ = (
    db.Index('ix_sale_time', 'sale_time'),
    db.Index('ix_sale_date', cast(sale_time, Date)),
    )
    
    def sales_trend(self, days=7):
        """Calculate sales trend percentage for given period"""
        current = self._get_sales_period(days=days)
        previous = self._get_sales_period(days=days, offset=days)
        return self._calculate_trend(current, previous)

    def _get_sales_period(self, days, offset=0):
        """Get sales data for a time period"""
        end = datetime.now(eat_tz) - timedelta(days=offset)
        start = end - timedelta(days=days)
        return db.session.query(
            func.coalesce(func.sum(Sale.quantity), 0).label('quantity'),
            func.coalesce(func.sum(Sale.total_price), 0).label('revenue')
        ).filter(
            Sale.product_id == self.id,
            Sale.sale_time >= start,
            Sale.sale_time < end
        ).first()

    def _calculate_trend(self, current, previous):
        """Calculate percentage change between two periods"""
        curr_qty, _ = current
        prev_qty, _ = previous
        if prev_qty == 0:
            return 100.0 if curr_qty > 0 else 0.0
        return ((curr_qty - prev_qty) / prev_qty) * 100 
    
    @classmethod
    def get_daily_sales(cls, pharmacy_id, start_date, end_date):
        return cls.query.with_entities(
            cast(cls.sale_time, Date).label('date'),
            func.sum(cls.total_price).label('total')
        ).filter(
            cls.pharmacy_id == pharmacy_id,
            cast(cls.sale_time, Date) >= start_date,
            cast(cls.sale_time, Date) <= end_date
        ).group_by('date').order_by('date')

    @classmethod
    def get_todays_top_product(cls, pharmacy_id):
        today = datetime.now(eat_tz).date()
        return Product.query.join(cls).filter(
            cls.pharmacy_id == pharmacy_id,
            cast(cls.sale_time, Date) == today
        ).with_entities(
            Product.name,
            func.sum(cls.quantity).label('total_units')
        ).group_by(Product.id).order_by(desc('total_units')).first()
        
    @classmethod
    def get_daily_summary(cls, start_date, end_date):
        return db.session.query(
            cast(cls.sale_time, Date).label('date'),
            func.sum(cls.total_price).label('daily_total'),
            func.count(cls.id).label('transactions'),
            func.argmax(Product.name, func.sum(cls.quantity)).label('top_product')
        ).join(Product).filter(
            cast(cls.sale_time, Date) >= start_date,
            cast(cls.sale_time, Date) <= end_date
        ).group_by('date').order_by('date')

    @classmethod
    def get_product_performance(cls, days=30):
        """Get product sales rankings for period"""
        cutoff_date = datetime.now(eat_tz) - timedelta(days=days)
        return db.session.query(
            Product.name,
            func.sum(cls.quantity).label('units_sold'),
            func.sum(cls.total_price).label('revenue'),
            (func.sum(cls.total_price) / func.sum(cls.quantity)).label('avg_price')
        ).join(Product).filter(
            cls.sale_time >= cutoff_date
        ).group_by(Product.id).order_by(db.desc('revenue'))    
    
    @property
    def payment_method(self):
        return PaymentMethod(self._payment_method)
    
    @payment_method.setter
    def payment_method(self, value):
        if isinstance(value, PaymentMethod):
            self._payment_method = value.value
        elif isinstance(value, str):
            normalized = value.upper().replace('-', '').replace(' ', '')
            if normalized in ('MPESA', 'MPESA'):
                normalized = 'MPESA'
            self._payment_method = normalized
        else:
            raise ValueError(f"Invalid payment method: {value}")
    
    payment_method = synonym('_payment_method', descriptor=payment_method)    

# ------------------------------
# Expense Model
# ------------------------------
class Expense(db.Model):
    __tablename__ = 'expenses'
    id = db.Column(db.Integer, primary_key=True)
    
    date = db.Column(db.Date, default=date.today, index=True)
    category = db.Column(db.String(50), nullable=False)
    description = db.Column(db.String(200))
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)
    
    # Tracking
    recorded_at = db.Column(db.DateTime, default=lambda: datetime.now(eat_tz))
    
    def __repr__(self):
        return f"<Expense {self.category} ({self.amount})>"

# ------------------------------  
# Reports Model
# ------------------------------  
class SalesRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    top_seller = db.Column(db.String(100), nullable=False)
    
    pharmacy_id = db.Column(db.Integer, db.ForeignKey('pharmacies.id'), nullable=False)

    @classmethod
    def get_weekly_sales(cls):
        # Get sales for the last 7 days
        end_date = datetime.now(eat_tz).date()
        start_date = end_date - timedelta(days=6)
        
        sales_data = cls.query.filter(
            cls.date.between(start_date, end_date)
        ).order_by(cls.date).all()
        
        return sales_data

    @classmethod
    def get_todays_stats(cls):
        today = datetime.now(eat_tz).date()
        record = cls.query.filter_by(date=today).first()
        
        if not record:
            return {
                'sales': 0,
                'top_seller': 'No sales today'
            }
            
        return {
            'sales': record.amount,
            'top_seller': record.top_seller
        }  
        
# ------------------------------  
# Payments Model
# ------------------------------   
class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_code = db.Column(db.String(50), nullable=False) # Your internal user identifier
    phone_number = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default='PENDING') # PENDING, SUCCESS, FAILED
    merchant_request_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    checkout_request_id = db.Column(db.String(100), unique=True, nullable=False)
    mpesa_receipt_number = db.Column(db.String(50), nullable=True)
    result_desc = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now(eat_tz))
    updated_at = db.Column(db.DateTime, onupdate=datetime.now(eat_tz))

    def __repr__(self):
        return f'<Payment {self.id} for {self.user_code}>'  
    
 # ------------------------------  
# Payments receipts model
# ------------------------------            
class PaymentReceipt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_code = db.Column(db.String(50), nullable=False)
    receipt = db.Column(db.String(20), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.now(eat_tz))
    status = db.Column(db.String(20), default='PENDING')  # PENDING, SUCCESS, FAILED