from flask_wtf import FlaskForm
from wtforms import (StringField, SelectField, DateField, IntegerField, 
                    DecimalField, TextAreaField, BooleanField, SubmitField, PasswordField, HiddenField)
from wtforms.validators import DataRequired, NumberRange, Optional, Length, ValidationError, InputRequired, EqualTo, Email, Regexp
from datetime import date
from .models import Category, Product, User, Pharmacy, Batch
from .enums import PaymentMethod, AlertType
from . import db

class RegistrationForm(FlaskForm):
    # Pharmacy Info
    pharmacy_name = StringField('Pharmacy Name', validators=[
        DataRequired(), Length(min=2, max=150, message="Pharmacy name must be between 2 and 150 characters.")
    ])
    
    location = StringField('Location', validators=[
        Optional(), Length(max=255, message="Location cannot exceed 255 characters.")
    ])
    
    monthly_target = DecimalField('Monthly Sales Target (KES)', validators=[
        Optional(), NumberRange(min=0, message="Monthly target must be a positive number")
    ])

    # Pharmacist Info
    full_name = StringField('Full Name', validators=[
        DataRequired(), Length(min=3, max=150)
    ])

    username = StringField('Username', validators=[
        DataRequired(), Length(min=3, max=50)
    ])

    phone = StringField('Phone', validators=[
        DataRequired(), Regexp(r'^07\d{8}$', message="Enter a valid Safaricom number starting with 07")
    ])

    email = StringField('Email', validators=[
        Optional(), Email(message="Invalid email address.")
    ])

    password = PasswordField('Password', validators=[
        DataRequired(), Length(min=6, message="Password must be at least 6 characters.")
    ])

    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(), EqualTo('password', message="Passwords must match.")
    ])

    submit = SubmitField('Register')

    # --- Custom validators ---
    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError("Username already exists. Please choose a different one.")

    def validate_phone(self, phone):
        user = User.query.filter_by(phone=phone.data).first()
        if user:
            raise ValidationError("Phone number already exists.")

    def validate_pharmacy_name(self, pharmacy_name):
        pharmacy = Pharmacy.query.filter_by(name=pharmacy_name.data).first()
        if pharmacy:
            raise ValidationError("Pharmacy name is already registered.")

class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    remember = BooleanField('Remember this device')  
    submit = SubmitField('Login')

class ProductForm(FlaskForm):
    name = StringField('Product Name', validators=[DataRequired()])
    generic_name = StringField('Generic Name')
    dosage = StringField('Dosage')
    
    prescription_required = BooleanField('Prescription Required', default=False)
    is_active = BooleanField('Active', default=True)
    
    cost_price = DecimalField('Cost Price', validators=[DataRequired()])
    selling_price = DecimalField('Selling Price', validators=[DataRequired()])
    max_discount = DecimalField('Max Discount', validators=[DataRequired()])
    
    reorder_level = IntegerField('Reorder Level', validators=[DataRequired(), NumberRange(min=1)], default=5)
    category_id = SelectField('Category', coerce=int, choices=[], validators=[DataRequired()])
    
    # This will be populated with category choices from the database
    def populate_categories(self, categories):
        self.category_id.choices = [(category.id, category.name) for category in categories]
    
    submit = SubmitField('Save Product')

class BatchForm(FlaskForm):
    batch_number = StringField('Batch Number', validators=[DataRequired()])
    manufacture_date = DateField('Manufacture Date', format='%Y-%m-%d', default=date.today)
    expiry_date = DateField('Expiry Date', format='%Y-%m-%d', validators=[DataRequired()])
    order_quantity = IntegerField('Order Quantity', validators=[DataRequired(), NumberRange(min=1)])
    supplier = StringField('Supplier')
    supplier_contact = StringField('Supplier Contact')
    product_id = SelectField('Product', coerce=int, choices=[], validators=[DataRequired()])

    # This will be populated with product choices from the database
    def populate_products(self, products):
        self.product_id.choices = [(product.id, product.name) for product in products]
        
    submit = SubmitField('Save Batch')
    
    def __init__(self, pharmacy_id, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pharmacy_id = pharmacy_id
        self.original_number = kwargs.get('obj') and kwargs.get('obj').batch_number

    def validate_batch_number(self, field):
        if not self.original_number or field.data != self.original_number:
            existing = Batch.query.filter_by(
                batch_number=field.data,
                pharmacy_id=self.pharmacy_id 
            ).first()
            if existing:
                raise ValidationError("This batch number already exists in your inventory.") 
    
class CategoryForm(FlaskForm):
    id = HiddenField("ID") 
    name = StringField(
        'Category Name',
        validators=[
            DataRequired(message="Category name is required"),
            Length(min=2, max=100, message="Name must be between 2-100 characters")
        ],
        render_kw={
            "placeholder": "e.g., Antibiotics, Pain Relief",
            "autofocus": True
        }
    )
    
    description = TextAreaField(
        'Description',
        validators=[Optional(), Length(max=500)],
        render_kw={
            "placeholder": "Optional description (max 500 chars)",
            "rows": 3
        }
    )
    
    submit = SubmitField('Save Category')

    def __init__(self, pharmacy_id, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pharmacy_id = pharmacy_id
        self.original_name = kwargs.get('obj') and kwargs.get('obj').name
        
    def validate_name(self, field):
        # Check if category name already exists (case insensitive)
        if not self.original_name or field.data.lower() != self.original_name.lower():  # Only check if name changed
            existing = db.session.query(Category).filter(
                Category.pharmacy_id == self.pharmacy_id,
                db.func.lower(Category.name) == db.func.lower(field.data))
            if existing.count() > 0:
                raise ValidationError('A category with this name already exists')           

class SaleForm(FlaskForm):
    product = HiddenField('Product', validators=[
        DataRequired(message="Please select a product before adding to cart.")
    ])

    quantity = IntegerField('Quantity', validators=[
        DataRequired(message="Please enter a quantity."),
        InputRequired(message="Quantity is required."),
        NumberRange(min=1, message="Quantity must be at least 1.")
    ])

    submit = SubmitField('Add to Cart')
    
    def __init__(self, pharmacy_id, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pharmacy_id = pharmacy_id

    def validate_quantity(self, field):
        product = Product.query.filter_by(id=self.product.data, pharmacy_id=self.pharmacy_id).first()
        if product:
            if not product.is_active:
                raise ValidationError(f"'{product.name}' is no longer available.")
            if field.data > product.quantity:
                raise ValidationError(f"Only {product.quantity} left in stock for '{product.name}'.")

class PaymentForm(FlaskForm):
    amount_due = DecimalField(
        'Amount Due',
        places=2,
        validators=[
            DataRequired(message="Please enter the amount due."),
            NumberRange(min=0, message="Amount must be at least 0.")
        ]
    )
    payment_method = SelectField(
    'Payment Method',
    choices=[] + [(m.value, m.display) for m in PaymentMethod],
    default="",
    coerce=str,
    validators=[DataRequired(message="Please select a payment method.")]
   )
    customer_name = StringField('Customer Name (Required for Prescription Items)',
                              validators=[Optional()])
    submit = SubmitField('Process Payment')

    def __init__(self, *args, **kwargs):
        super(PaymentForm, self).__init__(*args, **kwargs)
        self.payment_method.choices = [
                    ('', 'Select Payment Method'),
                    ('CASH', 'Cash'),
                    ('MPESA', 'M-Pesa')
            ]
class ExpenseForm(FlaskForm):
    date = DateField(
        'Date',
        validators=[DataRequired()],
        default=date.today,
        format='%Y-%m-%d'
    )
    
    category = StringField(
        'Category',
        validators=[
            DataRequired(),
            Length(min=2, max=50, message='Category must be between 2-50 characters')
        ],
        render_kw={
            'placeholder': 'e.g. Utilities, Supplies, Salaries',
            'autofocus': True
        }
    )
    
    amount = DecimalField(
        'Amount (KES)',
        validators=[
            DataRequired(message='Please enter a valid amount'),
            NumberRange(
                min=0.01,
                max=10000000,
                message='Amount must be between KES 0.01 and 10,000,000'
            )
        ],
        places=2,
        render_kw={'placeholder': '0.00'}
    )
    
    description = TextAreaField(
        'Description',
        validators=[
            Optional(),
            Length(max=200, message='Description cannot exceed 200 characters')
        ],
        render_kw={
            'placeholder': 'Optional details about this expense',
            'rows': 3
        }
    )
    
    submit = SubmitField(
        'Save Expense',
        render_kw={'class': 'btn btn-hospital'}
    )

    # Hidden field for edit form
    id = HiddenField()

class AlertForm(FlaskForm):
    alert_type = SelectField('Alert Type', 
                           choices=[(at.value, at.name) for at in AlertType],
                           validators=[DataRequired()])
    message = TextAreaField('Message', validators=[DataRequired()])
    severity = SelectField('Severity', 
                          choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High')])
    submit = SubmitField('Create Alert')
    