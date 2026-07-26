# Polyclinic Reporting Platform (multi-organization)

A working Flask + SQLite system that lets multiple polyclinics register as
separate organizations, each with its own admin, staff, KPI categories, and
data — fixing the "everything tied to one test account" limitation from the
original system.

## How it works

1. **Register** — a polyclinic registers itself (organization name, region,
   admin account). Status starts as `pending`.
2. **Approve** — the platform admin (super admin) logs in, reviews pending
   organizations, and approves or rejects them. On approval, the org's admin
   account is activated and the organization is seeded with the default 14
   KPI categories.
3. **Org Admin** — logs in, adds staff accounts, and can add or remove KPI
   categories (organizations can have more or fewer than the default set).
4. **Staff** — can only enter data: upload Excel/CSV **or** enter rows
   manually. The system cleans contamination (blanks, bad formats,
   duplicates, inconsistent labels), shows a **review screen**, then staff
   confirm to save and can **download the clean dataset**.
5. **Org admin — reports** — after staff save cleaned data, the org admin
   generates a simple performance report (profit/loss, patient flow,
   compliance, diagnoses, demographics) and can **Print / Save as PDF**.
   Specialized reports still work for separate Revenue/Compliance uploads:

   - **Revenue and Expenses** (needs columns `department, type, actual,
     target` — type is "Revenue" or "Expense") → actual vs. target vs.
     recommended next-period target (8% growth assumption, adjustable in
     `report_engine.py`), % achieved, profit/loss, a bar chart, and — once
     more than one month has been uploaded in the same calendar year — a
     year-to-date trend with month-over-month growth % and cumulative
     revenue chart.
   - **Regulatory Compliance** (needs columns `certification_name,
     valid_until`) → days-before-expiry and status (Expired / Near Deadline
     / Valid) computed consistently from one rule, so a license can never
     show "Valid" while also showing negative days remaining.

6. **Dashboard** — renders all of the above per organization.

## Test data included

`test_data/` has ready-to-upload files with intentionally messy data so you
can see the auto-correction working:

- `Revenue_and_Expenses.xlsx`, `RevExp_2026-03.xlsx` … `RevExp_2026-06.xlsx`
  — four months of revenue/expense data (upload all four for the same
  categories to see the YTD trend and growth% appear); June's file also has
  a currency-formatted value (`"TZS 7,950"`) and a blank cell to show format
  correction in action.
- `Regulatory_Compliance.xlsx` — a mix of expired, near-deadline, and valid
  certifications to test the status logic (upload it with period
  2026-06-01 to 2026-06-30 to match the dates used when generating it).
- `Patient_Volume_and_Flow.xlsx`, `Clients_Age_Group.xlsx`,
  `Feedback_Experience.xlsx` — messy data (blanks, "N/A", inconsistent
  capitalization/whitespace, currency symbols) for the generic KPI
  categories, to test the general-purpose auto-correction.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

The app runs at http://127.0.0.1:5050

## Ethics & extras

- Uploads with personal patient columns (name, phone, ID, etc.) are blocked.
- Staff must confirm a privacy checkbox before upload/manual review.
- Audit log records upload, save, download, report, and suspend actions.
- Review screen shows a data quality score (0–100).
- Month presets fill period start/end; org admin sees in-app alerts.
- Reports can include a simple next-period forecast when enough history exists.
- UAT feedback form at `/feedback`; privacy page at `/privacy`.
- Stale unfinished review drafts are cleaned after 48 hours.

Platform admin (approves new organizations):
- Email: `admin@platform.com`
- Password: `admin123`

(Change this in `app.py` → `ensure_super_admin()` before any real deployment.)

## Notes / what to change before real deployment

- `SECRET_KEY` in `app.py` is a placeholder — set a real random secret.
- Uses SQLite for simplicity; for multiple concurrent users in production,
  moving to PostgreSQL/MySQL is recommended.
- Passwords are hashed (werkzeug), but there's no HTTPS/session hardening
  configured here — add that before going live.
- File uploads are stored as cleaned JSON in the database for simplicity;
  for large datasets you'd want a proper file store + background processing.
- The default 14 KPI categories mirror the ones from the original report
  (Revenue and Expenses, Patient Volume and Flow, etc.) — edit
  `DEFAULT_KPI_CATEGORIES` in `models.py` to change them.
