from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

DEFAULT_KPI_CATEGORIES = [
    "Operations Dataset",  # wide Book1-style table (recommended)
    "Revenue and Expenses",
    "Regulatory Compliance",
    "Service Utilization",
    "Patient Volume and Flow",
    "Top Diagnoses",
    "Clients Occupation",
    "Clients Residence",
    "Clients Age Group",
    "Feedback Source",
    "Feedback Experience",
    "Marketing Action Log",
    "Clients Sponsorship",
    "New Clients Survey",
]


class Organization(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    region = db.Column(db.String(100), nullable=False)
    # pending / approved / rejected / suspended
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    users = db.relationship("User", backref="organization", lazy=True)
    categories = db.relationship("KPICategory", backref="organization", lazy=True)
    datasets = db.relationship("Dataset", backref="organization", lazy=True)
    reports = db.relationship("ReportRecord", backref="organization", lazy=True)
    targets = db.relationship("OrgTarget", backref="organization", lazy=True)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # super_admin / org_admin / staff
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class KPICategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    is_default = db.Column(db.Boolean, default=False)

    datasets = db.relationship("Dataset", backref="category", lazy=True)


class Dataset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("kpi_category.id"), nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    period_start = db.Column(db.String(20), nullable=False)
    period_end = db.Column(db.String(20), nullable=False)
    original_filename = db.Column(db.String(255))
    row_count = db.Column(db.Integer, default=0)
    missing_count = db.Column(db.Integer, default=0)
    columns_json = db.Column(db.Text)   # JSON list of column names
    cleaned_json = db.Column(db.Text)   # JSON list of cleaned row dicts
    corrections_json = db.Column(db.Text)  # JSON summary of what was corrected
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


class ReportRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False)
    period_start = db.Column(db.String(20), nullable=False)
    period_end = db.Column(db.String(20), nullable=False)
    summary_json = db.Column(db.Text)   # JSON: per-category computed stats
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    generated_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class OrgTarget(db.Model):
    """Simple revenue targets / expense budgets set by org admin for a period."""
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=False)
    period_start = db.Column(db.String(20), nullable=False)
    period_end = db.Column(db.String(20), nullable=False)
    department = db.Column(db.String(120), nullable=False)
    # revenue_target or expense_budget
    target_type = db.Column(db.String(30), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)


class AuditLog(db.Model):
    """Track sensitive actions for accountability (no patient row contents stored)."""
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(80), nullable=False)
    detail = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class UatFeedback(db.Model):
    """Lightweight usability feedback for evaluation objective."""
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organization.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    role = db.Column(db.String(30))
    ease_score = db.Column(db.Integer)  # 1-5
    useful_score = db.Column(db.Integer)  # 1-5
    comment = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
