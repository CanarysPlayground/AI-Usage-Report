#!/usr/bin/env python3
"""
GitHub Copilot AI Usage — Monthly Report Generator (v5)
----------------------------------------------------------
Produces a CSV with four sections:

  OVERALL METRICS
    Total Allocated Credits, Total AI Credits Used, Total Copilot
    Licensed Users, Total Unique Users

  COST CENTER WISE AI CREDIT USAGE
    Cost Center, Total AI Credits, Additional AI Credits, Unique Users,
    % of Total Credits

  MODEL WISE AI CREDIT USAGE
    Model Name, Total AI Credits, Additional AI Credits, % of Total Credits

  COST CENTER AND AI MODEL WISE AI CREDIT USAGE
    Cost Center, Model Name, Total AI Credits, Additional AI Credits,
    % of Total Credits

WHERE DATA ACTUALLY COMES FROM
-------------------------------
AI credit quantities for both the COST CENTER WISE and MODEL WISE sections
come from the same endpoint GitHub's own billing UI uses:

  GET /enterprises/{enterprise}/settings/billing/ai_credit/usage
      ?year=...&month=...&cost_center_id=<id>   # per cost center (looped over ALL cost centers)
      ?year=...&month=...&cost_center_id=none   # usage not assigned to any cost center

Only cost centers with actual usage (grossQuantity > 0) this period are
included in the report. Cost centers that exist but show zero usage --
whether because they are stale, inactive, or their activity predates the
reporting window -- are silently dropped, so every row in the report
reflects real consumption.

The enterprise-wide total and the MODEL WISE breakdown are built by
SUMMING every one of those per-bucket calls, rather than by making a
separate unfiltered call to the same endpoint. GitHub's billing docs note
that for these usage endpoints, omitting the cost_center_id filter
returns only usage NOT belonging to any cost center -- not a grand total
-- so relying on an unfiltered call for "everything" would silently
under-report both the overall total and the model breakdown. Building
totals by summing the per-bucket calls avoids depending on that
undocumented behavior, and guarantees the COST CENTER WISE total, the
MODEL WISE total, and "Total AI Credits Used" always reconcile exactly,
by construction.

Unique-users counts per cost center still come from the `detailed` report
export, which carries per-row user information:

  POST /enterprises/{enterprise}/settings/billing/reports
       {"report_type": "detailed", "start_date": ..., "end_date": ...}
  GET  /enterprises/{enterprise}/settings/billing/reports/{id}   (poll)
  -> download_urls once status == "completed"

Cost centers are listed via:

  GET /enterprises/{enterprise}/settings/billing/cost-centers?state=active

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

    return {
        "total_credits": total_credits,
        "total_unique_users": len(all_users),
        "total_transactions": len(rows),
        "cc_credits": {k: v for k, v in cc_credits.items() if v > 0},
        "cc_users": {k: len(v) for k, v in cc_users.items()},
        "headers": reader.fieldnames,
    }


def fetch_ai_credit_usage_by_model(session, enterprise, year, month, cost_center_id=None):
    """
    The same endpoint behind GitHub's own 'AI usage' billing page when
    grouped by Model. Returns usageItems already aggregated per model for
    the given month.

    Pass cost_center_id to filter to a specific cost center; pass
    cost_center_id="none" to get usage not associated with any cost center;
    omit (or pass None) to get enterprise-wide totals.
    """
    url = f"{base_url()}/enterprises/{enterprise}/settings/billing/ai_credit/usage"
    params = {"year": year, "month": month}
    if cost_center_id is not None:
        params["cost_center_id"] = cost_center_id
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json().get("usageItems", [])


def fetch_cost_centers(session, enterprise, active_only=True):
    """
    Returns cost centers. GitHub's documented response for this endpoint is
    a flat, unpaginated `costCenters` array; each object carries its own
    `state` field ("active" / "archived" etc.) rather than the endpoint
    supporting a `state=` query filter, so filtering is done client-side
    here instead of trusting an unverified query param. Deleted/archived
    cost centers are excluded by default (active_only=True) -- pass
    active_only=False to see everything, e.g. for debugging.
    """
    url = f"{base_url()}/enterprises/{enterprise}/settings/billing/cost-centers"
    all_cost_centers = []
    page = 1
    while True:
        resp = session.get(url, params={"per_page": 100, "page": page})
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("costCenters", [])
        all_cost_centers.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    if not active_only:
        return all_cost_centers

    active = [cc for cc in all_cost_centers if (cc.get("state") or "active").lower() == "active"]
    dropped = len(all_cost_centers) - len(active)
    if dropped:
        dropped_names = [cc.get("name") for cc in all_cost_centers if cc not in active]
        print(f"  Excluded {dropped} non-active cost center(s): {dropped_names}", file=sys.stderr)
    return active


def build_usage_by_bucket(session, enterprise, year, month, cost_centers):
    """
    Fetches AI credit usage per cost center via the ai_credit/usage endpoint
    (same source as GitHub's billing UI), PLUS a separate call for usage not
    associated with any cost center. Returns (cc_credits, cc_additional,
    model_credits, model_additional, cc_model_credits, cc_model_additional):

      cc_credits / model_credits: {name: total_credits} -- TOTAL AI credits
        consumed (included-pool usage + additional usage combined), i.e.
        each item's `grossQuantity`.

      cc_additional / model_additional: {name: additional_credits} -- just
        the portion billed BEYOND the included pool, i.e. each item's
        `netQuantity` (matches the billing UI's "Additional AI Credits"
        column; 0 for a cost center/model that stayed within its included
        allotment).

      cc_model_credits / cc_model_additional: {(cc_name, model): credits} --
        per-cost-center, per-model breakdown for the cross-tab section.

    Cost centers (and models) with zero total credits this period are
    dropped from the result entirely, rather than shown as a 0.00 row --
    a cost center with no activity this month (deleted, stale, or inactive)
    is excluded so the report only reflects real consumption.

    Built by summing every per-bucket call rather than trusting a single
    unfiltered call to ai_credit/usage as "the enterprise total" -- GitHub's
    billing docs state that omitting the cost_center_id filter on these
    usage endpoints returns only usage NOT belonging to any cost center,
    not a grand total. Summing every bucket explicitly avoids depending on
    that undocumented behavior, and guarantees the COST CENTER WISE and
    MODEL WISE totals reconcile by construction.

    Any single cost center's API call failing is caught and logged, rather
    than silently vanishing or aborting the whole report.
    """
    cc_credits, cc_additional = {}, {}
    model_credits, model_additional = {}, {}
    cc_model_credits, cc_model_additional = {}, {}

    def _accumulate(items, cc_name):
        total, additional = 0.0, 0.0
        for item in items:
            gross = float(item.get("grossQuantity") or 0)
            net = float(item.get("netQuantity") or 0)  # portion beyond the included pool
            total += gross
            additional += net
            if gross > 0:
                model = (item.get("model") or "").strip() or "(No model)"
                model_credits[model] = model_credits.get(model, 0.0) + gross
                model_additional[model] = model_additional.get(model, 0.0) + net
                key = (cc_name, model)
                cc_model_credits[key] = cc_model_credits.get(key, 0.0) + gross
                cc_model_additional[key] = cc_model_additional.get(key, 0.0) + net
        if total > 0:
            cc_credits[cc_name] = cc_credits.get(cc_name, 0.0) + total
            cc_additional[cc_name] = cc_additional.get(cc_name, 0.0) + additional

    for cc in cost_centers:
        cc_id = cc.get("id")
        cc_name = (cc.get("name") or "").strip() or "(Unnamed Cost Center)"
        try:
            items = fetch_ai_credit_usage_by_model(session, enterprise, year, month, cost_center_id=cc_id)
        except Exception as e:
            print(f"  WARNING: couldn't fetch usage for cost center '{cc_name}' (id={cc_id}): {e}. "
                  f"Excluded from the report -- investigate separately.", file=sys.stderr)
            continue
        _accumulate(items, cc_name)

    try:
        unassigned_items = fetch_ai_credit_usage_by_model(session, enterprise, year, month, cost_center_id="none")
    except Exception as e:
        print(f"  WARNING: couldn't fetch unassigned (no cost center) usage: {e}. "
              f"Treating it as 0.00 -- the enterprise total below may be understated.", file=sys.stderr)
        unassigned_items = []
    _accumulate(unassigned_items, "(Not Assigned)")

    print(f"  Cost centers checked: {len(cost_centers)}. Rows with usage in report: {len(cc_credits)}.")
    return cc_credits, cc_additional, model_credits, model_additional, cc_model_credits, cc_model_additional


# ---------------------------------------------------------------------------
# Step 5: write the four-section CSV
# ---------------------------------------------------------------------------

def write_report(output_path, detailed_agg, model_credits, model_additional, cc_api_credits, cc_additional,
                 enterprise_total, enterprise_additional, total_licensed_users, total_allocated_credits,
                 year, month, cc_model_credits=None, cc_model_additional=None, unrecognized_plans=None):
    cc_model_credits = cc_model_credits or {}
    cc_model_additional = cc_model_additional or {}

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        writer.writerow([f"Copilot AI Usage Report - {year}-{month:02d}"])
        writer.writerow([])

        writer.writerow(["OVERALL METRICS"])
        writer.writerow(["Total Allocated Credits", f"{total_allocated_credits:,.2f}"])
        writer.writerow(["Total AI Credits Used", f"{enterprise_total:,.2f}"])
        writer.writerow(["Total Additional AI Credits (beyond included pool)", f"{enterprise_additional:,.2f}"])
        writer.writerow(["Total Copilot Licensed Users", total_licensed_users])
        writer.writerow(["Total Unique Users", detailed_agg["total_unique_users"]])
        if unrecognized_plans:
            writer.writerow([
                "  Note", f"seats with unrecognized plan_type excluded from allocation: {sorted(unrecognized_plans)}"
            ])
        writer.writerow([])

        writer.writerow(["COST CENTER WISE AI CREDIT USAGE"])
        writer.writerow(["Cost Center", "Total AI Credits", "Additional AI Credits", "Unique Users", "% of Total Credits"])
        for cc_name in sorted(cc_api_credits, key=lambda k: -cc_api_credits[k]):
            credits_ = cc_api_credits[cc_name]
            additional_ = cc_additional.get(cc_name, 0.0)
            pct = (credits_ / enterprise_total * 100) if enterprise_total else 0
            writer.writerow([cc_name, f"{credits_:,.2f}", f"{additional_:,.2f}",
                              detailed_agg["cc_users"].get(cc_name, 0), f"{pct:.2f}%"])
        writer.writerow(["TOTAL", f"{enterprise_total:,.2f}", f"{enterprise_additional:,.2f}",
                          detailed_agg["total_unique_users"], "100.00%"])
        writer.writerow([])

        writer.writerow(["MODEL WISE AI CREDIT USAGE"])
        writer.writerow(["Model Name", "Total AI Credits", "Additional AI Credits", "% of Total Credits"])
        for model in sorted(model_credits, key=lambda k: -model_credits[k]):
            credits_ = model_credits[model]
            additional_ = model_additional.get(model, 0.0)
            pct = (credits_ / enterprise_total * 100) if enterprise_total else 0
            writer.writerow([model, f"{credits_:,.2f}", f"{additional_:,.2f}", f"{pct:.2f}%"])
        model_additional_total = sum(model_additional.values())
        writer.writerow(["TOTAL", f"{enterprise_total:,.2f}", f"{model_additional_total:,.2f}", "100.00%"])
        writer.writerow([])

        writer.writerow(["COST CENTER AND AI MODEL WISE AI CREDIT USAGE"])
        writer.writerow(["Cost Center", "Model Name", "Total AI Credits", "Additional AI Credits", "% of Total Credits"])
        for (cc_name, model) in sorted(cc_model_credits, key=lambda k: (-cc_model_credits[k], k[0], k[1])):
            credits_ = cc_model_credits[(cc_name, model)]
            additional_ = cc_model_additional.get((cc_name, model), 0.0)
            pct = (credits_ / enterprise_total * 100) if enterprise_total else 0
            writer.writerow([cc_name, model, f"{credits_:,.2f}", f"{additional_:,.2f}", f"{pct:.2f}%"])
        cc_model_additional_total = sum(cc_model_additional.values())
        writer.writerow(["TOTAL", "", f"{enterprise_total:,.2f}", f"{cc_model_additional_total:,.2f}", "100.00%"])


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

    # Detailed report: used for unique-user counts per cost center
    detailed_csv = fetch_report_csv(session, args.enterprise, "detailed", start_date, end_date, detailed_raw_path)
    detailed_agg = aggregate_detailed(detailed_csv)

    # Per-cost-center AND model credits (total + additional/overage) from
    # ai_credit/usage, built together from the same set of calls so every
    # section reconciles. Only active cost centers with real usage (> 0) this
    # period end up in the report; deleted or inactive cost centers with no
    # usage are silently excluded.
    print("Fetching cost centers...")
    try:
        cost_centers = fetch_cost_centers(session, args.enterprise)  # active_only=True by default
        print(f"  Found {len(cost_centers)} active cost center(s): {[c.get('name') for c in cost_centers]}")
        cc_api_credits, cc_additional, model_credits, model_additional, cc_model_credits, cc_model_additional = \
            build_usage_by_bucket(session, args.enterprise, year, month, cost_centers)
        enterprise_total = sum(cc_api_credits.values())
        enterprise_additional = sum(cc_additional.values())
        if not model_credits:
            print("  WARNING: no usageItems came back for any bucket this month.", file=sys.stderr)
    except Exception as e:
        print(f"  WARNING: couldn't get cost-center/model breakdown ({e}). "
              f"Falling back to detailed report totals (these come from a different "
              f"report type and may not exactly match the billing UI; additional-credits "
              f"figures won't be available in this fallback).", file=sys.stderr)
        # detailed_agg["cc_credits"] already has 0-credit entries filtered out
        cc_api_credits = {k: v for k, v in detailed_agg["cc_credits"].items() if v > 0}
        cc_additional = {}
        model_credits = {"(Model Breakdown Unavailable)": detailed_agg["total_credits"]}
        model_additional = {}
        cc_model_credits = {}
        cc_model_additional = {}
        enterprise_total = detailed_agg["total_credits"]
        enterprise_additional = 0.0

    print("Fetching licensed seats...")
    seats = fetch_all_seats(session, args.enterprise)
    total_allocated_credits, total_licensed_users, unrecognized_plans = \
        compute_allocated_credits_from_seats(seats, year, month)
    if unrecognized_plans:
        print(f"  WARNING: unrecognized plan_type(s) on some seats, excluded from allocation total: "
              f"{sorted(unrecognized_plans)}. Add them to STANDARD_INCLUDED_CREDITS / "
              f"PROMO_INCLUDED_CREDITS if they're valid Copilot plans.", file=sys.stderr)

    output_path = args.output or f"copilot_ai_usage_{year}-{month:02d}.csv"
    write_report(output_path, detailed_agg, model_credits, model_additional, cc_api_credits, cc_additional,
                 enterprise_total, enterprise_additional, total_licensed_users, total_allocated_credits,
                 year, month, cc_model_credits=cc_model_credits, cc_model_additional=cc_model_additional,
                 unrecognized_plans=unrecognized_plans)

    print(f"\nDone. Report written to {output_path}")
    print(f"  Total Allocated Credits: {total_allocated_credits:,.2f}")
    print(f"  Total AI Credits Used: {enterprise_total:,.2f}")
    print(f"  Total Additional AI Credits: {enterprise_additional:,.2f}")
    print(f"  Total Copilot Licensed Users: {total_licensed_users}")
    print(f"  Total Unique Users: {detailed_agg['total_unique_users']}")


if __name__ == "__main__":
    main()
