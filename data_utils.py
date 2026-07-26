import json
import os
import re

import pandas as pd

NULL_LIKE = {"", "n/a", "na", "n.a", "none", "null", "unknown", "-", "--", "?", "nil", "not available"}
SMALL_WORDS = {"and", "or", "of", "the", "in", "for", "on", "at", "to", "a", "an"}

RATING_MAP = {
    "excellent": 5,
    "very good": 5,
    "good": 4,
    "average": 3,
    "fair": 3,
    "poor": 1,
    "bad": 1,
    "terrible": 1,
}

WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100, "thousand": 1000,
}

# Columns where negative values are almost always data-entry mistakes
POSITIVE_COUNT_HINTS = (
    "patient", "flow", "waiting", "revenue", "expense", "occupancy", "count", "volume",
)


def words_to_number(text):
    """
    Convert simple English number phrases to float.
    Examples: "eighty" → 80, "one hundred" → 100, "two hundred fifty" → 250.
    """
    s = re.sub(r"[^a-z\s\-]", " ", str(text).lower()).strip()
    s = s.replace("-", " ")
    parts = [p for p in s.split() if p and p not in {"and", "a"}]
    if not parts:
        return None
    if any(p not in WORD_NUMBERS for p in parts):
        return None

    total = 0
    current = 0
    for part in parts:
        val = WORD_NUMBERS[part]
        if val == 100:
            current = (current or 1) * 100
        elif val == 1000:
            current = (current or 1) * 1000
            total += current
            current = 0
        else:
            current += val
    return float(total + current)


def parse_messy_number(v):
    """Recover a number from messy values (currency, commas, word-numbers)."""
    if pd.isna(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).strip()
    if s.lower() in NULL_LIKE:
        return None
    if s.lower() in RATING_MAP:
        return float(RATING_MAP[s.lower()])

    # Try phrase word-numbers first: "one hundred", "eighty", etc.
    as_words = words_to_number(s)
    if as_words is not None and re.search(r"[a-zA-Z]", s):
        return as_words

    cleaned = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def should_force_positive(col_name):
    name = col_name.lower().replace(" ", "_")
    return any(hint in name for hint in POSITIVE_COUNT_HINTS)


OPERATIONS_COLUMNS = [
    "Record_ID", "Date", "Department", "Revenue_TZS", "Expenses_TZS",
    "Compliance_Status", "Patients_Seen", "Waiting_Time_Min", "Top_Diagnosis",
    "Occupation", "Residence", "Age_Group", "Feedback_Source",
    "Experience_Rating", "Marketing_Action", "Sponsorship", "Survey_Response",
    "Bed_Occupancy_Rate",
]

OPERATIONS_MARKERS = {"department", "revenue_tzs", "expenses_tzs", "patients_seen"}


def smart_title(s):
    words = s.split(" ")
    out = []
    for i, w in enumerate(words):
        if i > 0 and w.lower() in SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:].lower() if w else w)
    return " ".join(out)


def read_any(file_storage, filename):
    """Read an uploaded xlsx/xls/csv file into a DataFrame."""
    name = filename.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(file_storage)
    else:
        df = pd.read_excel(file_storage)
    return df


def is_null_like(v):
    if pd.isna(v):
        return True
    if isinstance(v, str) and v.strip().lower() in NULL_LIKE:
        return True
    return False


def is_numeric_series(s):
    parsed = s.map(parse_messy_number)
    non_null = s.map(lambda v: not is_null_like(v))
    if non_null.sum() == 0:
        return False
    return parsed[non_null].notna().mean() > 0.5


def is_operations_dataframe(df: pd.DataFrame) -> bool:
    cols = {str(c).strip().lower() for c in df.columns}
    return OPERATIONS_MARKERS.issubset(cols)


def clean_and_correct(df: pd.DataFrame):
    """
    Detect blanks and incorrect formats, then auto-correct:
      - revenue/expenses missing or junk → filled with column median
      - other numeric missing/junk → filled with column mean
      - categorical missing → filled with mode (most common value)
      - negative money / patient / waiting values → absolute (positive)
      - word numbers like "eighty" → 80 (bed occupancy then becomes 0.80)
    Also drops empty rows and exact duplicate rows.
    """
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    notes = []
    before = len(df)
    df = df.dropna(how="all")
    dropped_empty = before - len(df)
    if dropped_empty:
        notes.append(f"Removed {dropped_empty} empty row(s).")

    before = len(df)
    df = df.drop_duplicates()
    dropped_dupes = before - len(df)
    if dropped_dupes:
        notes.append(f"Removed {dropped_dupes} duplicate row(s).")

    total_rows = len(df)
    missing_report = {}
    corrections = {}

    for col in df.columns:
        col_l = col.lower()

        # Date columns: coerce; invalid dates become missing then filled with mode date
        if col_l == "date" or "date" in col_l:
            original = df[col]
            parsed = pd.to_datetime(original, errors="coerce")
            bad_count = int(parsed.isna().sum())
            missing_report[col] = bad_count
            if parsed.notna().any():
                fill_value = parsed.dropna().mode().iloc[0]
                df[col] = parsed.fillna(fill_value).dt.strftime("%Y-%m-%d")
            else:
                df[col] = parsed
            if bad_count:
                corrections[col] = {
                    "method": "invalid/missing dates set to most common date",
                    "value": str(df[col].mode().iloc[0]) if len(df[col].dropna()) else "",
                    "count": bad_count,
                    "reformatted_count": int((~original.map(is_null_like) & parsed.notna()).sum()),
                }
            continue

        if is_numeric_series(df[col]):
            parsed = df[col].map(parse_messy_number)

            # Negative values in money / patient-flow style columns → positive
            if should_force_positive(col):
                neg_mask = parsed.notna() & (parsed < 0)
                if neg_mask.any():
                    parsed.loc[neg_mask] = parsed.loc[neg_mask].abs()
                    notes.append(
                        f"{col}: converted {int(neg_mask.sum())} negative value(s) to positive."
                    )

            if col_l == "experience_rating":
                over = parsed.notna() & (parsed > 5)
                under = parsed.notna() & (parsed < 1)
                if over.any() or under.any():
                    parsed = parsed.clip(lower=1, upper=5)
                    notes.append(f"{col}: clipped ratings to 1–5.")

            if col_l == "bed_occupancy_rate":
                # "eighty" → 80, or typed 87 meaning 87% → convert to rate 0.80 / 0.87
                percent_mask = parsed.notna() & (parsed > 1.5) & (parsed <= 100)
                if percent_mask.any():
                    parsed.loc[percent_mask] = parsed.loc[percent_mask] / 100.0
                    notes.append(
                        f"{col}: converted {int(percent_mask.sum())} percent-style value(s) "
                        f"(e.g. eighty/80 → 0.80)."
                    )

            blank_or_bad = int(parsed.isna().sum())
            recovered_mask = (
                parsed.notna()
                & df[col].map(is_null_like).eq(False)
                & df[col].map(lambda v: not isinstance(v, (int, float)) or isinstance(v, bool))
            )
            recovered_count = int(recovered_mask.sum())
            missing_report[col] = blank_or_bad

            # Money columns: median is safer when data has outliers / corruption.
            # Other numeric columns: mean (simple average).
            money_cols = {"revenue_tzs", "expenses_tzs"}
            if parsed.notna().any():
                if col_l in money_cols:
                    fill_value = round(float(parsed.median()), 2)
                    fill_method = "filled with column median"
                else:
                    fill_value = round(float(parsed.mean()), 2)
                    fill_method = "filled with column mean"
            else:
                fill_value = 0
                fill_method = "filled with 0 (no valid numbers found)"

            df[col] = parsed.fillna(fill_value)

            if blank_or_bad > 0 or recovered_count > 0:
                corrections[col] = {
                    "method": fill_method,
                    "value": fill_value,
                    "count": blank_or_bad,
                    "reformatted_count": recovered_count,
                }
        else:
            # "unknown" is a real compliance label, not a blank
            allow_unknown = col_l in {"compliance_status"}

            def normalize(v):
                if pd.isna(v):
                    return pd.NA
                if isinstance(v, str) and v.strip().lower() == "unknown" and allow_unknown:
                    return "Unknown"
                if is_null_like(v):
                    return pd.NA
                return smart_title(" ".join(str(v).strip().split()))

            original = df[col]
            normalized = original.map(normalize)

            # Normalize common compliance labels
            if col_l == "compliance_status":
                def fix_compliance(v):
                    if pd.isna(v):
                        return v
                    key = str(v).strip().lower().replace("_", "-").replace(" ", "-")
                    mapping = {
                        "compliant": "Compliant",
                        "non-compliant": "Non-Compliant",
                        "noncompliant": "Non-Compliant",
                        "pending": "Pending",
                        "unknown": "Unknown",
                    }
                    return mapping.get(key, smart_title(str(v)))
                normalized = normalized.map(fix_compliance)

            if col_l == "survey_response":
                def fix_yes_no(v):
                    if pd.isna(v):
                        return v
                    key = str(v).strip().lower()
                    if key in {"yes", "y", "true"}:
                        return "Yes"
                    if key in {"no", "n", "false"}:
                        return "No"
                    if key in {"maybe", "unknown"}:
                        return "Maybe"
                    return smart_title(str(v))
                normalized = normalized.map(fix_yes_no)

            changed_mask = normalized.astype(str) != original.astype(str)
            reformatted_count = int((changed_mask & normalized.notna()).sum())
            blank_count = int(normalized.isna().sum())
            missing_report[col] = blank_count
            mode_series = normalized.dropna()
            fill_value = mode_series.mode().iloc[0] if not mode_series.mode().empty else "Unknown"
            df[col] = normalized.fillna(fill_value)

            if blank_count > 0 or reformatted_count > 0:
                corrections[col] = {
                    "method": "filled with most common value",
                    "value": str(fill_value),
                    "count": blank_count,
                    "reformatted_count": reformatted_count,
                }

    if notes:
        corrections["_notes"] = {"method": "; ".join(notes), "value": "", "count": 0, "reformatted_count": 0}

    return df, missing_report, corrections, total_rows


def detect_column_types(df: pd.DataFrame):
    numeric_cols, categorical_cols = [], []
    for col in df.columns:
        if is_numeric_series(df[col]):
            numeric_cols.append(col)
        else:
            uniq_ratio = df[col].nunique() / max(len(df), 1)
            if uniq_ratio < 0.9:
                categorical_cols.append(col)
    return numeric_cols, categorical_cols


def summarize_dataset(df: pd.DataFrame):
    """Produce a JSON-serializable summary: numeric stats + top categorical values."""
    numeric_cols, categorical_cols = detect_column_types(df)
    summary = {"row_count": len(df), "numeric": {}, "categorical": {}}

    for col in numeric_cols:
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s):
            summary["numeric"][col] = {
                "mean": round(float(s.mean()), 2),
                "min": round(float(s.min()), 2),
                "max": round(float(s.max()), 2),
                "sum": round(float(s.sum()), 2),
                "count": int(len(s)),
            }

    for col in categorical_cols:
        counts = df[col].astype(str).value_counts().head(8)
        summary["categorical"][col] = [{"label": k, "count": int(v)} for k, v in counts.items()]

    return summary


def pending_path(base_dir, user_id):
    folder = os.path.join(base_dir, "uploads")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"pending_{user_id}.json")


def save_pending(base_dir, user_id, payload):
    path = pending_path(base_dir, user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


def load_pending(base_dir, user_id):
    path = pending_path(base_dir, user_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def clear_pending(base_dir, user_id):
    path = pending_path(base_dir, user_id)
    if os.path.exists(path):
        os.remove(path)


def records_preview(records, limit=12):
    return records[:limit]


# ---------------------------------------------------------------------------
# Ethics: block columns that look like personal patient identifiers
# ---------------------------------------------------------------------------
FORBIDDEN_EXACT = {
    "name", "patient_name", "client_name", "full_name", "firstname", "first_name",
    "lastname", "last_name", "surname", "phone", "mobile", "telephone", "email",
    "national_id", "nida", "passport", "mrn", "patient_id", "address", "dob",
    "date_of_birth", "birth_date", "next_of_kin", "fingerprint", "photo",
    "nhif_number", "insurance_number", "id_number", "file_number", "file_no",
}

FORBIDDEN_SUBSTRINGS = (
    "patient_name", "client_name", "full_name", "phone_number", "mobile_number",
    "email_address", "national_id", "passport", "medical_record", "date_of_birth",
    "next_of_kin", "fingerprint", "nhif_number", "insurance_number", "home_address",
    "patient_phone", "patient_email", "patient_id",
)


def find_forbidden_columns(columns):
    """Return column names that look like personal patient identifiers."""
    banned = []
    for col in columns:
        key = re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")
        if key in FORBIDDEN_EXACT or any(s in key for s in FORBIDDEN_SUBSTRINGS):
            banned.append(str(col))
    return banned


def compute_quality_score(row_count, missing_count, corrections):
    """
    0–100 score: higher = cleaner original data.
    Penalize cells that needed fill/fix relative to rows×columns.
    """
    if row_count <= 0:
        return 0.0
    corr = {k: v for k, v in (corrections or {}).items() if not str(k).startswith("_")}
    col_count = max(len(corr), 1)
    issue_cells = int(missing_count or 0)
    for detail in corr.values():
        if isinstance(detail, dict):
            issue_cells += int(detail.get("reformatted_count") or 0)
    capacity = max(row_count * max(col_count, 1), 1)
    ratio = min(issue_cells / capacity, 1.0)
    score = round(100.0 * (1.0 - ratio), 1)
    return max(0.0, min(100.0, score))


def month_period_options(months_back=11):
    """List of {label, start, end} for recent calendar months including current."""
    today = pd.Timestamp.today().normalize()
    options = []
    for i in range(months_back + 1):
        d = (today.replace(day=1) - pd.DateOffset(months=i))
        start = d.replace(day=1)
        end = (start + pd.offsets.MonthEnd(0))
        options.append({
            "label": start.strftime("%B %Y"),
            "start": start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
        })
    return options


def cleanup_stale_pending(base_dir, max_age_hours=48):
    """Ethics/retention: remove unfinished review drafts older than max_age_hours."""
    folder = os.path.join(base_dir, "uploads")
    if not os.path.isdir(folder):
        return 0
    import time
    removed = 0
    now = time.time()
    for name in os.listdir(folder):
        if not name.startswith("pending_") or not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            age_h = (now - os.path.getmtime(path)) / 3600.0
            if age_h > max_age_hours:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed
