"""
GitHub Copilot Detailed Usage Report Generator

Fetches Copilot usage data from the GitHub API and generates a summary report
with overall metrics, cost center-wise usage, and model-wise usage.

Updated for the June 2026 AI Credits billing model:
  - Uses the new Copilot metrics reports API (NDJSON download-based)
  - Supports per-user ai_credits_used tracking
  - Fetches Copilot seat assignments for accurate user counts
  - Uses included_ai_credits from the billing API for pooled credits
"""

import os
import sys
import json
import csv
import io
import time
from datetime import datetime, timedelta
from collections import defaultdict

import requests

# Ensure all print() output is immediately flushed to the log
sys.stdout.reconfigure(line_buffering=True)


def extract_download_links(api_data):
    """
    Extract NDJSON download links from API responses across formats.

    Args:
        api_data: API response body, which may be a dict, str, list, or other type.

    Returns:
        A list of signed download URL strings.
    """
    if isinstance(api_data, dict):
        links = api_data.get("download_links", [])
        if isinstance(links, str):
            return [links] if links.strip() else []
        if isinstance(links, list):
            valid_links = [link for link in links if isinstance(link, str) and link.strip()]
            if len(valid_links) != len(links):
                print("Warning: Ignoring non-string or empty values in download_links response.")
            return valid_links
        return []

    # Some responses return a direct signed URL or a list of URLs
    if isinstance(api_data, str):
        return [api_data] if api_data.strip() else []
    if isinstance(api_data, list):
        if not api_data:
            return []
        valid_links = [item for item in api_data if isinstance(item, str) and item.strip()]
        invalid_count = len(api_data) - len(valid_links)
        if invalid_count:
            print(f"Warning: Ignoring {invalid_count} non-string or empty values in download link list response.")
        return valid_links

    return []


def extract_inline_records(api_data):
    """
    Extract inline metric records when API returns a list of objects.

    Args:
        api_data: API response body, expected to sometimes be a list.

    Returns:
        A list of dictionary records.
    """
    if isinstance(api_data, list):
        records = [item for item in api_data if isinstance(item, dict)]
        if len(records) != len(api_data):
            print("Warning: Ignoring non-object inline metric records.")
        return records
    return []


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


def _get_with_retry(url, headers, params=None, timeout=(10, 30), max_retries=3):
    """
    Perform a GET request with exponential-backoff retry on transient errors.

    Uses a (connect_timeout, read_timeout) tuple so a stalled connection is
    detected quickly while still allowing the server up to 30 s to respond.

    Retries on connection/timeout errors and on HTTP 429 (rate limit) or
    5xx (server) responses.

    Returns the response object on success, or raises the last exception if
    all retries are exhausted.
    """
    delay = 5  # initial delay in seconds between retry attempts
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=timeout)
            # Retry on rate-limit or transient server errors
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == max_retries:
                    return response
                print(f"Warning: HTTP {response.status_code} (attempt {attempt}/{max_retries}). "
                      f"Retrying in {delay}s...")
                time.sleep(delay)
                delay *= 2
                continue
            return response
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt == max_retries:
                raise
            print(f"Warning: Request failed (attempt {attempt}/{max_retries}): {exc}. "
                  f"Retrying in {delay}s...")
            time.sleep(delay)
            delay *= 2  # exponential backoff


# ---------------------------------------------------------------------------
# NEW: Copilot metrics reports API (NDJSON-based, replaces legacy metrics)
# ---------------------------------------------------------------------------

def fetch_copilot_metrics_report(enterprise, token, start_date, end_date):
    """
    Fetch enterprise Copilot metrics using the new reports API.

    The new API (GA as of 2026) returns download links to NDJSON files
    instead of inline JSON.  We iterate day-by-day and download each report.

    Endpoint: GET /enterprises/{enterprise}/copilot/metrics/reports/enterprise-1-day
              ?day=YYYY-MM-DD

    Falls back to the legacy /copilot/metrics endpoint if the reports API
    is unavailable.
    """
    headers = get_auth_headers(token)
    all_data = []

    current = start_date
    while current <= end_date:
        day_str = current.strftime("%Y-%m-%d")
        url = (f"https://api.github.com/enterprises/{enterprise}"
               f"/copilot/metrics/reports/enterprise-1-day")
        params = {"day": day_str}

        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            download_links = extract_download_links(data)
            if download_links:
                ndjson_data = download_ndjson(download_links)
                for record in ndjson_data:
                    record.setdefault("date", day_str)
                all_data.extend(ndjson_data)
            elif isinstance(data, list):
                # Some API versions may still return inline data
                all_data.extend(extract_inline_records(data))
        elif response.status_code == 404:
            # Reports API not available — caller will fall back to legacy
            print("Note: Copilot metrics reports API not available. "
                  "Will try legacy metrics endpoint.")
            return None
        elif response.status_code in (204, 422):
            pass  # No data for this day
        elif response.status_code in (401, 403):
            # Access denied — no point retrying for each remaining day.
            print(f"Warning: Metrics report API returned {response.status_code} "
                  f"(access denied). Stopping day-by-day iteration.")
            return None
        else:
            print(f"Warning: Metrics report API returned {response.status_code} "
                  f"for {day_str}")

        current += timedelta(days=1)

    return all_data if all_data else None


def fetch_copilot_user_metrics(enterprise, token, start_date, end_date):
    """
    Fetch per-user Copilot metrics using the new reports API.

    Endpoint: GET /enterprises/{enterprise}/copilot/metrics/reports/users-1-day
              ?day=YYYY-MM-DD

    Each NDJSON line contains per-user data including ai_credits_used.
    Returns a list of per-user records across all days in the range.
    """
    headers = get_auth_headers(token)
    all_user_data = []

    current = start_date
    while current <= end_date:
        day_str = current.strftime("%Y-%m-%d")
        url = (f"https://api.github.com/enterprises/{enterprise}"
               f"/copilot/metrics/reports/users-1-day")
        params = {"day": day_str}

        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            download_links = extract_download_links(data)
            if download_links:
                ndjson_data = download_ndjson(download_links)
                for record in ndjson_data:
                    record.setdefault("date", day_str)
                all_user_data.extend(ndjson_data)
            elif isinstance(data, list):
                all_user_data.extend(extract_inline_records(data))
        elif response.status_code == 404:
            print("Note: Per-user metrics reports API not available.")
            return None
        elif response.status_code in (204, 422):
            pass  # No data for this day
        elif response.status_code in (401, 403):
            # Access denied — no point retrying for each remaining day.
            print(f"Warning: User metrics report API returned {response.status_code} "
                  f"(access denied). Stopping day-by-day iteration.")
            return None
        else:
            print(f"Warning: User metrics report API returned "
                  f"{response.status_code} for {day_str}")

        current += timedelta(days=1)

    return all_user_data if all_user_data else None


def fetch_copilot_seats(enterprise, token):
    """
    Fetch all Copilot seat assignments for the enterprise.

    Endpoint: GET /enterprises/{enterprise}/copilot/billing/seats

    Returns a list of seat objects containing user info, organization,
    and assignment details.  Used for accurate user counts.
    """
    headers = get_auth_headers(token)
    all_seats = []
    page = 1

    while True:
        url = f"https://api.github.com/enterprises/{enterprise}/copilot/billing/seats"
        params = {"page": page, "per_page": 100}
        response = requests.get(url, headers=headers, params=params, timeout=30)

        if response.status_code == 200:
            data = response.json()
            seats = data.get("seats", [])
            if not seats:
                break
            all_seats.extend(seats)
            if len(seats) < 100:
                break
            page += 1
        elif response.status_code in (403, 404):
            print(f"Note: Copilot seats API returned {response.status_code}. "
                  f"Will estimate user count from other sources.")
            return None
        else:
            print(f"Warning: Copilot seats API returned {response.status_code}")
            return None

    return all_seats


def download_ndjson(download_links):
    """Download and parse NDJSON files from signed URLs."""
    all_data = []
    for url in download_links:
        if not url.strip():
            print("Warning: Skipping empty download URL.")
            continue
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

    As of June 2026, the response includes:
      - included_ai_credits: total monthly AI credits pool
      - ai_credits_used: total AI credits consumed this billing cycle
      - seat_breakdown: seat assignment details
    """
    headers = get_auth_headers(token)

    billing_url = f"https://api.github.com/enterprises/{enterprise}/copilot/billing"
    response = requests.get(billing_url, headers=headers, timeout=30)

    if response.status_code == 200:
        data = response.json()
        if isinstance(data, dict):
            top_keys = list(data.keys())
            print(f"  Billing API response keys: {top_keys}")
            # Log the full response as JSON to help diagnose field names for
            # ai_credits_used / included_ai_credits in the new billing model.
            print(f"  Billing API full response: {json.dumps(data, default=str)}")
            ai_obj = data.get("ai_credits")
            if isinstance(ai_obj, dict):
                print(f"  Billing API 'ai_credits' sub-keys: {list(ai_obj.keys())}")
            elif ai_obj is not None:
                print(f"  Billing API 'ai_credits' value (non-dict): {ai_obj}")
        return data
    else:
        print(f"Warning: Could not fetch billing data: {response.status_code}")
        return None


def fetch_enterprise_billing_usage(enterprise, token, year, month):
    """
    Fetch enterprise billing usage line items for a specific month.

    Endpoint: GET /enterprises/{enterprise}/settings/billing/usage
              ?year=YYYY&month=MM

    Returns a dict with a ``usageItems`` list containing per-day, per-SKU
    line items (product, sku, quantity, unitType, organizationName, etc.).
    Handles pagination via the HTTP Link header (GitHub standard) with a
    MAX_BILLING_PAGES safety cap to prevent infinite loops on large enterprises.

    Note: For very large enterprises the billing usage dataset can contain
    millions of records (one per product/SKU/org/day).  When the cap is
    reached, Copilot AI-credit totals are sourced from the Copilot-specific
    APIs (copilot/billing, per-user metrics) instead.
    """
    # Safety cap: stop after this many pages regardless of Link header.
    # At ~1.5 s/page this limits the billing-usage fetch to ≈5 minutes,
    # leaving the rest of the workflow time budget for other steps.
    MAX_BILLING_PAGES = 200

    headers = get_auth_headers(token)
    url = f"https://api.github.com/enterprises/{enterprise}/settings/billing/usage"

    all_items = []
    per_page = 100
    page = 1
    # Records the actual items-per-page the API delivers on its first response.
    # The server may ignore per_page and return a larger fixed chunk (e.g. 2729
    # items instead of 100).  We capture it once so the fallback break
    # condition can compare against the correct observed page size.
    observed_page_size = None

    while page <= MAX_BILLING_PAGES:
        params = {"year": year, "month": month, "page": page, "per_page": per_page}
        try:
            response = _get_with_retry(url, headers, params=params)
        except requests.exceptions.RequestException as exc:
            print(f"Warning: Billing usage API request failed after retries: {exc}. "
                  f"Returning {len(all_items)} items collected so far.")
            break

        if response.status_code == 200:
            data = response.json()
            items = data.get("usageItems", [])
            all_items.extend(items)
            print(f"  Billing usage page {page}: {len(items)} items "
                  f"(total so far: {len(all_items)})")

            # Guard: an empty page means there is no more data.
            if len(items) == 0:
                break

            # Record observed page size from the first non-empty page. Used as
            # fallback pagination when the API does not return Link headers.
            if observed_page_size is None:
                observed_page_size = len(items)

            # Primary pagination: GitHub Link header signals the next page.
            link_header = response.headers.get("Link", "")
            has_next_page = 'rel="next"' in link_header

            # Fallback pagination: some responses omit Link headers.
            # Without an explicit next-page link, keep fetching while pages are
            # full-size; stop on the first short page.
            if not has_next_page and len(items) < observed_page_size:
                break

            page += 1
        elif response.status_code == 403:
            print("Note: Billing usage API access forbidden. "
                  "Ensure the token has 'manage_billing:copilot' and "
                  "'read:enterprise' scopes.")
            return None
        elif response.status_code == 404:
            print("Note: Billing usage API not available for this enterprise.")
            return None
        else:
            print(f"Note: Billing usage API returned {response.status_code}")
            return None

    # page exceeds MAX_BILLING_PAGES only when the last full page was fetched
    # and then page was incremented past the cap — i.e. the loop exited via the
    # while condition rather than a break, meaning more pages still exist.
    if page > MAX_BILLING_PAGES:
        print("Warning: Billing usage API page limit reached. "
              "For large enterprises the dataset may be incomplete; "
              "AI-credit totals will fall back to Copilot-specific API sources.")

    return {"usageItems": all_items} if all_items else None


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


def _extract_username(record):
    """Extract a username from a user data record, handling multiple formats."""
    username = record.get("login") or record.get("user_login") or ""
    if not username:
        user_field = record.get("user")
        if isinstance(user_field, dict):
            username = user_field.get("login", "")
        elif user_field:
            username = str(user_field)
    return username or "Unknown"


def process_user_report_data(user_data, user_teams_data):
    """
    Process user-level report data (NDJSON) to extract per-user and per-team breakdowns.

    Supports both legacy format (copilot_ide_code_completions nested structure)
    and new format (flat ai_credits_used field).
    """
    total_credits = 0
    unique_users = set()
    cost_center_credits = defaultdict(lambda: {"credits": 0, "users": set()})
    model_credits = defaultdict(float)

    # Build user-to-team mapping from user-teams report
    user_team_map = {}
    for entry in user_teams_data:
        login = entry.get("login", "")
        team = entry.get("team_slug") or entry.get("team", "")
        if login and team:
            user_team_map[login] = team

    for record in user_data:
        username = _extract_username(record)
        unique_users.add(username)
        team = user_team_map.get(username, "Not Assigned")

        # New format: flat ai_credits_used field (June 2026+)
        ai_credits = record.get("ai_credits_used", 0) or 0
        if ai_credits > 0:
            total_credits += ai_credits
            cost_center_credits[team]["credits"] += ai_credits
            cost_center_credits[team]["users"].add(username)
            continue

        # Legacy format: nested feature data
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


def process_per_user_metrics(user_metrics_data, seats_data=None):
    """
    Process per-user metrics from the new reports API (ai_credits_used).

    Each record has:
      user_login, ai_credits_used, and optionally organization, model info.

    If seats_data is provided, it is used to map users to organizations
    for cost-center attribution.
    """
    total_credits = 0
    unique_users = set()
    user_credits_map = defaultdict(float)
    org_credits = defaultdict(lambda: {"credits": 0.0, "users": set()})

    # Build user → org mapping from seats data
    user_org_map = {}
    if seats_data:
        for seat in seats_data:
            assignee = seat.get("assignee", {})
            login = assignee.get("login", "") if isinstance(assignee, dict) else ""
            org = (seat.get("organization", {}).get("login", "")
                   if isinstance(seat.get("organization"), dict) else
                   seat.get("organization", ""))
            if login:
                user_org_map[login] = org or "Not Assigned"

    for record in user_metrics_data:
        username = _extract_username(record)
        credits = float(record.get("ai_credits_used", 0) or 0)
        org = record.get("organization") or user_org_map.get(username, "Not Assigned")

        unique_users.add(username)
        user_credits_map[username] += credits
        total_credits += credits
        org_credits[org]["credits"] += credits
        org_credits[org]["users"].add(username)

    return {
        "total_credits": total_credits,
        "unique_users": len(unique_users),
        "user_credits": dict(user_credits_map),
        "org_breakdown": dict(org_credits),
        "cost_center_breakdown": {},
        "model_breakdown": {}
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
    model_breakdown = defaultdict(lambda: {
        "total": 0.0,
        "included": 0.0,
        "additional": 0.0
    })
    found_copilot_items = False

    for item in usage_items:
        product = (item.get("product") or "").lower()
        sku = (item.get("sku") or "").lower()
        unit_type = (item.get("unitType") or "").lower()

        # Match Copilot AI credit / premium-request line items.
        # Known GitHub billing SKUs (as of 2025-2026):
        #   "Copilot Premium Requests"          – standard included AI credits
        #   "Copilot Add-on Premium Requests"   – additional (paid) AI credits
        #   "Copilot AI Credits"                – alternative SKU name
        #   "Copilot Premium Model Requests"    – premium model variant
        #   "Copilot Credits"                   – simplified SKU variant
        #   "Copilot AI Requests"               – another variant
        #   "Copilot Model Requests"            – model-specific variant
        #   "Copilot Usage"                     – generic usage SKU variant
        #   "Copilot Premium Usage"             – premium usage variant
        # We require the item to be Copilot-branded AND to reference one of the
        # specific AI-credit-related SKU phrases to avoid matching seat/license
        # or other Copilot line items (e.g. "Copilot for Business Seat").
        is_copilot = "copilot" in product or "copilot" in sku
        # Match only AI-usage SKUs; do NOT fall back on unit_type alone because
        # that can accidentally include seat/license line items.
        is_ai_usage = any(kw in sku for kw in (
            "premium request", "ai credit", "premium model",
            "add-on premium", "addon premium",
            "copilot credit", "ai request",
            "model request", "premium usage",
            "copilot usage",
        ))
        # Broader fallback: Copilot product with request/credit unit type,
        # but only when the SKU does NOT look like a seat/license line item.
        _is_seat_sku = any(kw in sku for kw in ("seat", "license", "subscription", "user"))
        if not is_ai_usage and is_copilot and not _is_seat_sku:
            is_ai_usage = unit_type in ("request", "requests", "credit", "credits")

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

        # Determine if this is included or additional credit
        # GitHub's add-on SKUs are specifically prefixed with "Copilot Add-on" or "Copilot Addon"
        # Check for these standard patterns to distinguish paid add-on credits from included credits
        # Using lowercase keywords since sku was already lowercased
        is_additional = "add-on premium" in sku or "addon premium" in sku

        # Extract model name if available
        # The GitHub billing API may include model information in different field names
        # depending on the API version and response format:
        # - modelName/model_name: Standard field names for AI model identification
        # - model: Alternative field name
        # Defaults to "Unknown Model" if no model field is present in the billing item
        model_name = "Unknown Model"
        for field in ("modelName", "model_name", "model"):
            value = item.get(field, "").strip()
            if value:
                model_name = value
                break

        total_credits += quantity
        cost_center_credits[cc_name]["credits"] += quantity

        # Track model breakdown with included/additional distinction
        model_data = model_breakdown[model_name]
        model_data["total"] += quantity
        if is_additional:
            model_data["additional"] += quantity
        else:
            model_data["included"] += quantity

    if not found_copilot_items or total_credits == 0:
        return None

    # Convert defaultdicts to regular dicts for cleaner return
    result = {
        "total_credits": total_credits,
        "unique_users": 0,
        "cost_center_breakdown": dict(cost_center_credits),
        "model_breakdown": dict(model_breakdown)
    }

    return result


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


def _coerce_number(value):
    """Convert API numeric values to float, tolerating numeric strings."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _has_named_models(model_breakdown):
    """Return True when a model breakdown contains at least one named model."""
    if not model_breakdown:
        return False

    for model_name in model_breakdown.keys():
        normalized = str(model_name or "").strip().lower()
        if normalized not in ("", "unknown", "unknown model"):
            return True

    return False


def _enrich_cost_center_users(cost_center_breakdown, per_user_org_breakdown, per_user_unique_users):
    """
    Add user counts to billing-derived cost-center rows using per-user org data.

    Billing usage line items provide accurate credit totals but usually do not
    include per-user attribution, which leaves Users = 0 in cost-center rows.
    This helper overlays users from per-user metrics where cost-center names
    match, and falls back to the overall active-user count for a single
    "Not Assigned" row when no direct mapping is available.
    """
    if not cost_center_breakdown:
        return cost_center_breakdown
    if not per_user_org_breakdown:
        return cost_center_breakdown

    enriched = {}
    matched_any_center = False

    for center, data in cost_center_breakdown.items():
        entry = dict(data)
        users_value = entry.get("users")
        if isinstance(users_value, set):
            users = set(users_value)
        elif isinstance(users_value, (list, tuple)):
            users = set(users_value)
        else:
            users = set()

        org_entry = per_user_org_breakdown.get(center)
        if isinstance(org_entry, dict):
            org_users_value = org_entry.get("users")
            if isinstance(org_users_value, set):
                org_users = set(org_users_value)
            elif isinstance(org_users_value, (list, tuple)):
                org_users = set(org_users_value)
            else:
                org_users = set()
            if org_users:
                users.update(org_users)
                matched_any_center = True

        entry["users"] = users
        if users:
            entry["user_count"] = len(users)
        enriched[center] = entry

    single_center_name = None
    if len(enriched) == 1:
        single_center_name = next(iter(enriched))

    no_matches_found = not matched_any_center
    is_single_not_assigned = single_center_name == "Not Assigned"
    has_active_users = per_user_unique_users > 0

    if (no_matches_found and is_single_not_assigned and has_active_users and
            "Not Assigned" in enriched):
        enriched["Not Assigned"]["user_count"] = per_user_unique_users

    return enriched


# ---------------------------------------------------------------------------
# AI credits per assigned seat per month – values from GitHub official docs:
# https://docs.github.com/en/copilot/concepts/billing/
#         usage-based-billing-for-organizations-and-enterprises
# ---------------------------------------------------------------------------

# Standard (post-promo) rates
_AI_CREDITS_PER_SEAT = {
    "enterprise": 3900,
    "business":   1900,
}
_AI_CREDITS_PER_SEAT_DEFAULT = 1900  # fallback when plan_type is unknown

# Promotional rates for existing customers: June 1 – September 1, 2026
_AI_CREDITS_PER_SEAT_PROMO = {
    "enterprise": 7000,
    "business":   3000,
}
_AI_CREDITS_PROMO_START = datetime(2026, 6, 1)
_AI_CREDITS_PROMO_END   = datetime(2026, 9, 1)  # exclusive (promo ends Aug 31)

# AI credits billing model was introduced in June 2026.
# Do not attempt to report AI credits for months before this date.
_AI_CREDITS_MIN_DATE = datetime(2026, 6, 1)


def compute_included_credits(seats_data, billing_month_start):
    """
    Compute the total pooled (allocated) AI credits for a billing month.

    Each seat contributes credits based on its plan_type (business or enterprise)
    and whether the billing month falls within the promotional period
    (June 1 – September 1, 2026).  Credits are pooled at the enterprise level,
    so this returns the enterprise-wide pool for the month.

    The enterprise seats API can return duplicate seat objects when the same user
    is granted Copilot access through multiple organisations or enterprise teams.
    This function deduplicates by user login, keeping the highest-value plan per
    user (enterprise > business > unknown), so that each user is counted exactly
    once regardless of how many orgs they belong to.

    Args:
        seats_data:           List of seat objects from the Copilot seats API.
                              Each object must have a ``plan_type`` field.
        billing_month_start:  datetime for the first day of the billing month.

    Returns:
        int  – total AI credits in the pool, or None if seats_data is empty.
    """
    if not seats_data:
        return None

    is_promo = _AI_CREDITS_PROMO_START <= billing_month_start < _AI_CREDITS_PROMO_END
    rates = _AI_CREDITS_PER_SEAT_PROMO if is_promo else _AI_CREDITS_PER_SEAT

    # Deduplicate by user login, keeping the highest-rate plan per user.
    # Plan rank (higher = better): enterprise > business > unknown/other.
    _plan_rank = {"enterprise": 2, "business": 1}
    unique_user_plans = {}  # login → plan_type
    _anon_counter = 0

    for seat in seats_data:
        assignee = seat.get("assignee") or {}
        login = (assignee.get("login", "") if isinstance(assignee, dict) else "")
        plan = (seat.get("plan_type") or "").lower().strip()

        if not login:
            # No login to deduplicate on – include directly with a stable key.
            # This should be rare but we handle it gracefully.
            _anon_counter += 1
            login = f"__anon_{_anon_counter}"

        current_rank = _plan_rank.get(unique_user_plans.get(login, ""), -1)
        new_rank = _plan_rank.get(plan, 0)
        if login not in unique_user_plans or new_rank > current_rank:
            unique_user_plans[login] = plan

    total = 0
    for plan in unique_user_plans.values():
        rate = rates.get(plan, _AI_CREDITS_PER_SEAT_DEFAULT)
        total += rate

    return total


def compute_included_credits_from_billing(billing_data, billing_month_start):
    """
    Compute allocated AI credits directly from the Copilot billing API response.

    The ``/enterprises/{enterprise}/copilot/billing`` endpoint returns the
    enterprise-wide ``plan_type`` and the ``seat_breakdown`` which includes
    ``active_this_cycle`` — the number of seats that were active during the
    current billing cycle.  Together these allow us to compute the pooled
    credit allocation without having to list every individual seat.

    Falls back to the per-plan default rate when the plan_type is not
    explicitly recognised (e.g. "team", "free", or absent).

    Args:
        billing_data:         Response dict from the Copilot billing API.
        billing_month_start:  datetime for the first day of the billing month.

    Returns:
        int  – total allocated AI credits, or None if the required fields are
               absent from the billing response.
    """
    if not billing_data:
        return None

    plan = (billing_data.get("plan_type") or "").lower().strip()
    seat_breakdown = billing_data.get("seat_breakdown") or {}
    # active_this_cycle is the most accurate count for the current billing period
    active_seats = (
        seat_breakdown.get("active_this_cycle") or
        seat_breakdown.get("total") or
        0
    )
    active_seats = int(active_seats)
    if active_seats == 0 or not plan:
        return None

    is_promo = _AI_CREDITS_PROMO_START <= billing_month_start < _AI_CREDITS_PROMO_END
    rates = _AI_CREDITS_PER_SEAT_PROMO if is_promo else _AI_CREDITS_PER_SEAT
    rate = rates.get(plan, _AI_CREDITS_PER_SEAT_DEFAULT)

    total = active_seats * rate
    period_label = "promotional" if is_promo else "standard"
    print(f"  Allocated credits: {active_seats} active seat(s) "
          f"× {rate:,} ({period_label} rate) = {total:,}")
    return total


def generate_report(report_data, billing_data, month_name,
                    seats_data=None, billing_start_date=None, is_current_month=False):
    """
    Generate the usage report as a formatted string and CSV.

    Args:
        report_data:          Aggregated metrics (total_credits, unique_users, breakdowns).
        billing_data:         Raw response from the Copilot billing API (may be None).
        month_name:           Human-readable month string, e.g. "June 2026".
        seats_data:           List of seat objects from the Copilot seats API.  Used to
                              build the licensed-users list and compute pooled credits.
        billing_start_date:   datetime for the first day of the billing month.  Required
                              to select the correct AI-credit rate (promo vs standard).
        is_current_month:     True when the selected month is the current billing cycle.
                              The Copilot billing API only returns live data for the
                              current cycle; for historical months its included_ai_credits
                              field reflects the current seat count, not the historical one.
    """
    total_credits = report_data["total_credits"]
    unique_users = report_data["unique_users"]
    cost_center_breakdown = report_data["cost_center_breakdown"]
    model_breakdown = report_data["model_breakdown"]

    # Get pooled (allocated) credits.
    # Priority:
    #   1. Per-seat computation (compute_included_credits): iterates every seat's
    #      own plan_type and deduplicates by user login.  This is the most accurate
    #      approach for mixed Business + Enterprise plans and is always preferred
    #      when seats_data is available.
    #   2. Billing API direct field (included_ai_credits etc.): only used as a
    #      fallback when seats_data is absent.  The billing API's seat count may
    #      differ from the actual billed-user count (e.g. it may include duplicates
    #      for users in multiple organisations), which can produce incorrect totals.
    #   3. Billing API seat-count computation (compute_included_credits_from_billing):
    #      last resort — uses a single enterprise-level plan_type applied to all seats,
    #      which is inaccurate for mixed-plan enterprises.
    #
    # NOTE: The Copilot billing API (/enterprises/{e}/copilot/billing) has no month
    # parameter — it always reflects the CURRENT billing cycle.  Any direct field
    # values from it are therefore only valid when is_current_month=True.
    pooled_credits = "N/A"

    # ── Priority 1: per-seat computation (most accurate) ─────────────────────
    if seats_data and billing_start_date:
        computed = compute_included_credits(seats_data, billing_start_date)
        if computed:
            pooled_credits = computed
            is_promo = (_AI_CREDITS_PROMO_START <= billing_start_date
                        < _AI_CREDITS_PROMO_END)
            period_label = "promotional" if is_promo else "standard"
            month_label = "current month" if is_current_month else "selected month"
            # Count unique users (deduplicated) for the log line.
            unique_logins = set()
            for _seat in seats_data:
                _assignee = _seat.get("assignee") or {}
                _login = (_assignee.get("login", "")
                          if isinstance(_assignee, dict) else "")
                if _login:
                    unique_logins.add(_login)
            n_unique = len(unique_logins) or len(seats_data)
            print(f"  Pooled credits computed from {n_unique} unique licensed seat(s) "
                  f"({len(seats_data)} total seat entries, deduplicated) "
                  f"using per-seat plan_type ({period_label} rates, {month_label}) "
                  f"→ {pooled_credits:,}")

    # ── Priority 2: billing API direct field (current month only) ────────────
    # Only use when per-seat data is absent, to avoid replacing accurate per-seat
    # values with potentially miscounted API totals.
    if pooled_credits == "N/A" and billing_data and is_current_month:
        ai_credits_nested = billing_data.get("ai_credits") or {}
        if not isinstance(ai_credits_nested, dict):
            ai_credits_nested = {}

        included_candidates = (
            billing_data.get("included_ai_credits"),
            billing_data.get("total_included_ai_credits"),
            billing_data.get("allocated_ai_credits"),
            billing_data.get("monthly_included_ai_credits"),
            billing_data.get("copilot_included_ai_credits"),
            billing_data.get("ai_credits_included"),
            billing_data.get("ai_credits_limit"),
            billing_data.get("total_ai_credits"),
            billing_data.get("ai_credits_pool"),
            ai_credits_nested.get("included"),
            ai_credits_nested.get("included_this_cycle"),
            ai_credits_nested.get("included_this_billing_cycle"),
            ai_credits_nested.get("allocated"),
            ai_credits_nested.get("allocated_this_cycle"),
            ai_credits_nested.get("limit"),
            ai_credits_nested.get("cycle_limit"),
            ai_credits_nested.get("total"),
            ai_credits_nested.get("pool"),
            ai_credits_nested.get("purchased"),
            ai_credits_nested.get("credits"),
            ai_credits_nested.get("available_this_cycle"),
        )
        for value in included_candidates:
            parsed = _coerce_number(value)
            if parsed is not None:
                pooled_credits = int(parsed)
                print(f"  Pooled credits from billing API (current cycle): {pooled_credits:,}")
                break

        if pooled_credits == "N/A":
            print("  Note: Billing API response did not contain a recognised "
                  "included_ai_credits field (pooled credits).")

    # ── Priority 3: billing API seat-count computation (last resort) ──────────
    if pooled_credits == "N/A" and billing_data and billing_start_date:
        computed = compute_included_credits_from_billing(billing_data, billing_start_date)
        if computed:
            pooled_credits = computed
            month_label = "current month" if is_current_month else "selected month"
            print(f"  Pooled credits computed from billing API seat data "
                  f"(for {month_label}): {pooled_credits:,}")

    if pooled_credits == "N/A":
        print("Warning: Could not determine pooled (allocated) AI credits. "
              "No seats data available and the billing API did not return "
              "plan_type / seat_breakdown data.")

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
    report_lines.append(f"  Total Unique Active Users: {unique_users:,}")
    report_lines.append("")
    report_lines.append("")

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

    # Determine format early for consistent use in both text and CSV
    has_detailed_format = any(isinstance(data, dict) for data in model_breakdown.values())

    # Model wise breakdown
    report_lines.append("MODEL WISE AI CREDIT USAGE")
    report_lines.append("-" * 90)
    report_lines.append(f"  {'Model Name':<30} {'Included':>15} {'Additional':>15}")
    report_lines.append(f"  {'-'*30} {'-'*15} {'-'*15}")

    sorted_models = []
    for model_name, model_data in model_breakdown.items():
        if isinstance(model_data, dict):
            # New format with included/additional breakdown
            total = model_data.get("total", 0.0)
        else:
            # Old format - just a float total
            total = model_data
        sorted_models.append((model_name, model_data, total))

    # Sort by total credits in descending order
    sorted_models = sorted(sorted_models, key=lambda x: x[2], reverse=True)

    total_model_credits = 0
    total_included = 0.0
    total_additional = 0.0
    
    for model_name, model_data, total in sorted_models:
        total_model_credits += total
        
        if isinstance(model_data, dict):
            # New format with included/additional breakdown
            included = model_data.get("included", 0.0)
            additional = model_data.get("additional", 0.0)
            total_included += included
            total_additional += additional
            report_lines.append(f"  {model_name:<30} {included:>15,.2f} {additional:>15,.2f}")
        else:
            # Old format - just total
            report_lines.append(f"  {model_name:<30} {'-':>15} {'-':>15}")

    report_lines.append(f"  {'-'*30} {'-'*15} {'-'*15}")
    # Use has_detailed_format computed earlier
    if has_detailed_format:
        report_lines.append(
            f"  {'TOTAL':<30} {total_included:>15,.2f} {total_additional:>15,.2f}"
        )
    else:
        report_lines.append(f"  {'TOTAL':<30} {'-':>15} {'-':>15}")
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
    writer.writerow(["Total Unique Active Users", unique_users])
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
    # Write model breakdown based on format:
    # - If detailed format (included/additional) is available: write detailed columns
    # - Else if models exist: write simple total columns
    # - Else: write detailed header for consistency with text report
    if has_detailed_format and sorted_models:
        # New format with included/additional breakdown
        writer.writerow(["Model Name", "Included Credits", "Additional Credits"])
        for model_name, model_data, total in sorted_models:
            if isinstance(model_data, dict):
                included = model_data.get("included", 0.0)
                additional = model_data.get("additional", 0.0)
                writer.writerow([model_name, f"{included:.2f}", f"{additional:.2f}"])
        writer.writerow(["TOTAL", f"{total_included:.2f}", f"{total_additional:.2f}"])
    elif sorted_models:
        # Old format - just total (from metrics API, no included/additional breakdown)
        writer.writerow(["Model Name", "Total AI Credits"])
        for model_name, model_data, total in sorted_models:
            writer.writerow([model_name, f"{total:.2f}"])
        writer.writerow(["TOTAL", f"{total_model_credits:.2f}"])
    else:
        # No models available - write header only
        writer.writerow(["Model Name", "Included Credits", "Additional Credits"])

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

    # Determine whether the selected month is the current billing cycle.
    # The Copilot billing API (/enterprises/{e}/copilot/billing) carries no date
    # parameter and always reflects the CURRENT cycle.  Its ai_credits_used and
    # included_ai_credits fields must only be used when the user selected the
    # current month; for any other month those values belong to a different period.
    today = datetime.now()
    is_current_month = (start_date.year == today.year and start_date.month == today.month)

    print(f"Generating Copilot Usage Report for: {month_name}")
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    if not is_current_month:
        print("Note: Historical month selected — billing usage API (month-specific) "
              "will be used as primary source for consumed credits.")

    # AI credits billing model is only available from June 2026 onwards.
    if start_date < _AI_CREDITS_MIN_DATE:
        print(f"\nNote: AI credits data is only available from "
              f"{_AI_CREDITS_MIN_DATE.strftime('%B %Y')} onwards. "
              f"The selected month ({month_name}) predates the AI credits billing model. "
              f"AI credit metrics will not be available for this period.")
    print()

    # ── Step 1: Fetch all data sources ────────────────────────────────────────

    # 1a. Billing/seats data → allocated (included) AI credits per month
    print("Fetching billing information...")
    billing_data = fetch_copilot_billing(enterprise, token)
    if billing_data:
        print(f"  Billing API responded with {len(billing_data)} top-level fields.")

    # 1b. Copilot seat assignments → accurate licensed user count
    print("Fetching Copilot seat assignments...")
    seats_data = fetch_copilot_seats(enterprise, token)

    # 1c. Per-user metrics (new API, June 2026+) → ai_credits_used per user
    print("Fetching per-user Copilot metrics...")
    user_metrics_data = fetch_copilot_user_metrics(
        enterprise, token, start_date, end_date
    )

    # 1d. Enterprise-level metrics reports (new API) → model breakdown, totals
    print("Fetching enterprise Copilot metrics reports...")
    enterprise_metrics_data = fetch_copilot_metrics_report(
        enterprise, token, start_date, end_date
    )

    # 1e. Legacy enterprise metrics (fallback if new API unavailable)
    legacy_metrics_data = None
    if enterprise_metrics_data is None:
        print("Fetching enterprise Copilot metrics (legacy)...")
        legacy_metrics_data = fetch_copilot_org_report(
            enterprise, token, start_date, end_date
        )

    # 1f. Enterprise billing usage → actual credits consumed + cost-center labels
    print("Fetching enterprise billing usage (AI credits)...")
    billing_usage = fetch_enterprise_billing_usage(enterprise, token, year, month)

    # 1g. Cost centers list → maps cost-center IDs/names to org resources
    print("Fetching cost centers...")
    cost_centers = fetch_cost_centers(enterprise, token)

    print()

    # ── Step 2: Process all data sources ──────────────────────────────────────

    # Process per-user metrics (best source for per-user AI credits)
    per_user_processed = None
    if user_metrics_data:
        print("Processing per-user metrics data...")
        per_user_processed = process_per_user_metrics(user_metrics_data, seats_data)
        print(f"  Per-user metrics: {per_user_processed['total_credits']:,.2f} AI credits, "
              f"{per_user_processed['unique_users']} active users")

    # Process enterprise metrics (model breakdown, aggregate credits)
    metrics_processed = None
    metrics_source = enterprise_metrics_data or legacy_metrics_data
    if metrics_source:
        print("Processing enterprise-level metrics data...")
        metrics_processed = process_metrics_data(metrics_source)

    # Process billing usage (cost-center labels, included/additional breakdown)
    billing_usage_processed = process_billing_usage_data(billing_usage, cost_centers)

    # Get seat count — this is the authoritative count of Copilot licensed users.
    # Deduplicate by user login to avoid double-counting users granted access
    # through multiple organisations or enterprise teams.
    seat_user_count = 0
    if seats_data:
        _seen_logins = set()
        for _seat in seats_data:
            _assignee = _seat.get("assignee") or {}
            _login = (_assignee.get("login", "")
                      if isinstance(_assignee, dict) else "")
            if _login:
                _seen_logins.add(_login)
        seat_user_count = len(_seen_logins) if _seen_logins else len(seats_data)
        print(f"  Copilot licensed seats: {seat_user_count} unique users "
              f"({len(seats_data)} total seat entries)")

    # ── Step 3: Assemble final report_data ────────────────────────────────────
    # Consumed credits: enterprise billing usage API is the single source.
    # It accepts year + month parameters so it returns the correct data for
    # both the current month (month-to-date) and any historical month.

    total_credits = 0
    unique_users = 0
    model_breakdown = {}
    cost_center_breakdown = {}

    # Consumed credits — billing usage API only (month-specific).
    if billing_usage_processed and billing_usage_processed["total_credits"] > 0:
        total_credits = billing_usage_processed["total_credits"]
        print("  Consumed credits sourced from billing usage API.")
    else:
        print("  Warning: Billing usage API returned no AI credit data for this month.")
        print("  Ensure the token has 'manage_billing:copilot' and 'read:enterprise' "
              "scopes and that there is Copilot usage for the selected month.")

    # User count: licensed seats are the authoritative value.
    # Only fall back to per-user active users when the seats API is unavailable.
    if seat_user_count > 0:
        unique_users = seat_user_count
    elif per_user_processed and per_user_processed["unique_users"] > 0:
        unique_users = per_user_processed["unique_users"]
        print("  Note: Using active user count from per-user metrics (seats API unavailable).")

    # Model breakdown: prefer billing usage only when model names are present;
    # otherwise use metrics (which usually carries detailed model names).
    if billing_usage_processed and billing_usage_processed.get("model_breakdown"):
        billing_models = billing_usage_processed["model_breakdown"]
        metrics_models = (metrics_processed.get("model_breakdown")
                          if metrics_processed else None)
        if _has_named_models(billing_models) or not metrics_models:
            model_breakdown = billing_models
        else:
            model_breakdown = metrics_models
            print("  Billing usage model data lacked model names; using metrics model breakdown.")
    elif metrics_processed and metrics_processed.get("model_breakdown"):
        model_breakdown = metrics_processed["model_breakdown"]

    # Cost center breakdown: prefer billing usage > per-user org > org metrics
    if billing_usage_processed and billing_usage_processed.get("cost_center_breakdown"):
        cost_center_breakdown = billing_usage_processed["cost_center_breakdown"]
        if per_user_processed and per_user_processed.get("org_breakdown"):
            cost_center_breakdown = _enrich_cost_center_users(
                cost_center_breakdown,
                per_user_processed["org_breakdown"],
                per_user_processed.get("unique_users", 0)
            )
    elif per_user_processed and per_user_processed.get("org_breakdown"):
        # Map org breakdown to cost center breakdown
        cost_center_breakdown = per_user_processed["org_breakdown"]
    elif cost_centers:
        print("Building cost-center breakdown from org metrics...")
        cc_breakdown = build_cost_center_metrics(
            cost_centers, token, start_date, end_date
        )
        if cc_breakdown:
            cost_center_breakdown = cc_breakdown

    report_data = {
        "total_credits": total_credits,
        "unique_users": unique_users,
        "cost_center_breakdown": cost_center_breakdown,
        "model_breakdown": model_breakdown,
        "user_credits": per_user_processed.get("user_credits", {}) if per_user_processed else {}
    }

    # Generate the report
    report_text, csv_text = generate_report(
        report_data, billing_data, month_name, seats_data,
        billing_start_date=start_date,
        is_current_month=is_current_month
    )

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
