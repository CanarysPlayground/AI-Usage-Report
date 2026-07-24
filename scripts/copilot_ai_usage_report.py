#!/usr/bin/env python3
"""
GitHub Copilot AI Usage — Monthly Report Generator (v3)
----------------------------------------------------------
Produces a CSV with three sections:

  OVERALL METRICS
    Total Allocated Credits, Total AI Credits Used, Total Copilot
    Licensed Users, Total Unique Users, Total Transactions

  COST CENTER WISE AI CREDIT USAGE
    Cost Center, Total AI Credits, Unique Users, % of Total Credits

  MODEL WISE AI CREDIT USAGE
    Model Name, Total AI Credits, Transactions, % of Total Credits

WHERE MODEL-WISE DATA ACTUALLY COMES FROM
-------------------------------------------
The previous version tried the `premium_request` report export for model
data, on the theory that `detailed` has no `model` column (true) and
`premium_request` does (also true, per docs) — but `premium_request` is
legacy pre-AI-Credits terminology and appears to return empty/stale data
now that Copilot bills in AI Credits, which is why it kept falling back
to an unattributed total.

The model breakdown you see on the "AI usage" page grouped by Model is
actually served by a different, simpler endpoint — the same one Copilot's
billing UI itself uses:

  GET /enterprises/{enterprise}/settings/billing/ai_credit/usage
      ?year=...&month=...

This returns `usageItems`, each with a `model` field and quantities
(`grossQuantity`, `discountQuantity`, `netQuantity`) already aggregated
per model for the month — exactly matching the numbers shown in GitHub's
own "Usage breakdown" table grouped by model. This version uses that
endpoint for the MODEL WISE section instead. It's a single synchronous
call (no async export/poll needed), so it's also much faster than the
detailed/premium_request report flow.

Trade-off: this endpoint doesn't return a per-model transaction/event
count (only credit quantities), so the "Transactions" column in the
model-wise section is "N/A" — that number simply isn't exposed by any
documented API today. The COST CENTER WISE section still gets real
"Unique Users" and overall "Transactions" from the `detailed` report
export, which does carry that per-row detail (just not by model).

  POST /enterprises/{enterprise}/settings/billing/reports
       {"report_type": "detailed", "start_date": ..., "end_date": ...}
  GET  /enterprises/{enterprise}/settings/billing/reports/{id}   (poll)
  -> download_urls once status == "completed"

ALLOCATED CREDITS
------------------
GitHub doesn't expose the enterprise-wide included-credit pool under a
documented API field, so this is calculated from licensed seats per
"Usage-based billing for organizations and enterprises":
  https://docs.github.com/en/copilot/concepts/billing/usage-based-billing-for-organizations-and-enterprises

Standard included credits per seat/month: Business = 1,900, Enterprise =
3,900. BUT existing customers get a PROMOTIONAL amount for their first
three billing cycles under usage-based billing — June, July, and August
2026 (Business = 3,000, Enterprise = 7,000) — reverting to standard from
the September 2026 cycle onward. The script picks the right table based
on which month is being reported on, not the date the script is run.

LICENSED USERS
---------------
Seats are de-duplicated by assignee login before counting. The same
person can hold a seat record from more than one organization within an
enterprise, but GitHub only bills — and should only be counted — once
per unique user.

Requires a token (classic PAT with manage_billing:enterprise scope, or a
fine-grained PAT/GitHub App token with "Enterprise administration" write
permission) held by an enterprise admin or billing manager.

Usage:
    export GITHUB_TOKEN=ghp_xxx
    export GITHUB_ENTERPRISE=my-enterprise-slug
    python copilot_ai_usage_report.py
    python copilot_ai_usage_report.py --year 2026 --month 6
    python copilot_ai_usage_report.py --output report.csv
    python copilot_ai_usage_report.py --save-raw-dir ./raw   # keep both raw CSVs
"""

import argparse
import calendar
import csv
import datetime
import io
import os
import sys
import time

import requests

API_VERSION = "2026-03-10"
DEFAULT_BASE_URL = "https://api.github.com"


def base_url():
    return os.environ.get("GITHUB_API_BASE_URL", DEFAULT_BASE_URL)


def make_session(token):
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": API_VERSION,
    })
    return session


def previous_month(today=None):
    today = today or datetime.date.today()
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - datetime.timedelta(days=1)
    return last_day_prev_month.year, last_day_prev_month.month


def month_bounds(year, month):
    """
    Returns (start_date, end_date) for the requested month, clamped so
    end_date never goes past yesterday — GitHub's usage report export API
    rejects date ranges that reach into unfinalized/future data. Raises
    ValueError if the whole month is still in the future.
    """
    start = datetime.date(year, month, 1)
    natural_end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    end = min(natural_end, yesterday)
    if start > end:
        raise ValueError(
            f"No finalized data yet for {year}-{month:02d}: GitHub's usage "
            f"data is only queryable through yesterday ({yesterday.isoformat()}), "
            f"and that's before this month even starts. Wait a day or two into "
            f"the month, or report on the previous month instead."
        )
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Step 1: request the "detailed" usage report export and wait for it
# ---------------------------------------------------------------------------

def create_usage_report(session, enterprise, start_date, end_date, report_type):
    url = f"{base_url()}/enterprises/{enterprise}/settings/billing/reports"
    resp = session.post(url, json={
        "report_type": report_type,
        "start_date": start_date,
        "end_date": end_date,
    })
    resp.raise_for_status()
    return resp.json()


def poll_usage_report(session, enterprise, report_id, timeout_seconds=600, interval_seconds=10):
    url = f"{base_url()}/enterprises/{enterprise}/settings/billing/reports/{report_id}"
    waited = 0
    while waited <= timeout_seconds:
        resp = session.get(url)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "completed":
            return data
        if status == "failed":
            raise RuntimeError(f"Usage report export {report_id} failed: {data}")
        print(f"  Report status: {status}, waiting {interval_seconds}s...", file=sys.stderr)
        time.sleep(interval_seconds)
        waited += interval_seconds
    raise TimeoutError(f"Usage report export {report_id} did not complete within {timeout_seconds}s")


def download_report_csv(download_url):
    # download_url is a pre-signed Azure Blob SAS URL (has its own st=/se=/sig=
    # query params for auth). Fetch it WITHOUT the GitHub "Authorization:
    # Bearer ..." header — sending both breaks Azure's SAS signature check
    # and returns 403.
    resp = requests.get(download_url)
    resp.raise_for_status()
    # requests falls back to ISO-8859-1 when the response has no explicit
    # charset, which silently mangles any non-ASCII bytes (this is the
    # same class of bug that caused the mojibake in the report title —
    # force UTF-8 here too rather than trusting the guess).
    resp.encoding = "utf-8"
    return resp.text


def fetch_report_csv(session, enterprise, report_type, start_date, end_date, save_raw_path=None):
    print(f"Requesting {report_type} usage report for {start_date} to {end_date}...")
    report = create_usage_report(session, enterprise, start_date, end_date, report_type)
    report = poll_usage_report(session, enterprise, report["id"])
    download_urls = report.get("download_urls") or []
    if not download_urls:
        raise RuntimeError(f"{report_type} report completed but no download_urls were returned.")
    print(f"Downloading {report_type} CSV...")
    csv_text = download_report_csv(download_urls[0])
    if save_raw_path:
        with open(save_raw_path, "w", newline="") as f:
            f.write(csv_text)
        print(f"Raw {report_type} CSV saved to {save_raw_path}")
    return csv_text


# ---------------------------------------------------------------------------
# Step 2: fetch licensed seats (full list, so we get each seat's plan_type),
# de-duplicated by assignee since the same person can appear via more than
# one organization within the enterprise but is billed/counted only once.
# ---------------------------------------------------------------------------

def fetch_all_seats(session, enterprise):
    seats = []
    url = f"{base_url()}/enterprises/{enterprise}/copilot/billing/seats"
    page = 1
    while True:
        resp = session.get(url, params={"per_page": 100, "page": page})
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("seats", [])
        seats.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return seats


def dedupe_seats_by_user(seats):
    """Keep one seat per unique assignee login (case-insensitive)."""
    seen = {}
    for seat in seats:
        assignee = seat.get("assignee") or {}
        login = (assignee.get("login") or "").strip().lower()
        key = login or f"__no_login_{id(seat)}"
        if key not in seen:
            seen[key] = seat
    return list(seen.values())


# ---------------------------------------------------------------------------
# Step 3: allocated / included AI credits, calculated from licensed seats
# ---------------------------------------------------------------------------

# Standard included credits per seat/month (docs.github.com/.../usage-based-billing...)
STANDARD_INCLUDED_CREDITS = {
    "business": 1900,
    "enterprise": 3900,
}

# Promotional amounts for EXISTING customers' first three billing cycles
# under usage-based billing: June 1 - September 1, 2026.
PROMO_INCLUDED_CREDITS = {
    "business": 3000,
    "enterprise": 7000,
}
PROMO_WINDOW_START = datetime.date(2026, 6, 1)   # inclusive
PROMO_WINDOW_END = datetime.date(2026, 9, 1)     # exclusive -> Sept cycle is standard


def normalize_plan_type(plan_type):
    return (plan_type or "").strip().lower().replace("+", "_plus").replace("-", "_")


def included_credits_table_for(year, month):
    cycle_start = datetime.date(year, month, 1)
    if PROMO_WINDOW_START <= cycle_start < PROMO_WINDOW_END:
        return PROMO_INCLUDED_CREDITS
    return STANDARD_INCLUDED_CREDITS


def compute_allocated_credits_from_seats(seats, year, month):
    """
    Sums each *unique* licensed seat's included AI credit allotment based
    on its plan_type and which billing cycle is being reported on.
    Returns (total_allocated_credits, total_unique_licensed_users,
    unrecognized_plan_types_seen).
    """
    unique_seats = dedupe_seats_by_user(seats)
    table = included_credits_table_for(year, month)

    total = 0
    unrecognized = set()
    for seat in unique_seats:
        plan = normalize_plan_type(seat.get("plan_type"))
        if plan in table:
            total += table[plan]
        elif plan:
            unrecognized.add(plan)
    return total, len(unique_seats), unrecognized


# ---------------------------------------------------------------------------
# Step 4: parse the exported CSVs and aggregate
# ---------------------------------------------------------------------------

# Real export headers have been observed to vary slightly from the docs'
# naming — resolve each logical field against whatever headers actually
# showed up rather than hard-coding one exact key.
COLUMN_CANDIDATES = {
    "product": ["product"],
    "sku": ["sku"],
    "unit_type": ["unit_type", "unittype", "unit"],
    "quantity": ["quantity", "qty"],
    "cost_center_name": ["cost_center_name", "costcentername", "cost_center", "costcenter"],
    "username": ["username", "user", "login", "user_login"],
    "model": ["model", "model_name", "modelname", "ai_model"],
}


def _normalize_key(key):
    return (key or "").strip().lower().replace(" ", "_").replace("-", "_")


def resolve_columns(fieldnames):
    normalized = {_normalize_key(f): f for f in (fieldnames or [])}
    resolved = {}
    for logical_name, candidates in COLUMN_CANDIDATES.items():
        for candidate in candidates:
            if candidate in normalized:
                resolved[logical_name] = normalized[candidate]
                break
    return resolved


def is_relevant_row(row, cols):
    """
    True unless the row's own product/unit_type/sku fields actively say
    it's NOT Copilot AI credit usage. Reports that don't carry a
    product/unit_type column at all (e.g. premium_request) are assumed
    to already be scoped to AI credit consumption.
    """
    if "product" in cols:
        product = (row.get(cols["product"], "") or "").strip().lower()
        if product and product != "copilot":
            return False
    if "unit_type" in cols:
        unit_type = (row.get(cols["unit_type"], "") or "").strip().lower()
        sku = (row.get(cols.get("sku", ""), "") or "").strip().lower()
        if unit_type and unit_type != "credits" and "credit" not in sku:
            return False
    return True


def aggregate_detailed(csv_text):
    """From the `detailed` report: overall totals, cost-center breakdown."""
    reader = csv.DictReader(io.StringIO(csv_text))
    cols = resolve_columns(reader.fieldnames)

    missing = [f for f in ("quantity",) if f not in cols]
    if missing:
        raise RuntimeError(
            f"Couldn't find expected columns {missing} in the detailed export. "
            f"Actual headers were: {reader.fieldnames}."
        )

    rows = [r for r in reader if is_relevant_row(r, cols)]

    total_credits = 0.0
    all_users = set()
    cc_credits = {}
    cc_users = {}
    model_transactions = {}
    has_model_col = "model" in cols

    for r in rows:
        qty = float(r.get(cols["quantity"]) or 0)
        cc_name = (r.get(cols.get("cost_center_name", ""), "") or "").strip() or "(Not Assigned)"
        username = (r.get(cols.get("username", ""), "") or "").strip().lower()

        total_credits += qty
        if username:
            all_users.add(username)

        cc_credits[cc_name] = cc_credits.get(cc_name, 0.0) + qty
        if username:
            cc_users.setdefault(cc_name, set()).add(username)

        if has_model_col:
            model_name = (r.get(cols["model"], "") or "").strip() or "(No model)"
            model_transactions[model_name] = model_transactions.get(model_name, 0) + 1

    return {
        "total_credits": total_credits,
        "total_unique_users": len(all_users),
        "total_transactions": len(rows),
        # Credit totals per cost center from THIS export — kept only as a
        # cross-check against the authoritative ai_credit/usage numbers
        # used in the report; not used directly for the CSV output anymore.
        "cc_credits": cc_credits,
        "cc_users": {k: len(v) for k, v in cc_users.items()},
        # Only populated if the detailed export actually carries a `model`
        # column for this account/period; otherwise stays empty and the
        # report falls back to "N/A" for per-model transaction counts.
        "model_transactions": model_transactions,
        "headers": reader.fieldnames,
    }


def fetch_ai_credit_usage_items(session, enterprise, year, month):
    """
    The same endpoint behind GitHub's own 'AI usage' billing page — this is
    what that page queries whether you're looking at it grouped by Model
    or grouped by Cost Center; the raw usageItems carry both a `model`
    field and a cost-center field per line item. We fetch it once and
    aggregate it two ways (by model, by cost center) instead of trying to
    re-derive cost-center totals from the separate `detailed` export,
    which does its own independent (and not always identical) cost-center
    attribution. Using this endpoint for BOTH sections is what keeps the
    numbers matching exactly what's shown on the billing UI.
    """
    url = f"{base_url()}/enterprises/{enterprise}/settings/billing/ai_credit/usage"
    resp = session.get(url, params={"year": year, "month": month})
    resp.raise_for_status()
    return resp.json().get("usageItems", [])


# Real payloads have been observed to name the cost-center field a couple
# of different ways depending on API version — same defensive pattern as
# resolve_columns() below, just for JSON keys instead of CSV headers.
COST_CENTER_ITEM_KEY_CANDIDATES = ["costCenterName", "cost_center_name", "costCenter", "cost_center"]


def _item_cost_center_name(item):
    for key in COST_CENTER_ITEM_KEY_CANDIDATES:
        if key in item:
            name = (item.get(key) or "").strip()
            if name:
                return name
    return "(Not Assigned)"


def aggregate_model_usage(usage_items):
    """
    grossQuantity is the total AI credits consumed by that model for the
    period (included-pool usage + any additional/overage usage combined),
    matching the 'Included credits' + 'Additional credits' columns shown
    in the billing UI's per-model table. This endpoint doesn't expose a
    per-model transaction count directly, so the "Transactions" column is
    filled in later from the `detailed` export's own `model` column when
    that export happens to carry one (see aggregate_detailed) — falling
    back to "N/A" only if neither source has it.
    """
    model_credits = {}
    for item in usage_items:
        model = (item.get("model") or "").strip() or "(No model)"
        gross_qty = float(item.get("grossQuantity") or 0)
        model_credits[model] = model_credits.get(model, 0.0) + gross_qty
    return {"model_credits": model_credits}


def aggregate_cost_center_usage(usage_items):
    """
    Cost-center credit totals from the SAME authoritative endpoint as the
    model breakdown, so these numbers reconcile exactly with what's shown
    in GitHub's own 'Usage breakdown' UI grouped by Cost Center. This
    intentionally does NOT come from the `detailed` export — that export's
    per-row cost-center attribution can drift from this endpoint's (e.g. a
    user moved between cost centers mid-month), which is what caused a
    cost center's credit total to come out wrong previously. Unique-user
    counts still come from the `detailed` export separately, since this
    endpoint only returns pre-aggregated totals, not per-user rows.
    """
    cc_credits = {}
    for item in usage_items:
        cc_name = _item_cost_center_name(item)
        gross_qty = float(item.get("grossQuantity") or 0)
        cc_credits[cc_name] = cc_credits.get(cc_name, 0.0) + gross_qty
    return {"cc_credits": cc_credits}


# ---------------------------------------------------------------------------
# Step 5: write the three-section CSV
# ---------------------------------------------------------------------------

def write_report(output_path, detailed_agg, model_agg, cc_agg, total_licensed_users,
                  total_allocated_credits, year, month, unrecognized_plans=None):
    total_credits = detailed_agg["total_credits"]

    # utf-8-sig writes a BOM so Excel/Sheets correctly detect UTF-8 instead
    # of falling back to a legacy codepage (that fallback is what turned a
    # plain "-" into "â€"" in earlier reports whenever the title used a
    # non-ASCII dash) — kept even though the title below is now pure ASCII,
    # since cost center / model names could contain non-ASCII characters.
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        writer.writerow([f"Copilot AI Usage Report - {year}-{month:02d}"])
        writer.writerow([])

        writer.writerow(["OVERALL METRICS"])
        writer.writerow(["Total Allocated Credits", f"{total_allocated_credits:,.2f}"])
        writer.writerow(["Total AI Credits Used", f"{total_credits:,.2f}"])
        writer.writerow(["Total Copilot Licensed Users", total_licensed_users])
        writer.writerow(["Total Unique Users", detailed_agg["total_unique_users"]])
        writer.writerow(["Total Transactions", detailed_agg["total_transactions"]])
        if unrecognized_plans:
            writer.writerow([
                "  Note", f"seats with unrecognized plan_type excluded from allocation: {sorted(unrecognized_plans)}"
            ])
        writer.writerow([])

        # Credits here come from the ai_credit/usage endpoint (cc_agg) —
        # the same authoritative source as the billing UI's own "Usage
        # breakdown" table — not from the detailed export, so these totals
        # reconcile exactly with what you see on github.com. Unique Users
        # still comes from the detailed export, which is the only source
        # that has per-user rows.
        writer.writerow(["COST CENTER WISE AI CREDIT USAGE"])
        writer.writerow(["Cost Center", "Total AI Credits", "Unique Users", "% of Total Credits"])
        cc_credits = cc_agg["cc_credits"]
        cc_total_credits = sum(cc_credits.values())
        for cc_name in sorted(cc_credits, key=lambda k: -cc_credits[k]):
            credits_ = cc_credits[cc_name]
            pct = (credits_ / cc_total_credits * 100) if cc_total_credits else 0
            writer.writerow([cc_name, f"{credits_:,.2f}", detailed_agg["cc_users"].get(cc_name, 0), f"{pct:.2f}%"])
        writer.writerow(["TOTAL", f"{cc_total_credits:,.2f}", detailed_agg["total_unique_users"], "100.00%"])
        writer.writerow([])

        writer.writerow(["MODEL WISE AI CREDIT USAGE"])
        writer.writerow(["Model Name", "Total AI Credits", "Transactions", "% of Total Credits"])
        model_credits = model_agg["model_credits"]
        model_total_credits = sum(model_credits.values())
        pct_base = model_total_credits or total_credits
        model_transactions = detailed_agg.get("model_transactions") or {}
        total_model_transactions = sum(model_transactions.values()) if model_transactions else "N/A"
        for model in sorted(model_credits, key=lambda k: -model_credits[k]):
            credits_ = model_credits[model]
            pct = (credits_ / pct_base * 100) if pct_base else 0
            txns = model_transactions.get(model, "N/A") if model_transactions else "N/A"
            writer.writerow([model, f"{credits_:,.2f}", txns, f"{pct:.2f}%"])
        writer.writerow(["TOTAL", f"{model_total_credits:,.2f}", total_model_transactions, "100.00%"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enterprise", default=os.environ.get("GITHUB_ENTERPRISE"))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--output")
    parser.add_argument("--save-raw-dir", help="Directory to also save the raw detailed export CSV, for auditing")
    args = parser.parse_args()

    if not args.enterprise or not args.token:
        sys.exit("ERROR: --enterprise/--token (or GITHUB_ENTERPRISE/GITHUB_TOKEN env vars) are required.")

    year, month = (args.year, args.month) if args.year and args.month else previous_month()
    try:
        start_date, end_date = month_bounds(year, month)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")

    natural_month_end = datetime.date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    if end_date != natural_month_end:
        print(f"NOTE: {year}-{month:02d} isn't finished yet — reporting {start_date} to {end_date} "
              f"only (partial month).")

    session = make_session(args.token)

    if args.save_raw_dir:
        os.makedirs(args.save_raw_dir, exist_ok=True)
        detailed_raw_path = os.path.join(args.save_raw_dir, f"detailed_{year}-{month:02d}.csv")
    else:
        detailed_raw_path = None

    detailed_csv = fetch_report_csv(session, args.enterprise, "detailed", start_date, end_date, detailed_raw_path)
    detailed_agg = aggregate_detailed(detailed_csv)

    print("Fetching AI credit usage (model + cost center breakdowns)...")
    try:
        usage_items = fetch_ai_credit_usage_items(session, args.enterprise, year, month)
        model_agg = aggregate_model_usage(usage_items)
        cc_agg = aggregate_cost_center_usage(usage_items)
        if not model_agg["model_credits"]:
            print("  WARNING: ai_credit/usage returned no usageItems for this month.", file=sys.stderr)
        else:
            # Sanity check against the detailed export's own (independent)
            # cost-center attribution — a mismatch here just confirms the
            # two sources disagree for a given cost center, which is
            # expected occasionally; it doesn't mean either number is
            # wrong, just that ai_credit/usage (used in the report) is the
            # one that matches the billing UI.
            for cc_name, credits_ in cc_agg["cc_credits"].items():
                detailed_credits = detailed_agg["cc_credits"].get(cc_name)
                if detailed_credits is not None and abs(detailed_credits - credits_) > 0.01:
                    print(f"  NOTE: '{cc_name}' differs between sources — "
                          f"ai_credit/usage: {credits_:,.2f}, detailed export: {detailed_credits:,.2f}. "
                          f"Using ai_credit/usage (matches billing UI).", file=sys.stderr)
    except Exception as e:
        print(f"  WARNING: couldn't get ai_credit/usage breakdown ({e}). "
              f"Model and cost-center sections will fall back to the detailed export's own totals.",
              file=sys.stderr)
        model_agg = {"model_credits": {"(model breakdown unavailable)": detailed_agg["total_credits"]}}
        cc_agg = {"cc_credits": detailed_agg["cc_credits"]}

    print("Fetching licensed seats...")
    seats = fetch_all_seats(session, args.enterprise)
    total_allocated_credits, total_licensed_users, unrecognized_plans = \
        compute_allocated_credits_from_seats(seats, year, month)
    if unrecognized_plans:
        print(f"  WARNING: unrecognized plan_type(s) on some seats, excluded from allocation total: "
              f"{sorted(unrecognized_plans)}. Add them to STANDARD_INCLUDED_CREDITS / "
              f"PROMO_INCLUDED_CREDITS if they're valid Copilot plans.", file=sys.stderr)

    output_path = args.output or f"copilot_ai_usage_{year}-{month:02d}.csv"
    write_report(output_path, detailed_agg, model_agg, cc_agg, total_licensed_users,
                 total_allocated_credits, year, month, unrecognized_plans=unrecognized_plans)

    print(f"\nDone. Report written to {output_path}")
    print(f"  Total Allocated Credits: {total_allocated_credits:,.2f}")
    print(f"  Total AI Credits Used: {detailed_agg['total_credits']:,.2f}")
    print(f"  Total Copilot Licensed Users: {total_licensed_users}")
    print(f"  Total Unique Users: {detailed_agg['total_unique_users']}")
    print(f"  Total Transactions: {detailed_agg['total_transactions']}")


if __name__ == "__main__":
    main()
