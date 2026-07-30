import os
import json
import io
from datetime import datetime
from functools import wraps

import pandas as pd
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, abort, send_file,
)

from models import (
    db, Organization, User, KPICategory, Dataset, ReportRecord,
    OrgTarget, AuditLog, UatFeedback, DEFAULT_KPI_CATEGORIES,
)
from data_utils import (
    read_any, clean_and_correct, summarize_dataset, is_operations_dataframe,
    OPERATIONS_COLUMNS, save_pending, load_pending, clear_pending, records_preview,
    anonymize_dataframe, deanonymize_dataframe,
    compute_quality_score, month_period_options,
    cleanup_stale_pending,
)
import report_engine as re_engine
# Ensure analysis helpers are available even if an old process kept a stale module.
from report_engine import (
    build_operations_trend,
    build_simple_forecast,
    build_simple_operations_report,
    compare_against_targets,
    build_recommendations,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = "dev-secret-change-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "instance", "app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB uploads

db.init_app(app)

# Official Tanzania regions (mainland + Zanzibar) for registration dropdown
TANZANIA_REGIONS = [
    "Arusha", "Dar es Salaam", "Dodoma", "Geita", "Iringa", "Kagera", "Katavi",
    "Kigoma", "Kilimanjaro", "Lindi", "Manyara", "Mara", "Mbeya", "Morogoro",
    "Mtwara", "Mwanza", "Njombe", "Pwani", "Rukwa", "Ruvuma", "Shinyanga",
    "Simiyu", "Singida", "Songwe", "Tabora", "Tanga",
    "Mjini Magharibi", "Kaskazini Unguja", "Kusini Unguja",
    "Kaskazini Pemba", "Kusini Pemba",
]


# ---------------------------------------------------------------- helpers
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def write_audit(action, detail="", org_id=None, user=None):
    user = user or current_user()
    entry = AuditLog(
        org_id=org_id if org_id is not None else (user.org_id if user else None),
        user_id=user.id if user else None,
        action=action,
        detail=(detail or "")[:500],
    )
    db.session.add(entry)
    db.session.commit()


def build_org_alerts(org_id):
    """In-app operational alerts for organization admins."""
    alerts = []
    recent = (
        Dataset.query.filter_by(org_id=org_id)
        .order_by(Dataset.uploaded_at.desc())
        .limit(8)
        .all()
    )
    for ds in recent:
        corr = {}
        try:
            corr = json.loads(ds.corrections_json or "{}")
        except Exception:
            pass
        score = corr.get("_quality_score")
        if score is not None and float(score) < 70:
            alerts.append({
                "level": "warn",
                "text": f"Low data quality ({score}/100) on {ds.category.name} "
                        f"({ds.period_start} → {ds.period_end}). Ask staff to improve source files.",
            })

    # Targets without matching dataset for same period
    targets = OrgTarget.query.filter_by(org_id=org_id).all()
    seen_periods = set()
    for t in targets:
        key = (t.period_start, t.period_end)
        if key in seen_periods:
            continue
        seen_periods.add(key)
        has_ds = Dataset.query.filter_by(
            org_id=org_id, period_start=t.period_start, period_end=t.period_end
        ).first()
        if not has_ds:
            alerts.append({
                "level": "info",
                "text": f"Targets set for {t.period_start} → {t.period_end}, but no cleaned dataset uploaded yet.",
            })

    # Stale uploads
    if recent:
        last = recent[0].uploaded_at
        if last and (datetime.utcnow() - last).days >= 30:
            alerts.append({
                "level": "warn",
                "text": "No cleaned dataset uploaded in the last 30 days.",
            })
    else:
        alerts.append({
            "level": "info",
            "text": "No datasets yet. Staff should upload operational data for the reporting period.",
        })

    return alerts[:8]


def login_required(roles=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if roles and user.role not in roles:
                abort(403)
            if user.role != "super_admin" and user.org_id:
                org = Organization.query.get(user.org_id)
                if org and org.status == "suspended":
                    session.clear()
                    flash("Your organization is suspended. Contact the platform admin.", "error")
                    return redirect(url_for("login"))
                if org and org.status != "approved":
                    session.clear()
                    flash("Your organization is not active.", "error")
                    return redirect(url_for("login"))
                if not user.is_active:
                    session.clear()
                    flash("Your account is inactive.", "error")
                    return redirect(url_for("login"))
            return fn(*args, **kwargs)
        return wrapper
    return decorator


@app.context_processor
def inject_user():
    return {
        "current_user": current_user(),
        "month_options": month_period_options(),
    }


def _stage_for_review(user, category, period_start, period_end, df, original_filename):
    cleaned_df, missing_report, corrections, total_rows = clean_and_correct(df)
    # Hide personal identifiers in stored/review data; keep map to restore on download
    cleaned_df, pii_map, pii_cols = anonymize_dataframe(cleaned_df)
    if pii_cols:
        corrections["_pii_columns"] = pii_cols
        corrections["_pii_map"] = pii_map
        anon_note = (
            "Personal columns were anonymized for privacy "
            f"({', '.join(pii_cols)}). Original values are restored when you download Excel/CSV."
        )
        existing = corrections.get("_notes")
        if isinstance(existing, dict) and existing.get("method"):
            existing["method"] = str(existing["method"]) + " " + anon_note
            corrections["_notes"] = existing
        else:
            corrections["_notes"] = {"method": anon_note}

    total_missing = sum(v for k, v in missing_report.items())
    quality = compute_quality_score(total_rows, total_missing, corrections)
    corrections["_quality_score"] = quality
    payload = {
        "category_id": category.id,
        "category_name": category.name,
        "period_start": period_start,
        "period_end": period_end,
        "original_filename": original_filename,
        "row_count": total_rows,
        "missing_count": total_missing,
        "quality_score": quality,
        "columns": list(cleaned_df.columns),
        "cleaned_records": json.loads(cleaned_df.to_json(orient="records")),
        "corrections": corrections,
        "missing_report": missing_report,
        "is_operations": is_operations_dataframe(cleaned_df),
        "pii_columns": pii_cols,
    }
    save_pending(BASE_DIR, user.id, payload)
    return payload


# ---------------------------------------------------------------- public
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    user = current_user()
    # Logged-in staff / org admins must not open a second organization registration
    if user and user.role in ("staff", "org_admin", "super_admin"):
        flash(
            "You already have an account. Staff cannot register a new organization — "
            "use your existing login. Organization registration is only for new polyclinic admins.",
            "error",
        )
        if user.role == "staff":
            return redirect(url_for("staff_dashboard"))
        if user.role == "org_admin":
            return redirect(url_for("org_admin_dashboard"))
        return redirect(url_for("super_admin_dashboard"))

    if request.method == "POST":
        org_name = request.form["org_name"].strip()
        region = request.form["region"].strip()
        admin_name = request.form["admin_name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if region not in TANZANIA_REGIONS:
            flash("Please select a valid region from the list.", "error")
            return redirect(url_for("register"))

        existing = User.query.filter_by(email=email).first()
        if existing:
            if existing.role == "staff":
                flash(
                    "This email belongs to a staff account. Staff cannot register an organization. "
                    "Log in with the account your organization admin created.",
                    "error",
                )
            elif existing.role == "org_admin":
                flash(
                    "This email already belongs to an organization admin. Log in instead of registering again.",
                    "error",
                )
            else:
                flash("That email is already registered.", "error")
            return redirect(url_for("register"))

        org = Organization(name=org_name, region=region, status="pending")
        db.session.add(org)
        db.session.flush()

        admin = User(org_id=org.id, name=admin_name, email=email, role="org_admin", is_active=False)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        flash("Registration submitted! Your organization is pending approval by the platform admin.", "success")
        return redirect(url_for("login"))

    return render_template("register.html", regions=TANZANIA_REGIONS)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))

        if user.role != "super_admin":
            if not user.is_active:
                flash("Your account is inactive. Contact your organization admin or platform admin.", "error")
                return redirect(url_for("login"))
            if user.org_id:
                org = Organization.query.get(user.org_id)
                if org and org.status == "pending":
                    flash("Your organization is still pending approval. Please wait for the platform admin.", "error")
                    return redirect(url_for("login"))
                if org and org.status == "rejected":
                    flash("Your organization registration was rejected.", "error")
                    return redirect(url_for("login"))
                if org and org.status == "suspended":
                    flash("Your organization is suspended (e.g. unpaid subscription). Contact the platform admin.", "error")
                    return redirect(url_for("login"))
                if org and org.status != "approved":
                    flash("Your organization is not active.", "error")
                    return redirect(url_for("login"))

        session["user_id"] = user.id
        if user.role == "super_admin":
            return redirect(url_for("super_admin_dashboard"))
        elif user.role == "org_admin":
            return redirect(url_for("org_admin_dashboard"))
        else:
            return redirect(url_for("staff_dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------- super admin
@app.route("/super-admin")
@login_required(roles=["super_admin"])
def super_admin_dashboard():

    pending = (
        db.session.query(Organization, User)
        .join(User, Organization.id == User.org_id)
        .filter(Organization.status == "pending", User.role == "org_admin")
        .all()
    )

    approved = (
        db.session.query(Organization, User)
        .join(User, Organization.id == User.org_id)
        .filter(Organization.status == "approved", User.role == "org_admin")
        .all()
    )

    suspended = (
        db.session.query(Organization, User)
        .join(User, Organization.id == User.org_id)
        .filter(Organization.status == "suspended", User.role == "org_admin")
        .all()
    )

    rejected = (
        db.session.query(Organization, User)
        .join(User, Organization.id == User.org_id)
        .filter(Organization.status == "rejected", User.role == "org_admin")
        .all()
    )

    return render_template(
        "super_admin.html",
        pending=pending,
        approved=approved,
        suspended=suspended,
        rejected=rejected,
    )

def _activate_org_users(org_id, active=True):
    users = User.query.filter_by(org_id=org_id).all()
    for u in users:
        u.is_active = active


def _seed_categories_if_needed(org_id):
    if KPICategory.query.filter_by(org_id=org_id).count() == 0:
        for name in DEFAULT_KPI_CATEGORIES:
            db.session.add(KPICategory(org_id=org_id, name=name, is_default=True))


@app.route("/super-admin/approve/<int:org_id>", methods=["POST"])
@login_required(roles=["super_admin"])
def approve_org(org_id):
    org = Organization.query.get_or_404(org_id)
    org.status = "approved"
    _activate_org_users(org.id, True)
    _seed_categories_if_needed(org.id)
    db.session.commit()
    flash(f'"{org.name}" approved and activated.', "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/reject/<int:org_id>", methods=["POST"])
@login_required(roles=["super_admin"])
def reject_org(org_id):
    org = Organization.query.get_or_404(org_id)
    org.status = "rejected"
    _activate_org_users(org.id, False)
    db.session.commit()
    flash(f'"{org.name}" rejected.', "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/suspend/<int:org_id>", methods=["POST"])
@login_required(roles=["super_admin"])
def suspend_org(org_id):
    org = Organization.query.get_or_404(org_id)
    org.status = "suspended"
    _activate_org_users(org.id, False)
    db.session.commit()
    write_audit("org_suspended", org.name, org_id=org.id)
    flash(f'"{org.name}" suspended. Staff and org admin accounts are inactive until reactivated.', "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/reactivate/<int:org_id>", methods=["POST"])
@login_required(roles=["super_admin"])
def reactivate_org(org_id):
    org = Organization.query.get_or_404(org_id)
    org.status = "approved"
    _activate_org_users(org.id, True)
    _seed_categories_if_needed(org.id)
    db.session.commit()
    write_audit("org_reactivated", org.name, org_id=org.id)
    flash(f'"{org.name}" reactivated (approved again).', "success")
    return redirect(url_for("super_admin_dashboard"))


# ---------------------------------------------------------------- org admin
@app.route("/org-admin")
@login_required(roles=["org_admin"])
def org_admin_dashboard():
    user = current_user()
    staff = User.query.filter_by(org_id=user.org_id, role="staff").all()
    categories = KPICategory.query.filter_by(org_id=user.org_id).all()
    reports = ReportRecord.query.filter_by(org_id=user.org_id).order_by(ReportRecord.generated_at.desc()).all()
    datasets = Dataset.query.filter_by(org_id=user.org_id).order_by(Dataset.uploaded_at.desc()).limit(20).all()
    targets = (
        OrgTarget.query.filter_by(org_id=user.org_id)
        .order_by(OrgTarget.period_start.desc(), OrgTarget.department.asc())
        .all()
    )
    audits = (
        AuditLog.query.filter_by(org_id=user.org_id)
        .order_by(AuditLog.created_at.desc())
        .limit(25)
        .all()
    )
    alerts = build_org_alerts(user.org_id)
    return render_template(
        "org_admin.html",
        staff=staff,
        categories=categories,
        reports=reports,
        datasets=datasets,
        targets=targets,
        audits=audits,
        alerts=alerts,
    )


@app.route("/org-admin/staff/add", methods=["POST"])
@login_required(roles=["org_admin"])
def add_staff():
    user = current_user()
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = request.form["password"]

    if User.query.filter_by(email=email).first():
        flash("That email is already in use.", "error")
        return redirect(url_for("org_admin_dashboard"))

    staff = User(org_id=user.org_id, name=name, email=email, role="staff", is_active=True)
    staff.set_password(password)
    db.session.add(staff)
    db.session.commit()
    flash(f"Staff account created for {name}.", "success")
    return redirect(url_for("org_admin_dashboard"))


@app.route("/org-admin/categories/add", methods=["POST"])
@login_required(roles=["org_admin"])
def add_category():
    user = current_user()
    name = request.form["name"].strip()
    if name:
        db.session.add(KPICategory(org_id=user.org_id, name=name, is_default=False))
        db.session.commit()
        flash(f'KPI category "{name}" added.', "success")
    return redirect(url_for("org_admin_dashboard"))


@app.route("/org-admin/categories/delete/<int:cat_id>", methods=["POST"])
@login_required(roles=["org_admin"])
def delete_category(cat_id):
    user = current_user()
    cat = KPICategory.query.get_or_404(cat_id)
    if cat.org_id != user.org_id:
        abort(403)
    db.session.delete(cat)
    db.session.commit()
    flash(f'KPI category "{cat.name}" removed.', "success")
    return redirect(url_for("org_admin_dashboard"))


@app.route("/org-admin/targets/add", methods=["POST"])
@login_required(roles=["org_admin"])
def add_target():
    user = current_user()
    department = request.form["department"].strip()
    target_type = request.form["target_type"].strip()
    period_start = request.form["period_start"]
    period_end = request.form["period_end"]
    try:
        amount = float(request.form["amount"])
    except (TypeError, ValueError):
        flash("Amount must be a number.", "error")
        return redirect(url_for("org_admin_dashboard"))

    if target_type not in {"revenue_target", "expense_budget"}:
        flash("Invalid target type.", "error")
        return redirect(url_for("org_admin_dashboard"))
    if not department or amount < 0:
        flash("Department and a non-negative amount are required.", "error")
        return redirect(url_for("org_admin_dashboard"))

    db.session.add(OrgTarget(
        org_id=user.org_id,
        period_start=period_start,
        period_end=period_end,
        department=department,
        target_type=target_type,
        amount=amount,
        created_by=user.id,
    ))
    db.session.commit()
    flash(f'Target/budget saved for {department}.', "success")
    return redirect(url_for("org_admin_dashboard"))


@app.route("/org-admin/targets/delete/<int:target_id>", methods=["POST"])
@login_required(roles=["org_admin"])
def delete_target(target_id):
    user = current_user()
    target = OrgTarget.query.get_or_404(target_id)
    if target.org_id != user.org_id:
        abort(403)
    db.session.delete(target)
    db.session.commit()
    flash("Target/budget removed.", "success")
    return redirect(url_for("org_admin_dashboard"))


# ---------------------------------------------------------------- staff (data entry only)
@app.route("/staff")
@login_required(roles=["staff"])
def staff_dashboard():
    user = current_user()
    categories = KPICategory.query.filter_by(org_id=user.org_id).all()
    recent = Dataset.query.filter_by(org_id=user.org_id).order_by(Dataset.uploaded_at.desc()).limit(15).all()
    pending = load_pending(BASE_DIR, user.id)
    return render_template(
        "staff.html",
        categories=categories,
        recent=recent,
        pending=pending,
        operations_columns=OPERATIONS_COLUMNS,
    )


@app.route("/staff/upload", methods=["POST"])
@login_required(roles=["staff"])
def upload_dataset():
    user = current_user()

    category_id = int(request.form["category_id"])
    period_start = request.form["period_start"]
    period_end = request.form["period_end"]
    file = request.files.get("datafile")

    category = KPICategory.query.get_or_404(category_id)
    if category.org_id != user.org_id:
        abort(403)

    if not file or file.filename == "":
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("staff_dashboard"))

    try:
        df = read_any(file, file.filename)
    except Exception:
        flash("Could not read that file. Please upload a valid .xlsx, .xls, or .csv file.", "error")
        return redirect(url_for("staff_dashboard"))

    if df.empty:
        flash("That file has no data rows.", "error")
        return redirect(url_for("staff_dashboard"))

    try:
        payload = _stage_for_review(user, category, period_start, period_end, df, file.filename)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("staff_dashboard"))

    write_audit(
        "upload_staged",
        f"{file.filename} → {category.name} ({period_start} to {period_end})",
        org_id=user.org_id,
        user=user,
    )
    if payload.get("pii_columns"):
        flash(
            "Personal columns were anonymized for on-screen use: "
            + ", ".join(payload["pii_columns"])
            + ". Download restores the original values.",
            "success",
        )
    return redirect(url_for("review_dataset"))


@app.route("/staff/manual", methods=["GET", "POST"])
@login_required(roles=["staff"])
def manual_entry():
    user = current_user()
    categories = KPICategory.query.filter_by(org_id=user.org_id).all()

    if request.method == "GET":
        draft = session.get("manual_draft", {"rows": []})
        return render_template(
            "manual_entry.html",
            categories=categories,
            columns=OPERATIONS_COLUMNS,
            draft_rows=draft.get("rows", []),
            period_start=draft.get("period_start", ""),
            period_end=draft.get("period_end", ""),
            category_id=draft.get("category_id", ""),
        )

    action = request.form.get("action", "add")
    draft = session.get("manual_draft", {"rows": []})
    draft["period_start"] = request.form.get("period_start") or draft.get("period_start", "")
    draft["period_end"] = request.form.get("period_end") or draft.get("period_end", "")
    draft["category_id"] = request.form.get("category_id") or draft.get("category_id", "")

    if action == "clear":
        session.pop("manual_draft", None)
        flash("Manual draft cleared.", "success")
        return redirect(url_for("manual_entry"))

    if action == "add":
        row = {}
        for col in OPERATIONS_COLUMNS:
            row[col] = request.form.get(col, "").strip()
        if not any(row.values()):
            flash("Enter at least one field before adding a row.", "error")
        else:
            draft.setdefault("rows", []).append(row)
            flash(f"Row added. Draft now has {len(draft['rows'])} row(s).", "success")
        session["manual_draft"] = draft
        return redirect(url_for("manual_entry"))

    # action == review
    rows = draft.get("rows", [])
    if not rows:
        flash("Add at least one row before reviewing.", "error")
        return redirect(url_for("manual_entry"))
    if not draft.get("period_start") or not draft.get("period_end") or not draft.get("category_id"):
        flash("Period and KPI category are required.", "error")
        return redirect(url_for("manual_entry"))

    category = KPICategory.query.get_or_404(int(draft["category_id"]))
    if category.org_id != user.org_id:
        abort(403)

    df = pd.DataFrame(rows)
    try:
        payload = _stage_for_review(
            user, category, draft["period_start"], draft["period_end"], df, "manual_entry.csv"
        )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("manual_entry"))

    session.pop("manual_draft", None)
    write_audit("manual_staged", f"{category.name} ({draft['period_start']} to {draft['period_end']})", org_id=user.org_id, user=user)
    if payload.get("pii_columns"):
        flash(
            "Personal columns were anonymized for on-screen use: "
            + ", ".join(payload["pii_columns"])
            + ". Download restores the original values.",
            "success",
        )
    return redirect(url_for("review_dataset"))


@app.route("/staff/review")
@login_required(roles=["staff"])
def review_dataset():
    user = current_user()
    pending = load_pending(BASE_DIR, user.id)
    if not pending:
        flash("Nothing to review. Upload a file or enter data first.", "error")
        return redirect(url_for("staff_dashboard"))
    preview = records_preview(pending["cleaned_records"], limit=15)
    correction_items = [
        (col, detail)
        for col, detail in pending.get("corrections", {}).items()
        if not str(col).startswith("_")
    ]
    notes = pending.get("corrections", {}).get("_notes", {}).get("method", "")
    return render_template(
        "review.html",
        pending=pending,
        preview=preview,
        correction_items=correction_items,
        notes=notes,
    )


@app.route("/staff/review/confirm", methods=["POST"])
@login_required(roles=["staff"])
def confirm_dataset():
    user = current_user()
    pending = load_pending(BASE_DIR, user.id)
    if not pending:
        flash("Nothing to save.", "error")
        return redirect(url_for("staff_dashboard"))

    category = KPICategory.query.get_or_404(pending["category_id"])
    if category.org_id != user.org_id:
        abort(403)

    ds = Dataset(
        org_id=user.org_id,
        category_id=category.id,
        uploaded_by=user.id,
        period_start=pending["period_start"],
        period_end=pending["period_end"],
        original_filename=pending["original_filename"],
        row_count=pending["row_count"],
        missing_count=pending["missing_count"],
        columns_json=json.dumps(pending["columns"]),
        cleaned_json=json.dumps(pending["cleaned_records"]),
        corrections_json=json.dumps(pending["corrections"]),
    )
    db.session.add(ds)
    db.session.commit()
    clear_pending(BASE_DIR, user.id)
    write_audit(
        "dataset_saved",
        f"{category.name}: {pending['row_count']} rows, quality {pending.get('quality_score', '—')}/100",
        org_id=user.org_id,
        user=user,
    )
    flash(
        f'Saved cleaned dataset for {category.name}: {pending["row_count"]} rows, '
        f'{pending["missing_count"]} issue(s) corrected, '
        f'quality score {pending.get("quality_score", "—")}/100.',
        "success",
    )
    return redirect(url_for("staff_dashboard"))


@app.route("/staff/review/cancel", methods=["POST"])
@login_required(roles=["staff"])
def cancel_review():
    user = current_user()
    clear_pending(BASE_DIR, user.id)
    flash("Review cancelled. Data was not saved.", "success")
    return redirect(url_for("staff_dashboard"))


@app.route("/staff/download/<int:dataset_id>")
@login_required(roles=["staff", "org_admin"])
def download_cleaned(dataset_id):
    user = current_user()
    ds = Dataset.query.get_or_404(dataset_id)
    if ds.org_id != user.org_id:
        abort(403)

    rows = json.loads(ds.cleaned_json)
    df = pd.DataFrame(rows)
    # Restore original personal values if anonymization map was stored
    try:
        corrections = json.loads(ds.corrections_json or "{}")
    except Exception:
        corrections = {}
    pii_map = corrections.get("_pii_map") or {}
    if pii_map:
        df = deanonymize_dataframe(df, pii_map)

    buf = io.BytesIO()
    fmt = request.args.get("format", "xlsx")
    write_audit(
        "dataset_downloaded",
        f"dataset #{dataset_id} as {fmt}" + (" (deanonymized)" if pii_map else ""),
        org_id=user.org_id,
        user=user,
    )
    if fmt == "csv":
        df.to_csv(buf, index=False)
        buf.seek(0)
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"cleaned_{ds.id}.csv",
            mimetype="text/csv",
        )

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Cleaned")
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"cleaned_{ds.id}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------- reports (org admin only)
@app.route("/reports/generate", methods=["POST"])
@login_required(roles=["org_admin"])
def generate_report():
    user = current_user()
    period_start = request.form["period_start"]
    period_end = request.form["period_end"]

    datasets = Dataset.query.filter_by(
        org_id=user.org_id, period_start=period_start, period_end=period_end
    ).all()

    if not datasets:
        flash("No cleaned datasets found for that period.", "error")
        return redirect(url_for("org_admin_dashboard"))

    summary = {}
    for ds in datasets:
        rows = json.loads(ds.cleaned_json)
        df = pd.DataFrame(rows)
        cat_name = ds.category.name

        if is_operations_dataframe(df) or cat_name == "Operations Dataset":
            cat_summary = build_simple_operations_report(df)
            period_targets = OrgTarget.query.filter_by(
                org_id=user.org_id, period_start=period_start, period_end=period_end
            ).all()
            if period_targets:
                cat_summary = compare_against_targets(
                    cat_summary,
                    [
                        {
                            "department": t.department,
                            "target_type": t.target_type,
                            "amount": t.amount,
                        }
                        for t in period_targets
                    ],
                )

            # History from prior Operations Dataset periods in the same year
            year = period_start[:4]
            history_ds = (
                Dataset.query.join(KPICategory)
                .filter(
                    Dataset.org_id == user.org_id,
                    KPICategory.name == "Operations Dataset",
                    Dataset.period_start.like(f"{year}-%"),
                    Dataset.period_start <= period_start,
                )
                .order_by(Dataset.period_start.asc())
                .all()
            )
            history = []
            for hds in history_ds:
                hrows = json.loads(hds.cleaned_json)
                hdf = pd.DataFrame(hrows)
                built = build_simple_operations_report(hdf)
                pl = (built.get("sections") or {}).get("profit_loss") or {}
                pf = (built.get("sections") or {}).get("patient_flow") or {}
                history.append({
                    "period_label": hds.period_start[:7],
                    "revenue_total": pl.get("revenue_total", 0),
                    "expense_total": pl.get("expense_total", 0),
                    "patients": pf.get("total_patients", 0),
                })
            trend = build_operations_trend(history) if history else None
            if trend:
                cat_summary.setdefault("sections", {})["trend"] = trend
            forecast = build_simple_forecast(history) if history else None
            if forecast:
                cat_summary.setdefault("sections", {})["forecast"] = forecast

        elif cat_name == "Revenue and Expenses" and re_engine.revenue_expense_columns_ok(df.columns):
            cat_summary = {"report_type": "revenue_expense"}
            cat_summary.update(re_engine.build_revenue_expense_report(df))
            year = period_start[:4]
            history_ds = (
                Dataset.query.join(KPICategory)
                .filter(
                    Dataset.org_id == user.org_id,
                    KPICategory.name == "Revenue and Expenses",
                    Dataset.period_start.like(f"{year}-%"),
                    Dataset.period_start <= period_start,
                )
                .order_by(Dataset.period_start.asc())
                .all()
            )
            history = []
            for hds in history_ds:
                hrows = json.loads(hds.cleaned_json)
                hdf = pd.DataFrame(hrows)
                if not re_engine.revenue_expense_columns_ok(hdf.columns):
                    continue
                built = re_engine.build_revenue_expense_report(hdf)
                history.append({
                    "period_label": hds.period_start[:7],
                    "revenue_total": built["revenue_total"],
                    "expense_total": built["expense_total"],
                })
            cat_summary["trend"] = re_engine.build_trend_report(history)
        elif cat_name == "Regulatory Compliance" and re_engine.compliance_columns_ok(df.columns):
            cat_summary = {"report_type": "compliance"}
            cat_summary.update(re_engine.build_compliance_report(df))
        else:
            cat_summary = {"report_type": "generic"}
            cat_summary.update(summarize_dataset(df))

        cat_summary["missing_corrected"] = ds.missing_count
        cat_summary["filename"] = ds.original_filename
        cat_summary["row_count"] = ds.row_count
        try:
            corr = json.loads(ds.corrections_json or "{}")
            cat_summary["quality_score"] = corr.get("_quality_score")
        except Exception:
            cat_summary["quality_score"] = None
        summary[cat_name] = cat_summary

    recommendations = build_recommendations(summary)
    report_payload = {"categories": summary, "recommendations": recommendations}

    report = ReportRecord(
        org_id=user.org_id,
        period_start=period_start,
        period_end=period_end,
        summary_json=json.dumps(report_payload),
        generated_by=user.id,
    )
    db.session.add(report)
    db.session.commit()
    write_audit(
        "report_generated",
        f"{period_start} to {period_end}",
        org_id=user.org_id,
        user=user,
    )

    flash(f"Report generated for {period_start} to {period_end}.", "success")
    return redirect(url_for("view_dashboard", report_id=report.id))


def _normalize_summary(summary):
    """Avoid Jinja clash: dict.key 'items' was read as dict.items method."""
    for cat in summary.values():
        if not isinstance(cat, dict):
            continue
        sections = cat.get("sections") or {}
        for section in sections.values():
            if isinstance(section, dict) and "items" in section and "rows" not in section:
                section["rows"] = section.pop("items")
    return summary


def _parse_report_payload(report):
    """
    Support both legacy summary_json ({cat: ...}) and
    new shape ({categories: {...}, recommendations: [...]}).
    """
    data = json.loads(report.summary_json)
    if isinstance(data, dict) and "categories" in data:
        summary = data.get("categories") or {}
        recommendations = data.get("recommendations")
    else:
        summary = data if isinstance(data, dict) else {}
        recommendations = None
        if isinstance(summary, dict) and "__recommendations__" in summary:
            recommendations = summary.pop("__recommendations__", None)
    summary = _normalize_summary(summary)
    if recommendations is None:
        recommendations = build_recommendations(summary)
    return summary, recommendations


@app.route("/dashboard/<int:report_id>")
@login_required(roles=["org_admin"])
def view_dashboard(report_id):
    user = current_user()
    report = ReportRecord.query.get_or_404(report_id)
    if report.org_id != user.org_id:
        abort(403)
    summary, recommendations = _parse_report_payload(report)
    org = Organization.query.get(user.org_id)
    return render_template(
        "dashboard.html",
        report=report,
        summary=summary,
        recommendations=recommendations,
        org=org,
    )


@app.route("/dashboard/<int:report_id>/print")
@login_required(roles=["org_admin"])
def print_report(report_id):
    """Simple printable HTML report (browser → Save as PDF)."""
    user = current_user()
    report = ReportRecord.query.get_or_404(report_id)
    if report.org_id != user.org_id:
        abort(403)
    summary, recommendations = _parse_report_payload(report)
    org = Organization.query.get(user.org_id)
    write_audit("report_printed", f"report #{report_id}", org_id=user.org_id, user=user)
    return render_template(
        "report_print.html",
        report=report,
        summary=summary,
        recommendations=recommendations,
        org=org,
    )


@app.route("/feedback", methods=["GET", "POST"])
@login_required(roles=["org_admin", "staff"])
def uat_feedback():
    user = current_user()
    if request.method == "POST":
        try:
            ease = int(request.form.get("ease_score", 0))
            useful = int(request.form.get("useful_score", 0))
        except ValueError:
            flash("Scores must be numbers from 1 to 5.", "error")
            return redirect(url_for("uat_feedback"))
        if ease < 1 or ease > 5 or useful < 1 or useful > 5:
            flash("Please rate ease and usefulness from 1 to 5.", "error")
            return redirect(url_for("uat_feedback"))
        fb = UatFeedback(
            org_id=user.org_id,
            user_id=user.id,
            role=user.role,
            ease_score=ease,
            useful_score=useful,
            comment=(request.form.get("comment") or "")[:1000],
        )
        db.session.add(fb)
        db.session.commit()
        write_audit("uat_feedback", f"ease={ease}, useful={useful}", org_id=user.org_id, user=user)
        flash("Thank you — your feedback was saved for evaluation.", "success")
        return redirect(url_for("uat_feedback"))
    return render_template("feedback.html")


@app.route("/privacy")
def privacy_policy():
    return render_template("privacy.html")


# ---------------------------------------------------------------- bootstrap
def ensure_super_admin():
    if not User.query.filter_by(role="super_admin").first():
        sa = User(name="Platform Admin", email="admin@platform.com", role="super_admin", is_active=True)
        sa.set_password("admin123")
        db.session.add(sa)
        db.session.commit()


def ensure_operations_category():
    """Add Operations Dataset category to already-approved orgs if missing."""
    orgs = Organization.query.filter_by(status="approved").all()
    for org in orgs:
        exists = KPICategory.query.filter_by(org_id=org.id, name="Operations Dataset").first()
        if not exists:
            db.session.add(KPICategory(org_id=org.id, name="Operations Dataset", is_default=True))
    db.session.commit()


with app.app_context():
    os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "uploads"), exist_ok=True)
    db.create_all()
    ensure_super_admin()
    ensure_operations_category()
    cleanup_stale_pending(BASE_DIR, max_age_hours=48)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
