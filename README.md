# 💊 PharmaDesk

![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge/flask-2.x-lightgrey)
![PostgreSQL](https://img.shields.io/badge/database-PostgreSQL-blue)
![License](https://img.shields.io/badge/license-private-red)

**PharmaDesk** is Kenya's leading pharmacy management system with real-time inventory, performance analytics, and seamless sales processing — built for modern pharmacies that want clarity, control, and efficiency.

---

## 🚀 Features

### 📦 Inventory Management

#### Products

- Setup product categories
- Filter by category
- Activate/deactivate products
- Stock status: out of stock, low stock (reorder level), in stock
- Define selling/buying price & max discount
- Total quantity auto-calculated (sum of all batches)

#### Batches

- Track days to expiry & remaining quantity
- Attach supplier info per batch
- Searchable by product name
- Add / Edit / Delete batches

#### Alerts & Monitoring

- Alert card: all products with quantity ≤ 10
- Graph: batches expiring in next 30 days

---

### 🧾 Sales Processing

- Search & add products to cart
- Stock deduction uses FEFO (first-expiry-first-out)
- Choose payment method (Cash or M-Pesa dropdown)
- 3 recent transactions preview
- Printable receipt under sales history:
  - Includes timestamp, quantity, amount, payment method, transaction ID

---

### 💸 Expense Tracking

- View expense chart
- Add and delete expenses

---

### 📊 Reports

- 7-day sales trend (Cash vs M-Pesa)
- Today's total sales + % progress toward monthly goal
- Top 5 best-selling products (by units sold)
- Export all product performance data to CSV
  - Date range filtering available

---

## 🛠️ Tech Stack

| Layer    | Tech Used                   |
| -------- | --------------------------- |
| Backend  | Flask (Python)              |
| Database | PostgreSQL                  |
| Frontend | HTML, CSS (Bootstrap), AJAX |
| Hosting  | Ultahost VPS                |

---

## ⚙️ Getting Started

### ✅ Prerequisites

- Python 3.10+
- PostgreSQL
- Git installed

### 🐍 Setup (Local Development)

```bash
# Clone the project
git clone https://github.com/your-username/pharmadesk.git
cd pharmadesk

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```
