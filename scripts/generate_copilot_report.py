"""
GitHub Copilot Detailed Usage Report Generator

Fetches Copilot usage data from the GitHub API and generates a summary report
with overall metrics, cost center-wise usage, and model-wise usage.
"""

import os
import sys
import json
import csv
import io
from datetime import datetime, timedelta
from collections import defaultdict

import requests


def get_billing_month_dates(month_selection):
    """
    Calculate the start and end dates for the selected billing month.
    GitHub Copilot billing cycles typically run from the 1st to the last day of a month.
    """
    today = datetime.now()

    if month_selection == "last_month":
        # Previous month
        first_of_current = today.replace(day=1)
        last_of_prev = first_of_current - timedelta(days=1)
        start_date = last_of_prev.replace(day=1)
        end_date = last_of_prev
    elif month_selection == "current_month":
        start_date = today.replace(day=1)
        end_date = today
    else:
        # Format: YYYY-MM (e.g., "2026-05" for May 2026)
        try:
            year, month = map(int, month_selection.split("-"))
            start_date = datetime(year, month, 1)
            # Get last day of the month
            if month == 12:
                end_date = datetime(year + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = datetime(year, month + 1, 1) - timedelta(days=1)
            # If end_date is in the future, cap at today
            if end_date > today:
                end_date = today
        except (ValueError, AttributeError):
            print(f"Invalid month selection: {month_selection}")
            sys.exit(1)

    return start_date, end_date


def get_auth_headers(token):
    """Build authentication headers for GitHub API requests."""
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28"
    }


def download_ndjson(download_links):
    """Download and parse NDJSON files from signed URLs."""
    all_data = []
    for url in download_links:
        # Signed URLs do not require authentication headers
        response = requests.get(url, timeout=60)
        if response.status_code == 200:
            for line in response.text.strip().split('\n'):
                if line.strip():
                    try:
                        all_data.append(json.loads(line))
                    except json.JSONDecodeError:
                        print(f"Warning: Failed to parse NDJSON line: {line[:100]}")
    return all_data


def fetch_copilot_org_report(enterprise, token, start_date, end_date):
    """
    Fetch enterprise-level Copilot usage metrics using the real metrics API.
    Uses /enterprises/{enterprise}/copilot/metrics with since/until parameters.
    The endpoint supports a maximum range of 28 days per request.
    """
    headers = get_auth_headers(token)
    all_data = []

    # Chunk requests to respect the 28-day API limit
    chunk_size = timedelta(days=27)
    current_start = start_date

    while current_start <= end_date:
        chunk_end = min(current_start + chunk_size, end_date)
        url = f"https://api.github.com/enterprises/{enterprise}/copilot/metrics"
        params = {
            "since": current_start.strftime("%Y-%m-%d"),
            "until": chunk_end.strftime("%Y-%m-%d")
        }
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                all_data.extend(data)
            else:
                print(f"Warning: Unexpected response format for enterprise metrics. Skipping.")
        elif response.status_code == 204:
            pass  # No data available for this period
        elif response.status_code == 404:
            print(f"Warning: Enterprise Copilot metrics not found. Check ENTERPRISE_SLUG.")
            break
        elif response.status_code == 403:
            print("Warning: Access forbidden for enterprise metrics.")
            print("Ensure the token has 'manage_billing:copilot' scope.")
            break
        else:
            print(f"Warning: Unexpected status {response.status_code} for enterprise metrics.")

        current_start = chunk_end + timedelta(days=1)

    return all_data


def fetch_copilot_user_report(enterprise, token, start_date, end_date):
    """
    Per-user Copilot metrics are not available via the standard public API.
    Enterprise-level metrics from fetch_copilot_org_report are used instead.
    """
    return []


def fetch_copilot_user_teams(enterprise, token, end_date):
    """
    User-to-team mapping is not available via the standard public API.
    Returns empty list; cost-center breakdown relies on team data when available.
    """
    return []


def fetch_copilot_usage(enterprise, token, start_date, end_date):
    """
    Fetch Copilot usage data from the legacy GitHub API.
    Uses the /enterprises/{enterprise}/copilot/usage endpoint (deprecated, kept as fallback).
    """
    headers = get_auth_headers(token)

    usage_url = f"https://api.github.com/enterprises/{enterprise}/copilot/usage"
    params = {
        "since": start_date.strftime("%Y-%m-%d"),
        "until": end_date.strftime("%Y-%m-%d")
    }

    all_usage_data = []
    page = 1

    while True:
        params["page"] = page
        params["per_page"] = 100
        response = requests.get(usage_url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            if not data:
                break
            all_usage_data.extend(data)
            if len(data) < 100:
                break
            page += 1
        elif response.status_code == 404:
            print("Note: Legacy usage API not available (deprecated). Using reports API data.")
            return []
        elif response.status_code == 403:
            print("Note: Legacy usage API access forbidden. Using reports API data.")
            return []
        else:
            print(f"Note: Legacy usage API returned {response.status_code}. Using reports API data.")
            return []

    return all_usage_data


def fetch_copilot_billing(enterprise, token):
    """
    Fetch Copilot billing/seats information to get license and pooled credits data.
    """
    headers = get_auth_headers(token)

    billing_url = f"https://api.github.com/enterprises/{enterprise}/copilot/billing"
    response = requests.get(billing_url, headers=headers, timeout=30)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Warning: Could not fetch billing data: {response.status_code}")
        return None


def fetch_enterprise_billing_usage(enterprise, token, year, month):
    """
    Fetch enterprise billing usage line items.
    Endpoint: GET /enterprises/{enterprise}/settings/billing/usage

    Returns detailed line items for every billable product (including Copilot AI
    credits).  Each item may carry a costCenter / organizationName field that lets
    us build a cost-center-wise breakdown without a separate API call.
    """
    headers = get_auth_headers(token)
    url = f"https://api.github.com/enterprises/{enterprise}/settings/billing/usage"
    params = {"year": year, "month": month}

    response = requests.get(url, headers=headers, params=params, timeout=30)

    if response.status_code == 200:
        return response.json()
    elif response.status_code == 403:
        print("Note: Billing usage API access forbidden. "
              "Ensure the token has 'read:enterprise' scope.")
        return None
    elif response.status_code == 404:
        print("Note: Billing usage API not available for this enterprise.")
        return None
    else:
        print(f"Note: Billing usage API returned {response.status_code}")
        return None


def fetch_cost_centers(enterprise, token):
    """
    Fetch the list of cost centers for the enterprise.
    Endpoint: GET /enterprises/{enterprise}/cost-centers

    Each cost center includes a 'resources' list that maps organizations or
    teams to the cost center.  This is used to look up which org belongs to
    which cost center when the billing-usage line items do not carry a direct
    cost-center label.
    """
    headers = get_auth_headers(token)
    url = f"https://api.github.com/enterprises/{enterprise}/cost-centers"
    all_centers = []
    page = 1

    while True:
        response = requests.get(
            url, headers=headers,
            params={"page": page, "per_page": 100}, timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            items = data if isinstance(data, list) else data.get("cost_centers", [])
            if not items:
                break
            all_centers.extend(items)
            if len(items) < 100:
                break
            page += 1
        elif response.status_code in (403, 404):
            print(f"Note: Cost centers API returned {response.status_code}.")
            return []
        else:
            print(f"Warning: Cost centers API returned {response.status_code}")
            break

    return all_centers


def fetch_org_copilot_metrics(org, token, start_date, end_date):
    """
    Fetch Copilot metrics for a single organization.
    Endpoint: GET /orgs/{org}/copilot/metrics

    Used to build a cost-center-level breakdown when the billing-usage API
    does not include cost-center fields in its line items.
    The endpoint supports a maximum range of 28 days per request; we use
    27-day chunks to avoid off-by-one boundary issues on inclusive date ranges.
    """
    headers = get_auth_headers(token)
    all_data = []
    chunk_size = timedelta(days=27)  # 27 not 28: keeps both ends inclusive safely
    current_start = start_date

    while current_start <= end_date:
        chunk_end = min(current_start + chunk_size, end_date)
        url = f"https://api.github.com/orgs/{org}/copilot/metrics"
        params = {
            "since": current_start.strftime("%Y-%m-%d"),
            "until": chunk_end.strftime("%Y-%m-%d")
        }
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                all_data.extend(data)
        elif response.status_code == 204:
            pass  # No data for this period
        elif response.status_code in (403, 404):
            # Token may not have org-level access; stop quietly
            break
        else:
            print(f"Warning: Org metrics API returned {response.status_code} for {org}")
            break

        current_start = chunk_end + timedelta(days=1)

    return all_data


def fetch_copilot_metrics(enterprise, token, start_date, end_date):
    """
    Fetch Copilot metrics from the legacy metrics API endpoint (deprecated, kept as fallback).
    """
    headers = get_auth_headers(token)

    metrics_url = f"https://api.github.com/enterprises/{enterprise}/copilot/metrics"
    params = {
        "since": start_date.strftime("%Y-%m-%d"),
        "until": end_date.strftime("%Y-%m-%d")
    }

    response = requests.get(metrics_url, headers=headers, params=params, timeout=30)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Note: Legacy metrics API returned {response.status_code}. Using reports API data.")
        return None


def process_usage_data(usage_data):
    """
    Process raw usage data to extract:
    - Total AI credits used
    - Unique users
    - Cost center (team) wise breakdown
    - Model wise breakdown
    """
    total_credits = 0
    unique_users = set()
    cost_center_credits = defaultdict(lambda: {"credits": 0, "users": set()})
    model_credits = defaultdict(float)

    for day_data in usage_data:
        breakdowns = day_data.get("breakdowns", [])

        for breakdown in breakdowns:
            user = breakdown.get("user", {})
            username = user.get("login", "Unknown") if isinstance(user, dict) else str(user)
            team = breakdown.get("team", "Not Assigned")
            if not team:
                team = "Not Assigned"

            # Get credits/suggestions metrics
            credits = breakdown.get("total_credits_consumed", 0) or 0
            suggestions_count = breakdown.get("suggestions_count", 0) or 0
            acceptances_count = breakdown.get("acceptances_count", 0) or 0

            # Use credits if available, otherwise fall back to suggestion count
            # (estimation only used for legacy API responses without credit data)
            day_credits = credits if credits else 0

            total_credits += day_credits
            unique_users.add(username)
            cost_center_credits[team]["credits"] += day_credits
            cost_center_credits[team]["users"].add(username)

            # Model breakdown
            model = breakdown.get("model", "Unknown")
            if not model:
                model = "Unknown"
            model_credits[model] += day_credits

    return {
        "total_credits": total_credits,
        "unique_users": len(unique_users),
        "cost_center_breakdown": cost_center_credits,
        "model_breakdown": model_credits
    }


def process_user_report_data(user_data, user_teams_data):
    """
    Process user-level report data (NDJSON) to extract per-user and per-team breakdowns.
    """
    total_credits = 0
    unique_users = set()
    cost_center_credits = defaultdict(lambda: {"credits": 0, "users": set()})
    model_credits = defaultdict(float)

    # Build user-to-team mapping from user-teams report
    # The API may use 'team_slug' or 'team' depending on the report version
    user_team_map = {}
    for entry in user_teams_data:
        login = entry.get("login", "")
        team = entry.get("team_slug") or entry.get("team", "")
        if login and team:
            user_team_map[login] = team

    for record in user_data:
        username = record.get("login", "Unknown")
        unique_users.add(username)
        team = user_team_map.get(username, "Not Assigned")

        # Aggregate credits from all Copilot features
        user_credits = 0

        for feature_key in ("copilot_ide_code_completions", "copilot_ide_chat",
                            "copilot_dotcom_chat", "copilot_dotcom_pull_requests"):
            feature_data = record.get(feature_key, {})
            if not feature_data:
                continue

            if feature_key == "copilot_ide_code_completions":
                for editor_data in feature_data.get("editors", []):
                    for model_data in editor_data.get("models", []):
                        model_name = model_data.get("name", "Unknown")
                        for lang in model_data.get("languages", []):
                            credits = lang.get("total_credits_consumed", 0) or 0
                            user_credits += credits
                            model_credits[model_name] += credits
            elif feature_key == "copilot_ide_chat":
                for editor_data in feature_data.get("editors", []):
                    for model_data in editor_data.get("models", []):
                        model_name = model_data.get("name", "Unknown")
                        credits = model_data.get("total_credits_consumed", 0) or 0
                        user_credits += credits
                        model_credits[model_name] += credits
            else:
                for model_data in feature_data.get("models", []):
                    model_name = model_data.get("name", "Unknown")
                    credits = model_data.get("total_credits_consumed", 0) or 0
                    user_credits += credits
                    model_credits[model_name] += credits

        total_credits += user_credits
        cost_center_credits[team]["credits"] += user_credits
        cost_center_credits[team]["users"].add(username)

    return {
        "total_credits": total_credits,
        "unique_users": len(unique_users),
        "cost_center_breakdown": cost_center_credits,
        "model_breakdown": model_credits
    }


def process_metrics_data(metrics_data):
    """
    Process metrics API data for detailed breakdowns.
    Extracts unique users from total_active_users and supports both
    total_credits_consumed (newer API) and total_ai_tokens (proxy) fields.
    """
    total_credits = 0
    cost_center_credits = defaultdict(lambda: {"credits": 0, "users": set()})
    model_credits = defaultdict(float)
    # The enterprise metrics API returns aggregate daily counts, not per-user records.
    # We use the peak (maximum) daily active-user count as a conservative estimate of
    # monthly unique users. This will undercount if different users are active on
    # different days, but is the best approximation available from the aggregate API.
    unique_users_max = 0

    for day_data in metrics_data:
        daily_users = day_data.get("total_active_users", 0) or 0
        if daily_users > unique_users_max:
            unique_users_max = daily_users

        copilot_ide_code_completions = day_data.get("copilot_ide_code_completions", {})
        copilot_ide_chat = day_data.get("copilot_ide_chat", {})
        copilot_dotcom_chat = day_data.get("copilot_dotcom_chat", {})
        copilot_dotcom_pull_requests = day_data.get("copilot_dotcom_pull_requests", {})

        # Process code completions
        if copilot_ide_code_completions:
            for editor_data in copilot_ide_code_completions.get("editors", []):
                for model_data in editor_data.get("models", []):
                    model_name = model_data.get("name", "Unknown")
                    for lang in model_data.get("languages", []):
                        # Prefer total_credits_consumed (newer API with AI credits billing).
                        # Fall back to total_ai_tokens only if credits field is absent —
                        # both represent consumption units from the same model response and
                        # are used as proxies when the billing credit field is unavailable.
                        credits = (lang.get("total_credits_consumed") or
                                   lang.get("total_ai_tokens") or 0)
                        total_credits += credits
                        model_credits[model_name] += credits

        # Process IDE chat
        if copilot_ide_chat:
            for editor_data in copilot_ide_chat.get("editors", []):
                for model_data in editor_data.get("models", []):
                    model_name = model_data.get("name", "Unknown")
                    # Same preference: credits > tokens > 0
                    credits = (model_data.get("total_credits_consumed") or
                               model_data.get("total_ai_tokens") or 0)
                    total_credits += credits
                    model_credits[model_name] += credits

        # Process dotcom chat
        if copilot_dotcom_chat:
            for model_data in copilot_dotcom_chat.get("models", []):
                model_name = model_data.get("name", "Unknown")
                credits = (model_data.get("total_credits_consumed") or
                           model_data.get("total_ai_tokens") or 0)
                total_credits += credits
                model_credits[model_name] += credits

        # Process pull requests
        if copilot_dotcom_pull_requests:
            for model_data in copilot_dotcom_pull_requests.get("models", []):
                model_name = model_data.get("name", "Unknown")
                credits = (model_data.get("total_credits_consumed") or
                           model_data.get("total_ai_tokens") or 0)
                total_credits += credits
                model_credits[model_name] += credits

    return {
        "total_credits": total_credits,
        "unique_users": unique_users_max,
        "cost_center_breakdown": cost_center_credits,
        "model_breakdown": model_credits
    }


def process_billing_usage_data(billing_usage, cost_centers):
    """
    Process enterprise billing usage line items to extract AI credits consumed
    and build a cost-center-wise breakdown.

    GitHub billing usage items for Copilot AI credits typically have:
      product  = "Copilot"
      sku      = "Copilot Premium Requests" / "Copilot AI Credits" / similar
      quantity = number of AI credits consumed
      unitType = "request" / "credits"

    The cost center is read from the item's costCenter / costCenterName field
    if present, otherwise derived from the costCenterId via the cost_centers list,
    and finally falls back to organizationName.
    """
    if not billing_usage:
        return None

    usage_items = billing_usage.get("usageItems", [])
    if not usage_items:
        return None

    # Build cost-center-ID → name lookup from the cost_centers list
    cc_id_to_name = {}
    for cc in (cost_centers or []):
        cc_id = str(cc.get("id") or cc.get("cost_center_id") or "")
        cc_name = cc.get("name") or cc.get("displayName") or "Unknown"
        if cc_id:
            cc_id_to_name[cc_id] = cc_name

    total_credits = 0.0
    cost_center_credits = defaultdict(lambda: {"credits": 0.0, "users": set()})
    found_copilot_items = False

    for item in usage_items:
        product = (item.get("product") or "").lower()
        sku = (item.get("sku") or "").lower()
        unit_type = (item.get("unitType") or "").lower()

        # Match Copilot AI credit / premium-request line items.
        # Known GitHub billing SKUs (as of 2025):
        #   "Copilot Premium Requests"          – standard included AI credits
        #   "Copilot Add-on Premium Requests"   – additional (paid) AI credits
        #   "Copilot AI Credits"                – alternative SKU name
        #   "Copilot Premium Model Requests"    – premium model variant
        # We require the item to be Copilot-branded AND to reference one of the
        # specific AI-credit-related SKU phrases to avoid matching seat/license
        # or other Copilot line items (e.g. "Copilot for Business Seat").
        is_copilot = "copilot" in product or "copilot" in sku
        is_ai_usage = any(kw in sku for kw in (
            "premium request", "ai credit", "premium model",
            "add-on premium", "addon premium"
        )) or unit_type in ("credit", "request", "credits", "requests")

        if not (is_copilot and is_ai_usage):
            continue

        found_copilot_items = True
        quantity = float(item.get("quantity") or 0)

        # Resolve cost center name – try several field name variations
        cc_id = str(item.get("costCenterId") or item.get("cost_center_id") or "")
        cc_name = (
            item.get("costCenter") or
            item.get("cost_center") or
            item.get("costCenterName") or
            item.get("cost_center_name") or
            cc_id_to_name.get(cc_id) or
            item.get("organizationName") or
            "Not Assigned"
        )
        if not cc_name:
            cc_name = "Not Assigned"

        total_credits += quantity
        cost_center_credits[cc_name]["credits"] += quantity

    if not found_copilot_items or total_credits == 0:
        return None

    return {
        "total_credits": total_credits,
        "unique_users": 0,
        "cost_center_breakdown": cost_center_credits,
        "model_breakdown": {}
    }


def build_cost_center_metrics(cost_centers, token, start_date, end_date):
    """
    Build a cost-center-level breakdown by fetching Copilot metrics for each
    organization that is a resource in a cost center.

    This is used as a fallback when the billing-usage API does not carry
    cost-center information directly in its line items.

    Returns a defaultdict keyed by cost-center name.  Each value has:
      credits    – float, total AI credits consumed
      users      – set (may be empty); user_count stores the count separately
      user_count – int, number of unique users reported by the org metrics API
    """
    cost_center_credits = defaultdict(
        lambda: {"credits": 0.0, "users": set(), "user_count": 0}
    )

    if not cost_centers:
        return cost_center_credits

    for center in cost_centers:
        center_name = center.get("name") or center.get("displayName", "Unknown")
        resources = center.get("resources", [])

        for resource in resources:
            res_type = (resource.get("type") or "").lower()
            res_name = resource.get("name") or resource.get("login") or ""

            if res_type == "organization" and res_name:
                print(f"  Fetching metrics for org '{res_name}' "
                      f"(cost center: '{center_name}')...")
                org_metrics = fetch_org_copilot_metrics(
                    res_name, token, start_date, end_date
                )
                if org_metrics:
                    org_processed = process_metrics_data(org_metrics)
                    cost_center_credits[center_name]["credits"] += (
                        org_processed["total_credits"]
                    )
                    # Keep the user count as a plain integer to avoid creating
                    # synthetic set members which inflate counts on aggregation.
                    cost_center_credits[center_name]["user_count"] += (
                        org_processed["unique_users"]
                    )

    return cost_center_credits


def _get_user_count(cost_center_data):
    """
    Return the user count for a cost-center data dict.

    Two paths populate cost-center entries:
    - process_billing_usage_data / process_user_report_data: fills the `users` set
    - build_cost_center_metrics: sets `user_count` as a plain integer (no set members)

    This helper checks both so callers don't need to repeat the logic.
    """
    return cost_center_data.get("user_count") or len(cost_center_data.get("users", set()))


# Default AI-credits-per-seat used when the billing API does not return the value.
# Copilot Business and Enterprise both include 100 AI credits / seat / month as of 2025.
# Verify against https://docs.github.com/en/billing/managing-billing-for-your-products/
# managing-billing-for-github-copilot/about-billing-for-github-copilot when upgrading.
_DEFAULT_AI_CREDITS_PER_SEAT = 100


def generate_report(report_data, billing_data, month_name):
    """
    Generate the usage report as a formatted string and CSV.
    """
    total_credits = report_data["total_credits"]
    unique_users = report_data["unique_users"]
    cost_center_breakdown = report_data["cost_center_breakdown"]
    model_breakdown = report_data["model_breakdown"]

    # Get pooled (allocated) credits from billing data.
    # GitHub exposes this under several field names depending on the API version.
    pooled_credits = "N/A"
    if billing_data:
        # Support both top-level total_seats and nested seat_breakdown.total
        seat_breakdown = billing_data.get("seat_breakdown", {})
        seat_count = (billing_data.get("total_seats") or
                      seat_breakdown.get("total") or 0)

        # Try every known field name for the monthly AI-credit allocation
        total_included = (
            billing_data.get("total_included_ai_credits") or
            billing_data.get("included_credits") or
            billing_data.get("allocated_ai_credits") or
            billing_data.get("monthly_included_ai_credits") or
            billing_data.get("ai_credits_budget") or
            billing_data.get("included_ai_credits")
        )

        if total_included:
            pooled_credits = int(total_included)
        elif seat_count:
            # GitHub Copilot seats include a monthly AI-credits allocation.
            # Try the per-seat field; fall back to the module-level constant.
            premium_per_seat = (
                billing_data.get("premium_requests_per_seat") or
                billing_data.get("included_requests_per_seat") or
                billing_data.get("ai_credits_per_seat")
            )
            if premium_per_seat:
                pooled_credits = int(seat_count * premium_per_seat)
            else:
                pooled_credits = int(seat_count * _DEFAULT_AI_CREDITS_PER_SEAT)
                print(f"Note: 'premium_requests_per_seat' not in billing response. "
                      f"Using default {_DEFAULT_AI_CREDITS_PER_SEAT} AI credits/seat. "
                      f"Total seats: {seat_count}")

    report_lines = []
    report_lines.append(f"GitHub Copilot USAGE SUMMARY REPORT - {month_name.upper()}")
    report_lines.append("=" * 60)
    report_lines.append("")
    report_lines.append("OVERALL METRICS")
    report_lines.append("-" * 40)
    if pooled_credits != "N/A":
        report_lines.append(f"  Pooled Credits (Allocated): {pooled_credits:,}")
    else:
        report_lines.append(f"  Pooled Credits (Allocated): {pooled_credits}")
    report_lines.append(f"  Total AI Credits Used:   {total_credits:,.2f}")
    report_lines.append(f"  Total Unique Users:      {unique_users:,}")
    report_lines.append("")
    report_lines.append("")

    # Cost Center wise breakdown
    report_lines.append("COST CENTER WISE AI CREDIT USAGE")
    report_lines.append("-" * 60)
    report_lines.append(f"  {'Cost Center':<30} {'Total AI Credits':>18} {'Users':>8}")
    report_lines.append(f"  {'-'*30} {'-'*18} {'-'*8}")

    sorted_centers = sorted(
        cost_center_breakdown.items(),
        key=lambda x: x[1]["credits"],
        reverse=True
    )

    total_center_credits = 0
    total_center_users = 0
    for center, data in sorted_centers:
        credits = data["credits"]
        users = _get_user_count(data)
        total_center_credits += credits
        total_center_users += users
        report_lines.append(f"  {center:<30} {credits:>18,.2f} {users:>8}")

    report_lines.append(f"  {'-'*30} {'-'*18} {'-'*8}")
    report_lines.append(
        f"  {'TOTAL':<30} {total_center_credits:>18,.2f} {total_center_users:>8}"
    )
    report_lines.append("")
    report_lines.append("")

    # Model wise breakdown
    report_lines.append("MODEL WISE AI CREDIT USAGE")
    report_lines.append("-" * 60)
    report_lines.append(f"  {'Model Name':<40} {'Total AI Credits':>18}")
    report_lines.append(f"  {'-'*40} {'-'*18}")

    sorted_models = sorted(
        model_breakdown.items(),
        key=lambda x: x[1],
        reverse=True
    )

    total_model_credits = 0
    for model, credits in sorted_models:
        total_model_credits += credits
        report_lines.append(f"  {model:<40} {credits:>18,.2f}")

    report_lines.append(f"  {'-'*40} {'-'*18}")
    report_lines.append(f"  {'TOTAL':<40} {total_model_credits:>18,.2f}")
    report_lines.append("")

    report_text = "\n".join(report_lines)

    # Generate CSV
    csv_output = io.StringIO()
    writer = csv.writer(csv_output)

    # Overall metrics
    writer.writerow(["GitHub Copilot USAGE SUMMARY REPORT", month_name.upper()])
    writer.writerow([])
    writer.writerow(["OVERALL METRICS"])
    writer.writerow(["Pooled Credits (Allocated)", pooled_credits])
    writer.writerow(["Total AI Credits Used", f"{total_credits:.2f}"])
    writer.writerow(["Total Unique Users", unique_users])
    writer.writerow([])

    # Cost Center breakdown
    writer.writerow(["COST CENTER WISE AI CREDIT USAGE"])
    writer.writerow(["Cost Center", "Total AI Credits", "Users"])
    for center, data in sorted_centers:
        writer.writerow([center, f"{data['credits']:.2f}", _get_user_count(data)])
    writer.writerow(["TOTAL", f"{total_center_credits:.2f}", total_center_users])
    writer.writerow([])

    # Model breakdown
    writer.writerow(["MODEL WISE AI CREDIT USAGE"])
    writer.writerow(["Model Name", "Total AI Credits"])
    for model, credits in sorted_models:
        writer.writerow([model, f"{credits:.2f}"])
    writer.writerow(["TOTAL", f"{total_model_credits:.2f}"])

    csv_text = csv_output.getvalue()

    return report_text, csv_text


def main():
    # Configuration from environment variables
    enterprise = os.environ.get("ENTERPRISE_SLUG")
    token = os.environ.get("GH_TOKEN")
    month_selection = os.environ.get("MONTH_SELECTION", "last_month")

    if not enterprise:
        print("Error: ENTERPRISE_SLUG environment variable is required.")
        sys.exit(1)

    if not token:
        print("Error: GH_TOKEN environment variable is required.")
        sys.exit(1)

    # Calculate date range
    start_date, end_date = get_billing_month_dates(month_selection)
    month_name = start_date.strftime("%B %Y")
    year = start_date.year
    month = start_date.month

    print(f"Generating Copilot Usage Report for: {month_name}")
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print()

    # ── Primary data sources ──────────────────────────────────────────────────
    # 1. Enterprise billing usage  → actual AI credits consumed + cost-center labels
    print("Fetching enterprise billing usage (AI credits)...")
    billing_usage = fetch_enterprise_billing_usage(enterprise, token, year, month)

    # 2. Cost centers list  → maps cost-center IDs/names to their org resources
    print("Fetching cost centers...")
    cost_centers = fetch_cost_centers(enterprise, token)

    # 3. Enterprise Copilot metrics  → daily aggregates (model breakdown, user counts)
    print("Fetching enterprise Copilot metrics...")
    org_report_data = fetch_copilot_org_report(enterprise, token, start_date, end_date)

    # 4. Billing/seats data  → allocated (included) AI credits per month
    print("Fetching billing information...")
    billing_data = fetch_copilot_billing(enterprise, token)

    # ── Process billing usage (best source for used credits + cost centers) ──
    billing_usage_processed = process_billing_usage_data(billing_usage, cost_centers)

    # ── Process enterprise metrics (best source for model breakdown + users) ──
    metrics_processed = None
    if org_report_data:
        print("Processing enterprise-level metrics data...")
        metrics_processed = process_metrics_data(org_report_data)

    # ── Assemble final report_data ────────────────────────────────────────────
    if billing_usage_processed and billing_usage_processed["total_credits"] > 0:
        # Billing usage API gave us real credit totals and cost-center data
        report_data = billing_usage_processed
        print(f"  Billing usage: {report_data['total_credits']:,.2f} AI credits used")

        # Supplement with metrics data where billing usage lacks detail
        if metrics_processed:
            if metrics_processed["model_breakdown"]:
                report_data["model_breakdown"] = metrics_processed["model_breakdown"]
            if metrics_processed["unique_users"] > report_data["unique_users"]:
                report_data["unique_users"] = metrics_processed["unique_users"]
            # The metrics API reflects near-real-time usage that may not yet be
            # reflected in the billing system (processing lag).  When the metrics
            # total is higher, we use it as the more up-to-date figure.  Note:
            # billing data is authoritative for invoicing; metrics is used here
            # solely to avoid under-reporting during the billing lag window.
            if metrics_processed["total_credits"] > report_data["total_credits"]:
                print(f"Note: Metrics API total ({metrics_processed['total_credits']:,.2f}) "
                      f"is higher than billing total ({report_data['total_credits']:,.2f}). "
                      f"Using metrics total (may include usage not yet reflected in billing).")
                report_data["total_credits"] = metrics_processed["total_credits"]

        # If billing usage had no cost-center labels on its items,
        # build the breakdown from org-level metrics as a fallback
        if not report_data["cost_center_breakdown"] and cost_centers:
            print("Building cost-center breakdown from org metrics (billing items "
                  "lacked cost-center labels)...")
            cc_breakdown = build_cost_center_metrics(
                cost_centers, token, start_date, end_date
            )
            if cc_breakdown:
                report_data["cost_center_breakdown"] = cc_breakdown

    elif metrics_processed and metrics_processed["total_credits"] > 0:
        # Billing usage API unavailable – fall back to enterprise metrics
        print("Billing usage API returned no data; using enterprise metrics.")
        report_data = {
            "total_credits": metrics_processed["total_credits"],
            "unique_users": metrics_processed["unique_users"],
            "model_breakdown": metrics_processed["model_breakdown"],
            "cost_center_breakdown": {}
        }

        # Build cost-center breakdown from org-level metrics
        if cost_centers:
            print("Building cost-center breakdown from org metrics...")
            cc_breakdown = build_cost_center_metrics(
                cost_centers, token, start_date, end_date
            )
            if cc_breakdown:
                report_data["cost_center_breakdown"] = cc_breakdown

    else:
        # ── Legacy API fallback ───────────────────────────────────────────────
        print("Primary APIs returned no data. Trying legacy APIs...")
        print("Fetching Copilot usage data (legacy)...")
        usage_data = fetch_copilot_usage(enterprise, token, start_date, end_date)

        print("Fetching metrics data (legacy)...")
        metrics_data = fetch_copilot_metrics(enterprise, token, start_date, end_date)

        if usage_data:
            report_data = process_usage_data(usage_data)
        else:
            report_data = {
                "total_credits": 0,
                "unique_users": 0,
                "cost_center_breakdown": {},
                "model_breakdown": {}
            }

        if metrics_data:
            legacy_metrics = process_metrics_data(metrics_data)
            if legacy_metrics["model_breakdown"]:
                report_data["model_breakdown"] = legacy_metrics["model_breakdown"]
            if legacy_metrics["total_credits"] > report_data["total_credits"]:
                report_data["total_credits"] = legacy_metrics["total_credits"]

        # Build cost-center breakdown from org metrics even in legacy path
        if cost_centers and not report_data["cost_center_breakdown"]:
            print("Building cost-center breakdown from org metrics (legacy path)...")
            cc_breakdown = build_cost_center_metrics(
                cost_centers, token, start_date, end_date
            )
            if cc_breakdown:
                report_data["cost_center_breakdown"] = cc_breakdown

        if report_data["total_credits"] == 0:
            print("\nWarning: No Copilot usage data could be retrieved from any API source.")
            print("Please verify:")
            print("  - ENTERPRISE_SLUG is correct")
            print("  - GH_TOKEN has 'manage_billing:copilot' and 'read:enterprise' scopes")
            print("  - The selected month has Copilot usage data")

    # Generate the report
    report_text, csv_text = generate_report(report_data, billing_data, month_name)

    # Print report to console
    print("\n" + "=" * 60)
    print(report_text)

    # Save report files
    os.makedirs("reports", exist_ok=True)
    report_filename = f"reports/copilot_usage_report_{start_date.strftime('%Y_%m')}.txt"
    csv_filename = f"reports/copilot_usage_report_{start_date.strftime('%Y_%m')}.csv"

    with open(report_filename, "w") as f:
        f.write(report_text)

    with open(csv_filename, "w") as f:
        f.write(csv_text)

    print(f"\nReport saved to: {report_filename}")
    print(f"CSV saved to: {csv_filename}")

    # Set output for GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"report_file={report_filename}\n")
            f.write(f"csv_file={csv_filename}\n")
            f.write(f"month_name={month_name}\n")

    # Also set the report as step summary
    github_step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        with open(github_step_summary, "a") as f:
            f.write(f"## GitHub Copilot Usage Report - {month_name}\n\n")
            f.write("```\n")
            f.write(report_text)
            f.write("\n```\n")


if __name__ == "__main__":
    main()

