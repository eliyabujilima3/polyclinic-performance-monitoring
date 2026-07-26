import io
import base64
from datetime import datetime

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

TEAL = "#1F5C6B"
MINT = "#3D8B7A"
CORAL = "#B85C38"
SLATE = "#243B4A"

# Default assumption for recommending next-period targets when no explicit
# target-growth policy is supplied. This is intentionally transparent and
# adjustable, rather than a hidden/opaque formula.
DEFAULT_GROWTH_ASSUMPTION = 0.08  # 8%


def _fig_to_base64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ---------------------------------------------------------------------------
# Revenue & Expenses: target vs actual, next-target, profit/loss
# ---------------------------------------------------------------------------
REQUIRED_REV_EXP_COLUMNS = {"department", "type", "actual", "target"}


def revenue_expense_columns_ok(columns):
    normalized = {c.strip().lower() for c in columns}
    return REQUIRED_REV_EXP_COLUMNS.issubset(normalized)


def build_revenue_expense_report(df, growth_assumption=DEFAULT_GROWTH_ASSUMPTION):
    """
    Expects columns (case-insensitive): department, type (Revenue/Expense), actual, target.
    Returns per-type breakdown, totals, profit/loss, and a bar chart (base64 PNG).
    """
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    df["type"] = df["type"].astype(str).str.strip().str.title()
    df["actual"] = pd.to_numeric(df["actual"], errors="coerce").fillna(0)
    df["target"] = pd.to_numeric(df["target"], errors="coerce").fillna(0)
    df["next_target"] = (df["target"] * (1 + growth_assumption)).round(2)
    df["pct_achieved"] = df.apply(
        lambda r: round(r["actual"] / r["target"] * 100, 2) if r["target"] else None, axis=1
    )

    sections = {}
    for t in ["Revenue", "Expense"]:
        sub = df[df["type"] == t]
        if sub.empty:
            continue
        rows = sub[["department", "actual", "target", "next_target", "pct_achieved"]].to_dict(orient="records")
        sections[t] = {
            "rows": rows,
            "totals": {
                "actual": round(sub["actual"].sum(), 2),
                "target": round(sub["target"].sum(), 2),
                "next_target": round(sub["next_target"].sum(), 2),
            },
        }

    revenue_total = sections.get("Revenue", {}).get("totals", {}).get("actual", 0)
    expense_total = sections.get("Expense", {}).get("totals", {}).get("actual", 0)
    profit = round(revenue_total - expense_total, 2)

    chart_b64 = None
    if "Revenue" in sections and sections["Revenue"]["rows"]:
        rows = sections["Revenue"]["rows"]
        depts = [r["department"] for r in rows]
        actual = [r["actual"] for r in rows]
        target = [r["target"] for r in rows]
        fig, ax = plt.subplots(figsize=(7.5, 4))
        x = range(len(depts))
        ax.bar([i - 0.2 for i in x], actual, width=0.4, label="Actual", color=TEAL)
        ax.bar([i + 0.2 for i in x], target, width=0.4, label="Target", color=MINT)
        ax.set_xticks(list(x))
        ax.set_xticklabels(depts, rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("Amount (TZS)")
        ax.set_title("Revenue: Actual vs Target by Department")
        ax.legend()
        fig.tight_layout()
        chart_b64 = _fig_to_base64(fig)

    return {
        "sections": sections,
        "revenue_total": revenue_total,
        "expense_total": expense_total,
        "profit": profit,
        "growth_assumption_pct": round(growth_assumption * 100, 1),
        "chart_b64": chart_b64,
    }


def build_trend_report(history):
    """
    history: list of {period_label, revenue_total, expense_total}, sorted ascending by period.
    Returns month-over-month growth %, YTD cumulative revenue, and two charts.
    """
    if not history:
        return None

    labels = [h["period_label"] for h in history]
    revenue = [h["revenue_total"] for h in history]
    expense = [h["expense_total"] for h in history]

    growth = [0.0]
    for i in range(1, len(revenue)):
        prev = revenue[i - 1]
        growth.append(round(((revenue[i] - prev) / prev * 100), 2) if prev else 0.0)

    ytd, running = [], 0.0
    for r in revenue:
        running += r
        ytd.append(round(running, 2))

    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    ax.plot(labels, revenue, marker="o", color=TEAL, label="Revenue")
    ax.plot(labels, expense, marker="o", color=CORAL, label="Expenses")
    ax.set_title("Monthly Revenue vs Expenses")
    ax.set_ylabel("Amount (TZS)")
    ax.legend()
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    trend_chart = _fig_to_base64(fig)

    fig2, ax2 = plt.subplots(figsize=(7.5, 3.2))
    ax2.fill_between(labels, ytd, color=MINT, alpha=0.3)
    ax2.plot(labels, ytd, marker="o", color=TEAL)
    ax2.set_title("YTD Revenue Trend")
    ax2.set_ylabel("Cumulative (TZS)")
    plt.setp(ax2.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig2.tight_layout()
    ytd_chart = _fig_to_base64(fig2)

    return {
        "labels": labels,
        "revenue": revenue,
        "expense": expense,
        "growth_pct": growth,
        "ytd": ytd,
        "trend_chart_b64": trend_chart,
        "ytd_chart_b64": ytd_chart,
    }


# ---------------------------------------------------------------------------
# Regulatory Compliance: expiry tracking with consistent status logic
# ---------------------------------------------------------------------------
REQUIRED_COMPLIANCE_COLUMNS = {"certification_name", "valid_until"}


def compliance_columns_ok(columns):
    normalized = {c.strip().lower() for c in columns}
    return REQUIRED_COMPLIANCE_COLUMNS.issubset(normalized)


def build_compliance_report(df, report_date=None):
    """
    Expects columns (case-insensitive): certification_name, valid_until (date).
    Status is derived ONLY from days-before-expiry, so it can never contradict
    itself the way "Valid" with -201 days did in the original report.
    """
    df = df.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    report_date = report_date or datetime.today()
    df["valid_until"] = pd.to_datetime(df["valid_until"], errors="coerce")
    df["days_before_expiry"] = (df["valid_until"] - pd.Timestamp(report_date)).dt.days

    def classify(days):
        if pd.isna(days):
            return "Unknown"
        if days < 0:
            return "Expired"
        if days <= 60:
            return "Near Deadline"
        return "Valid"

    df["status"] = df["days_before_expiry"].apply(classify)
    out = df[["certification_name", "valid_until", "days_before_expiry", "status"]].copy()
    out["valid_until"] = out["valid_until"].dt.strftime("%d-%b-%Y")
    counts = df["status"].value_counts().to_dict()
    return {"rows": out.to_dict(orient="records"), "counts": counts, "report_date": report_date.strftime("%d-%b-%Y")}


# ---------------------------------------------------------------------------
# Simple combined operations report (Book1-style wide dataset)
# ---------------------------------------------------------------------------
def _freq_table(series, limit=8):
    """Frequency table with share percentages (like June_ReportsHub.pdf summaries)."""
    counts = series.astype(str).value_counts().head(limit)
    total = int(series.astype(str).value_counts().sum()) or 1
    rows = []
    for k, v in counts.items():
        rows.append({
            "label": k,
            "count": int(v),
            "pct": round(int(v) / total * 100, 1),
        })
    return rows


def _bar_chart(labels, values, title, ylabel="Count"):
    if not labels:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    ax.bar(labels, values, color=TEAL)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    return _fig_to_base64(fig)


def build_simple_operations_report(df):
    """
    Lightweight performance report from a cleaned Book1-style table.
    Sections stay close to June_ReportsHub.pdf but much simpler.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    lower = {c.lower(): c for c in df.columns}

    def col(name):
        return lower.get(name.lower())

    revenue_c = col("Revenue_TZS")
    expense_c = col("Expenses_TZS")
    dept_c = col("Department")
    patients_c = col("Patients_Seen") or col("Patient_Flow")
    wait_c = col("Waiting_Time_Min")
    diagnosis_c = col("Top_Diagnosis") or col("Top_Disease")
    compliance_c = col("Compliance_Status")
    occupation_c = col("Occupation")
    residence_c = col("Residence")
    age_c = col("Age_Group")
    feedback_c = col("Feedback_Source")
    experience_c = col("Experience_Rating")
    marketing_c = col("Marketing_Action")
    sponsorship_c = col("Sponsorship")
    survey_c = col("Survey_Response")
    bed_c = col("Bed_Occupancy_Rate")

    report = {"report_type": "operations_simple", "sections": {}}

    # Profit & loss + department revenue (with % shares like sample PDF)
    if revenue_c and expense_c:
        rev = pd.to_numeric(df[revenue_c], errors="coerce").fillna(0)
        exp = pd.to_numeric(df[expense_c], errors="coerce").fillna(0)
        revenue_total = round(float(rev.sum()), 2)
        expense_total = round(float(exp.sum()), 2)
        profit = round(revenue_total - expense_total, 2)
        profit_margin_pct = round(profit / revenue_total * 100, 2) if revenue_total else None
        expense_ratio_pct = round(expense_total / revenue_total * 100, 2) if revenue_total else None
        dept_rows = []
        chart_b64 = None
        if dept_c:
            grouped = df.groupby(df[dept_c].astype(str)).agg(
                revenue=(revenue_c, lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()),
                expenses=(expense_c, lambda s: pd.to_numeric(s, errors="coerce").fillna(0).sum()),
            ).reset_index()
            grouped.columns = ["department", "revenue", "expenses"]
            grouped["profit"] = grouped["revenue"] - grouped["expenses"]
            grouped["revenue_share_pct"] = grouped["revenue"].apply(
                lambda v: round(float(v) / revenue_total * 100, 1) if revenue_total else 0
            )
            grouped["expense_share_pct"] = grouped["expenses"].apply(
                lambda v: round(float(v) / expense_total * 100, 1) if expense_total else 0
            )
            dept_rows = grouped.round(2).to_dict(orient="records")
            chart_b64 = _bar_chart(
                list(grouped["department"]),
                list(grouped["revenue"]),
                "Revenue by Department",
                "TZS",
            )
        report["sections"]["profit_loss"] = {
            "revenue_total": revenue_total,
            "expense_total": expense_total,
            "profit": profit,
            "profit_margin_pct": profit_margin_pct,
            "expense_ratio_pct": expense_ratio_pct,
            "by_department": dept_rows,
            "chart_b64": chart_b64,
        }

    # Patient volume & flow
    if patients_c or wait_c or bed_c:
        patients = pd.to_numeric(df[patients_c], errors="coerce") if patients_c else pd.Series(dtype=float)
        waits = pd.to_numeric(df[wait_c], errors="coerce") if wait_c else pd.Series(dtype=float)
        beds = pd.to_numeric(df[bed_c], errors="coerce") if bed_c else pd.Series(dtype=float)
        report["sections"]["patient_flow"] = {
            "total_patients": int(patients.fillna(0).sum()) if patients_c else 0,
            "avg_waiting_min": round(float(waits.mean()), 1) if wait_c and waits.notna().any() else None,
            "avg_bed_occupancy": round(float(beds.mean()), 2) if bed_c and beds.notna().any() else None,
            "records": int(len(df)),
        }

    # Compliance snapshot
    if compliance_c:
        rows = _freq_table(df[compliance_c])
        report["sections"]["compliance"] = {
            "rows": rows,
            "chart_b64": _bar_chart([i["label"] for i in rows], [i["count"] for i in rows], "Compliance Status"),
        }

    # Top diagnoses
    if diagnosis_c:
        rows = _freq_table(df[diagnosis_c])
        report["sections"]["diagnoses"] = {
            "rows": rows,
            "chart_b64": _bar_chart([i["label"] for i in rows], [i["count"] for i in rows], "Top Diagnoses"),
        }

    # Simple categorical summaries
    for key, column in [
        ("occupation", occupation_c),
        ("residence", residence_c),
        ("age_group", age_c),
        ("feedback_source", feedback_c),
        ("marketing", marketing_c),
        ("sponsorship", sponsorship_c),
        ("survey", survey_c),
    ]:
        if column:
            report["sections"][key] = {"rows": _freq_table(df[column])}

    if experience_c:
        ratings = pd.to_numeric(df[experience_c], errors="coerce").dropna()
        report["sections"]["experience"] = {
            "average": round(float(ratings.mean()), 2) if len(ratings) else None,
            "rows": _freq_table(df[experience_c].astype(str)),
        }

    return report


def compare_against_targets(operations_summary, targets):
    """
    targets: list of dicts with department, target_type, amount
    Attaches actual vs target/budget comparison onto operations_summary.
    """
    if not operations_summary or operations_summary.get("report_type") != "operations_simple":
        return operations_summary

    pl = (operations_summary.get("sections") or {}).get("profit_loss") or {}
    by_dept = {str(r["department"]).strip().lower(): r for r in pl.get("by_department") or []}

    revenue_rows, expense_rows = [], []
    for t in targets:
        dept = str(t["department"]).strip()
        actual_row = by_dept.get(dept.lower(), {})
        amount = float(t["amount"])
        if t["target_type"] == "revenue_target":
            actual = float(actual_row.get("revenue", 0) or 0)
            pct = round(actual / amount * 100, 2) if amount else None
            revenue_rows.append({
                "department": dept,
                "target": amount,
                "actual": actual,
                "pct_achieved": pct,
                "variance": round(actual - amount, 2),
            })
        elif t["target_type"] == "expense_budget":
            actual = float(actual_row.get("expenses", 0) or 0)
            pct = round(actual / amount * 100, 2) if amount else None
            expense_rows.append({
                "department": dept,
                "budget": amount,
                "actual": actual,
                "pct_used": pct,
                "variance": round(actual - amount, 2),
            })

    # Next-period recommended target/budget (sample PDF used ~8% uplift)
    for row in revenue_rows:
        row["next_target"] = round(row["target"] * (1 + DEFAULT_GROWTH_ASSUMPTION), 2)
    for row in expense_rows:
        row["next_budget"] = round(row["budget"] * (1 + DEFAULT_GROWTH_ASSUMPTION), 2)

    if revenue_rows or expense_rows:
        operations_summary.setdefault("sections", {})["targets"] = {
            "revenue_rows": revenue_rows,
            "expense_rows": expense_rows,
            "growth_assumption_pct": round(DEFAULT_GROWTH_ASSUMPTION * 100, 1),
        }
    return operations_summary


def build_operations_trend(history):
    """
    PDF-style monthly trend: MoM growth %, YTD cumulative, profit/loss per month.
    history: [{period_label, revenue_total, expense_total, patients}]
    """
    if not history:
        return None

    labels = [h["period_label"] for h in history]
    revenue = [float(h.get("revenue_total") or 0) for h in history]
    expense = [float(h.get("expense_total") or 0) for h in history]
    patients = [float(h.get("patients") or 0) for h in history]
    profit = [round(r - e, 2) for r, e in zip(revenue, expense)]

    growth = [0.0]
    for i in range(1, len(revenue)):
        prev = revenue[i - 1]
        growth.append(round(((revenue[i] - prev) / prev * 100), 2) if prev else 0.0)

    ytd, running = [], 0.0
    for r in revenue:
        running += r
        ytd.append(round(running, 2))

    expense_ytd, erun = [], 0.0
    for e in expense:
        erun += e
        expense_ytd.append(round(erun, 2))

    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    ax.plot(labels, revenue, marker="o", color=TEAL, label="Revenue")
    ax.plot(labels, expense, marker="o", color=CORAL, label="Expenses")
    ax.set_title("Monthly Revenue vs Expenses")
    ax.set_ylabel("Amount (TZS)")
    ax.legend()
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig.tight_layout()
    trend_chart = _fig_to_base64(fig)

    fig2, ax2 = plt.subplots(figsize=(7.5, 3.2))
    ax2.fill_between(labels, ytd, color=MINT, alpha=0.3)
    ax2.plot(labels, ytd, marker="o", color=TEAL)
    ax2.set_title("YTD Revenue Trend")
    ax2.set_ylabel("Cumulative (TZS)")
    plt.setp(ax2.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    fig2.tight_layout()
    ytd_chart = _fig_to_base64(fig2)

    rows = []
    for i, label in enumerate(labels):
        rows.append({
            "period": label,
            "revenue": revenue[i],
            "expenses": expense[i],
            "profit": profit[i],
            "growth_pct": growth[i],
            "ytd_revenue": ytd[i],
            "ytd_expenses": expense_ytd[i],
            "patients": patients[i],
        })

    return {
        "rows": rows,
        "trend_chart_b64": trend_chart,
        "ytd_chart_b64": ytd_chart,
    }


def build_simple_forecast(history):
    """
    history: list of {period_label, revenue_total, expense_total, patients} sorted ascending.
    Simple next-period forecast using average month-over-month growth (capped).
    """
    if not history or len(history) < 2:
        return None

    revenues = [float(h.get("revenue_total") or 0) for h in history]
    expenses = [float(h.get("expense_total") or 0) for h in history]
    patients = [float(h.get("patients") or 0) for h in history]

    def avg_growth(series):
        grows = []
        for i in range(1, len(series)):
            prev = series[i - 1]
            if prev:
                grows.append((series[i] - prev) / prev)
        if not grows:
            return 0.0
        g = sum(grows) / len(grows)
        return max(-0.25, min(0.25, g))  # cap ±25%

    g_rev = avg_growth(revenues)
    g_exp = avg_growth(expenses)
    g_pat = avg_growth(patients) if any(patients) else 0.0
    last = history[-1]
    return {
        "based_on_periods": len(history),
        "next_revenue": round(revenues[-1] * (1 + g_rev), 2),
        "next_expenses": round(expenses[-1] * (1 + g_exp), 2),
        "next_patients": round(patients[-1] * (1 + g_pat), 0) if any(patients) else None,
        "revenue_growth_pct": round(g_rev * 100, 1),
        "expense_growth_pct": round(g_exp * 100, 1),
        "last_period": last.get("period_label"),
        "note": "Simple average growth forecast (±25% cap). For planning only — not a clinical prediction.",
    }
