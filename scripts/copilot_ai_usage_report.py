#!/usr/bin/env python3
"""
GitHub Copilot AI Usage — Monthly Report Generator (v5)
----------------------------------------------------------
Produces a CSV with three sections:

  OVERALL METRICS
    Total Allocated Credits, Total AI Credits Used, Total Copilot
    Licensed Users, Total Unique Users

  COST CENTER WISE AI CREDIT USAGE
    Cost Center, Total AI Credits, Additional AI Credits, Unique Users,
    % of Total Credits

  MODEL WISE AI CREDIT USAGE
    Model Name, Total AI Credits, Additional AI Credits, % of Total Credits

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

Requires a CLASSIC PAT with the manage_billing:enterprise scope, held by an
enterprise admin or billing manager. A fine-grained PAT or GitHub App token
will NOT work for this script: the cost-centers endpoint
(/enterprises/{enterprise}/settings/billing/cost-centers) and the usage
report export endpoints explicitly reject GitHub App user/installation
tokens and fine-grained PATs (per GitHub's REST API docs), so the
cost-center listing and the detailed export -- and therefore the COST
CENTER WISE and MODEL WISE sections -- would fail even though the
AI-credit-usage and seats calls might succeed with such a token.

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


def warn_if_unsupported_token(token):
    """
    The cost-centers and usage-report-export endpoints reject fine-grained
    PATs and GitHub App tokens outright (classic PAT only). Fine-grained
    PATs are identifiable by their 'github_pat_' prefix; GitHub App
    installation/user tokens start with 'ghs_'/'ghu_'. Catching this here
    means the COST CENTER WISE / MODEL WISE sections fail loudly up front
    instead of quietly falling back to the degraded detailed-report-only
    path later, which is easy to miss.
    """
    if token.startswith("github_pat_") or token.startswith("ghs_") or token.startswith("ghu_"):
        print(
            "WARNING: this token looks like a fine-grained PAT or GitHub App token. "
            "The cost-centers and usage-report-export endpoints only accept classic "
            "PATs (manage_billing:enterprise scope). Expect 403s on the cost-center "
            "and model breakdown -- switch to a classic PAT (ghp_...) if you hit them.",
            file=sys.stderr,
        )


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

    all_rows = list(reader)
    rows = [r for r in all_rows if is_relevant_row(r, cols)]
    excluded = len(all_rows) - len(rows)
    if excluded:
        print(f"  Detailed export: {len(all_rows)} total row(s) across all metered products, "
              f"{excluded} excluded as non-Copilot-AI-credit rows, {len(rows)} kept. "
              f"(This filter is heuristic -- see is_relevant_row -- so double-check counts "
              f"look right if this report looks off.)", file=sys.stderr)

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
        "cc_credits": {k: v for k, v in cc_credits.items() if v > 0.0},
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
    a flat, unpaginated `costCenters` array. The endpoint now also supports
    an optional `state=active` query parameter server-side, so that's
    passed when active_only=True to avoid pulling archived/deleted cost
    centers over the wire at all. Each object still carries its own `state`
    field too, so a client-side filter is kept as a safety net in case the
    query param is ever ignored or a cost center comes back without a
    `state` value. Pass active_only=False to see everything, e.g. for
    debugging.
    """
    url = f"{base_url()}/enterprises/{enterprise}/settings/billing/cost-centers"
    params = {"per_page": 100}
    if active_only:
        params["state"] = "active"

    all_cost_centers = []
    page = 1
    while True:
        params["page"] = page
        resp = session.get(url, params=params)
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
        print(f"  Excluded {dropped} non-active cost center(s) (client-side safety net "
              f"-- the state=active filter should normally prevent these from being "
              f"returned at all): {dropped_names}", file=sys.stderr)
    return active


def build_usage_by_bucket(session, enterprise, year, month, cost_centers):
    """
    Fetches AI credit usage per cost center via the ai_credit/usage endpoint
    (same source as GitHub's billing UI), PLUS a separate call for usage not
    associated with any cost center. Returns (cc_credits, cc_additional,
    model_credits, model_additional):

      cc_credits / model_credits: {name: total_credits} -- TOTAL AI credits
        consumed (included-pool usage + additional usage combined), i.e.
        each item's `grossQuantity`.

      cc_additional / model_additional: {name: additional_credits} -- just
        the portion billed BEYOND the included pool, i.e. each item's
        `netQuantity` (matches the billing UI's "Additional credits"
        column; 0 for a cost center/model that stayed within its included
        allotment).

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

    Cost centers are keyed internally by their ID, not their display name.
    GitHub lets two cost centers share the same name (a rename, or a
    recreated cost center), and keying by name would silently merge their
    usage into a single row -- which would make that row's credits correct
    in aggregate but wrongly attributed, and would understate how many
    distinct cost centers actually have usage. Keying by ID first and only
    falling back to the display name at the very end (disambiguating with
    the ID if two live cost centers do share a name) avoids that.

    Any single cost center's API call failing is caught and logged, rather
    than silently vanishing or aborting the whole report.
    """
    # Keyed by cost center id (or a sentinel for "no cost center") so usage
    # is never merged across two differently-provisioned cost centers that
    # happen to share a display name.
    cc_totals_by_id = {}
    model_credits, model_additional = {}, {}

    def _accumulate(items):
        """
        Accumulates every item unconditionally into the model breakdown --
        no per-item threshold -- so model_credits/model_additional are
        built from exactly the same set of items as the cost-center bucket
        total below, with nothing dropped by one path and kept by the
        other. Returns this bucket's (total, additional) for the caller to
        decide whether the bucket itself counts as "real usage".
        """
        total, additional = 0.0, 0.0
        for item in items:
            gross = float(item.get("grossQuantity") or 0)
            net = float(item.get("netQuantity") or 0)  # portion beyond the included pool
            total += gross
            additional += net
            model = (item.get("model") or "").strip() or "(No model)"
            model_credits[model] = model_credits.get(model, 0.0) + gross
            model_additional[model] = model_additional.get(model, 0.0) + net
        return total, additional

    for cc in cost_centers:
        cc_id = cc.get("id")
        cc_name = (cc.get("name") or "").strip() or "(Unnamed Cost Center)"
        try:
            items = fetch_ai_credit_usage_by_model(session, enterprise, year, month, cost_center_id=cc_id)
        except Exception as e:
            print(f"  WARNING: couldn't fetch usage for cost center '{cc_name}' (id={cc_id}): {e}. "
                  f"Excluded from the report -- investigate separately.", file=sys.stderr)
            continue
        total, additional = _accumulate(items)
        if total > 0:
            cc_totals_by_id[cc_id] = {"name": cc_name, "total": total, "additional": additional}

    try:
        unassigned_items = fetch_ai_credit_usage_by_model(session, enterprise, year, month, cost_center_id="none")
    except Exception as e:
        print(f"  WARNING: couldn't fetch unassigned (no cost center) usage: {e}. "
              f"Treating it as 0.00 -- the enterprise total below may be understated.", file=sys.stderr)
        unassigned_items = []
    total, additional = _accumulate(unassigned_items)
    if total > 0:
        cc_totals_by_id["__unassigned__"] = {"name": "(Not Assigned)", "total": total, "additional": additional}

    # Model rows with zero net total this period are dropped, matching the
    # same "only real usage" rule applied to cost centers above.
    model_credits = {m: v for m, v in model_credits.items() if v > 0}
    model_additional = {m: model_additional.get(m, 0.0) for m in model_credits}

    # Collapse the id-keyed buckets down to the name-keyed dicts the rest
    # of the script (and the CSV) works with. If two live cost centers
    # really do share a display name, disambiguate with the ID rather than
    # silently summing them into one row.
    name_counts = {}
    for info in cc_totals_by_id.values():
        name_counts[info["name"]] = name_counts.get(info["name"], 0) + 1

    cc_credits, cc_additional = {}, {}
    for cc_id, info in cc_totals_by_id.items():
        name = info["name"]
        if name_counts[name] > 1:
            display_name = f"{name} (id: {cc_id})"
            print(f"  NOTE: multiple cost centers with usage share the display name "
                  f"'{name}' -- showing them separately as '{display_name}' instead of "
                  f"merging their credits into one row.", file=sys.stderr)
        else:
            display_name = name
        cc_credits[display_name] = info["total"]
        cc_additional[display_name] = info["additional"]

    # Sanity check: since cc_credits/cc_additional and model_credits/model_additional
    # are built from the exact same set of API responses, their totals must match.
    # A mismatch here means the two groupings genuinely disagree on the underlying
    # data (not just a display bug), so it's surfaced loudly rather than silently
    # papered over.
    cc_total_sum = sum(cc_credits.values())
    model_total_sum = sum(model_credits.values())
    if abs(cc_total_sum - model_total_sum) > 0.01:
        print(f"  WARNING: cost-center total ({cc_total_sum:,.2f}) and model total "
              f"({model_total_sum:,.2f}) disagree by {abs(cc_total_sum - model_total_sum):,.2f} "
              f"credits. Investigate -- the API responses themselves are inconsistent, "
              f"this isn't just a display issue.", file=sys.stderr)
    cc_additional_sum = sum(cc_additional.values())
    model_additional_sum = sum(model_additional.values())
    if abs(cc_additional_sum - model_additional_sum) > 0.01:
        print(f"  WARNING: cost-center additional-credits total ({cc_additional_sum:,.2f}) and "
              f"model additional-credits total ({model_additional_sum:,.2f}) disagree by "
              f"{abs(cc_additional_sum - model_additional_sum):,.2f} credits. Investigate.",
              file=sys.stderr)

    print(f"  Cost centers checked: {len(cost_centers)}. Rows with usage in report: {len(cc_credits)}.")
    return cc_credits, cc_additional, model_credits, model_additional


# ---------------------------------------------------------------------------
# Step 5: write the four-section CSV
# ---------------------------------------------------------------------------

def match_cost_center_users(cc_names, cc_users):
    """
    cc_names come from the ai_credit/usage cost-center list (the
    authoritative source for credit totals); cc_users is keyed by whatever
    cost_center_name string showed up in the separate `detailed` CSV
    export. These are two independent API calls, so a cost center that's
    real and has credit usage can still fail to line up on an exact string
    match (case, extra whitespace beyond a plain .strip(), a rename between
    calls, etc.) -- and silently showing "0" for Unique Users in that case
    looks like valid data instead of a join miss.

    Matches first by exact name, then falls back to a case/space-normalized
    match. Any cc_name that still can't be matched to a detailed-report
    cost center is reported via a warning rather than silently defaulted to
    0, and the caller decides what to show.

    Returns {cc_name: user_count_or_None}. None means "no match found" --
    the caller is responsible for rendering that distinctly from a real 0.
    """
    def _norm(s):
        return " ".join((s or "").strip().lower().split())

    normalized_index = {}
    for name, count in cc_users.items():
        normalized_index.setdefault(_norm(name), []).append((name, count))

    resolved = {}
    for cc_name in cc_names:
        if cc_name in cc_users:
            resolved[cc_name] = cc_users[cc_name]
            continue
        candidates = normalized_index.get(_norm(cc_name), [])
        if len(candidates) == 1:
            matched_name, count = candidates[0]
            print(f"  NOTE: matched cost center '{cc_name}' (credit usage) to "
                  f"'{matched_name}' (detailed report) by normalized name -- exact "
                  f"strings differed.", file=sys.stderr)
            resolved[cc_name] = count
        elif len(candidates) > 1:
            print(f"  WARNING: cost center '{cc_name}' has {len(candidates)} ambiguous "
                  f"normalized-name matches in the detailed report {[c[0] for c in candidates]}. "
                  f"Unique Users left blank for this row -- investigate separately.", file=sys.stderr)
            resolved[cc_name] = None
        else:
            print(f"  WARNING: cost center '{cc_name}' has AI credit usage but no matching "
                  f"entry in the detailed report -- Unique Users can't be determined for it "
                  f"(shown as blank, not 0).", file=sys.stderr)
            resolved[cc_name] = None
    return resolved


def write_report(output_path, detailed_agg, model_credits, model_additional, cc_api_credits, cc_additional,
                 enterprise_total, enterprise_additional, total_licensed_users, total_allocated_credits,
                 year, month, unrecognized_plans=None):

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        writer.writerow([f"Copilot AI Usage Report - {year}-{month:02d}"])
        writer.writerow([])

        writer.writerow(["OVERALL METRICS"])
        writer.writerow(["Total Allocated Credits", f"{total_allocated_credits:,.2f}"])
        writer.writerow(["Total AI Credits Used (included + additional combined)", f"{enterprise_total:,.2f}"])
        writer.writerow(["Total Included AI Credits (within pool -- matches the billing UI's \"Included credits\" tile)",
                          f"{enterprise_total - enterprise_additional:,.2f}"])
        writer.writerow(["Total Additional AI Credits (beyond included pool)", f"{enterprise_additional:,.2f}"])
        writer.writerow(["Total Copilot Licensed Users", total_licensed_users])
        writer.writerow(["Total Unique Users", detailed_agg["total_unique_users"]])
        if unrecognized_plans:
            writer.writerow([
                "  Note", f"seats with unrecognized plan_type excluded from allocation: {sorted(unrecognized_plans)}"
            ])
        writer.writerow([])

        writer.writerow(["COST CENTER WISE AI CREDIT USAGE"])
        writer.writerow(["Cost Center", "Total AI Credits", "Included AI Credits", "Additional AI Credits",
                          "Unique Users", "% of Total Credits"])
        cc_user_counts = match_cost_center_users(cc_api_credits.keys(), detailed_agg["cc_users"])
        for cc_name in sorted(cc_api_credits, key=lambda k: -cc_api_credits[k]):
            credits_ = cc_api_credits[cc_name]
            additional_ = cc_additional.get(cc_name, 0.0)
            included_ = credits_ - additional_
            pct = (credits_ / enterprise_total * 100) if enterprise_total else 0
            user_count = cc_user_counts.get(cc_name)
            user_display = user_count if user_count is not None else "(unmatched)"
            writer.writerow([cc_name, f"{credits_:,.2f}", f"{included_:,.2f}", f"{additional_:,.2f}",
                              user_display, f"{pct:.2f}%"])
        writer.writerow(["TOTAL", f"{enterprise_total:,.2f}", f"{enterprise_total - enterprise_additional:,.2f}",
                          f"{enterprise_additional:,.2f}", detailed_agg["total_unique_users"], "100.00%"])
        writer.writerow([])

        writer.writerow(["MODEL WISE AI CREDIT USAGE"])
        writer.writerow(["Model Name", "Total AI Credits", "Included AI Credits", "Additional AI Credits",
                          "% of Total Credits"])
        for model in sorted(model_credits, key=lambda k: -model_credits[k]):
            credits_ = model_credits[model]
            additional_ = model_additional.get(model, 0.0)
            included_ = credits_ - additional_
            pct = (credits_ / enterprise_total * 100) if enterprise_total else 0
            writer.writerow([model, f"{credits_:,.2f}", f"{included_:,.2f}", f"{additional_:,.2f}", f"{pct:.2f}%"])
        # Reuse the same enterprise_total / enterprise_additional values printed in the
        # COST CENTER WISE TOTAL row above (rather than independently re-summing
        # model_credits/model_additional here) so the two TOTAL rows -- and the
        # OVERALL METRICS "Total AI Credits Used" / "Total Additional AI Credits" --
        # are guaranteed identical by construction, not just equal in practice.
        writer.writerow(["TOTAL", f"{enterprise_total:,.2f}", f"{enterprise_total - enterprise_additional:,.2f}",
                          f"{enterprise_additional:,.2f}", "100.00%"])


def audit_cost_center_coverage(detailed_cc_credits, cc_api_credits):
    """
    Cross-checks the cost-center names seen in the raw `detailed` export
    (detailed_cc_credits, already filtered to > 0 usage) against the cost
    centers that ended up in the ai_credit/usage-based breakdown
    (cc_api_credits) -- the two come from independent API calls, so this
    catches a cost center that genuinely has usage but silently never made
    it into the report at all (e.g. because fetch_cost_centers() didn't
    return it, or its ai_credit/usage call came back empty for some other
    reason). That failure mode produces no exception and no existing
    warning -- the cost center's row just doesn't exist -- so without this
    check it's invisible until someone manually compares against the
    billing UI.

    Returns the list of (name, detailed_report_credits) pairs that are
    missing, and prints a WARNING for each. An empty list means every
    cost center with real usage in the detailed export is accounted for
    somewhere in the ai_credit/usage breakdown.
    """
    def _norm(s):
        return " ".join((s or "").strip().lower().split())

    api_normalized = {_norm(k) for k in cc_api_credits}
    missing = []
    for name, credits_ in detailed_cc_credits.items():
        if _norm(name) in api_normalized:
            continue
        # Allow for the "(id: xxx)" suffix build_usage_by_bucket adds when
        # disambiguating two cost centers that share a display name.
        if any(_norm(k).startswith(_norm(name) + " (id:") for k in cc_api_credits):
            continue
        missing.append((name, credits_))

    for name, credits_ in sorted(missing, key=lambda x: -x[1]):
        print(f"  WARNING: cost center '{name}' shows {credits_:,.2f} credits of usage in the "
              f"detailed report but does not appear anywhere in the ai_credit/usage cost-center "
              f"breakdown -- it is MISSING from this report. Check whether fetch_cost_centers() "
              f"returned this cost center at all, and whether its ai_credit/usage call returned "
              f"any items.", file=sys.stderr)
    return missing


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

    warn_if_unsupported_token(args.token)
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
    # section reconciles. Every cost center (active or not) is fetched, but
    # only those with real usage (> 0) this period end up in the report;
    # ones with no usage this period are excluded.
    print("Fetching cost centers...")
    try:
        # active_only=False: fetch EVERY cost center, not just ones GitHub marks
        # "active". A cost center's `state` field has been observed to not
        # always line up with whether it has real usage this period -- filtering
        # to "active" risks silently skipping a cost center's usage call entirely
        # (no error, it just never gets queried). There's no downside to fetching
        # all of them: any cost center with zero usage this period is already
        # dropped later by build_usage_by_bucket's "total > 0" check, so casting
        # a wider net here only prevents real usage from going missing.
        cost_centers = fetch_cost_centers(session, args.enterprise, active_only=False)
        print(f"  Found {len(cost_centers)} cost center(s) total: {[c.get('name') for c in cost_centers]}")
        cc_api_credits, cc_additional, model_credits, model_additional = \
            build_usage_by_bucket(session, args.enterprise, year, month, cost_centers)
        enterprise_total = sum(cc_api_credits.values())
        enterprise_additional = sum(cc_additional.values())
        if not model_credits:
            print("  WARNING: no usageItems came back for any bucket this month.", file=sys.stderr)
        missing = audit_cost_center_coverage(detailed_agg["cc_credits"], cc_api_credits)
        if missing:
            print(f"  WARNING: {len(missing)} cost center(s) show usage in the detailed report "
                  f"but are missing from this report's cost-center breakdown -- see above. "
                  f"Total AI Credits Used and the cost-center rows below are understated.",
                  file=sys.stderr)
    except Exception as e:
        print(f"  WARNING: couldn't get cost-center/model breakdown ({e}). "
              f"Falling back to detailed report totals (these come from a different "
              f"report type and may not exactly match the billing UI; additional-credits "
              f"figures won't be available in this fallback).", file=sys.stderr)
        # detailed_agg["cc_credits"] already has 0-credit entries filtered out
        cc_api_credits = detailed_agg["cc_credits"]
        cc_additional = {}
        model_credits = {"(Model Breakdown Unavailable)": detailed_agg["total_credits"]}
        model_additional = {}
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
                 year, month, unrecognized_plans=unrecognized_plans)

    print(f"\nDone. Report written to {output_path}")
    print(f"  Total Allocated Credits: {total_allocated_credits:,.2f}")
    print(f"  Total AI Credits Used: {enterprise_total:,.2f}")
    print(f"  Total Additional AI Credits: {enterprise_additional:,.2f}")
    print(f"  Total Copilot Licensed Users: {total_licensed_users}")
    print(f"  Total Unique Users: {detailed_agg['total_unique_users']}")


if __name__ == "__main__":
    main()
