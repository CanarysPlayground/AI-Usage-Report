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


def fetch_copilot_usage(org, token, start_date, end_date):
    """
    Fetch Copilot usage data from GitHub API.
    Uses the /orgs/{org}/copilot/usage endpoint.
    """
    headers = get_auth_headers(token)

    # Fetch usage data
    usage_url = f"https://api.github.com/orgs/{org}/copilot/usage"
    params = {
        "since": start_date.strftime("%Y-%m-%d"),
        "until": end_date.strftime("%Y-%m-%d")
    }

    all_usage_data = []
    page = 1

    while True:
        params["page"] = page
        params["per_page"] = 100
        response = requests.get(usage_url, headers=headers, params=params)

        if response.status_code == 200:
            data = response.json()
            if not data:
                break
            all_usage_data.extend(data)
            if len(data) < 100:
                break
            page += 1
        elif response.status_code == 404:
            print("Error: Organization not found or Copilot usage API not available.")
            print("Ensure the organization has Copilot enabled and the token has appropriate permissions.")
            sys.exit(1)
        elif response.status_code == 403:
            print("Error: Access forbidden. Ensure the token has 'manage_billing:copilot' or 'org:read' scope.")
            sys.exit(1)
        else:
            print(f"Error fetching usage data: {response.status_code} - {response.text}")
            sys.exit(1)

    return all_usage_data


def fetch_copilot_billing(org, token):
    """
    Fetch Copilot billing/seats information to get license and pooled credits data.
    """
    headers = get_auth_headers(token)

    billing_url = f"https://api.github.com/orgs/{org}/copilot/billing"
    response = requests.get(billing_url, headers=headers)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Warning: Could not fetch billing data: {response.status_code}")
        return None


def fetch_copilot_metrics(org, token, start_date, end_date):
    """
    Fetch Copilot metrics from the newer metrics API endpoint.
    """
    headers = get_auth_headers(token)

    metrics_url = f"https://api.github.com/orgs/{org}/copilot/metrics"
    params = {
        "since": start_date.strftime("%Y-%m-%d"),
        "until": end_date.strftime("%Y-%m-%d")
    }

    response = requests.get(metrics_url, headers=headers, params=params)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Note: Metrics API returned {response.status_code}. Using usage API data instead.")
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

            # Use credits if available, otherwise estimate from activity
            day_credits = credits if credits else suggestions_count * 0.01

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


def process_metrics_data(metrics_data):
    """
    Process metrics API data for detailed breakdowns.
    """
    total_credits = 0
    unique_users = set()
    cost_center_credits = defaultdict(lambda: {"credits": 0, "users": set()})
    model_credits = defaultdict(float)

    for day_data in metrics_data:
        copilot_ide_code_completions = day_data.get("copilot_ide_code_completions", {})
        copilot_ide_chat = day_data.get("copilot_ide_chat", {})
        copilot_dotcom_chat = day_data.get("copilot_dotcom_chat", {})
        copilot_dotcom_pull_requests = day_data.get("copilot_dotcom_pull_requests", {})

        total_engaged = day_data.get("total_active_users", 0)

        # Process code completions
        if copilot_ide_code_completions:
            for editor_data in copilot_ide_code_completions.get("editors", []):
                for model_data in editor_data.get("models", []):
                    model_name = model_data.get("name", "Unknown")
                    languages = model_data.get("languages", [])
                    for lang in languages:
                        credits = lang.get("total_credits_consumed", 0) or 0
                        total_credits += credits
                        model_credits[model_name] += credits

        # Process IDE chat
        if copilot_ide_chat:
            for editor_data in copilot_ide_chat.get("editors", []):
                for model_data in editor_data.get("models", []):
                    model_name = model_data.get("name", "Unknown")
                    credits = model_data.get("total_credits_consumed", 0) or 0
                    total_credits += credits
                    model_credits[model_name] += credits

        # Process dotcom chat
        if copilot_dotcom_chat:
            for model_data in copilot_dotcom_chat.get("models", []):
                model_name = model_data.get("name", "Unknown")
                credits = model_data.get("total_credits_consumed", 0) or 0
                total_credits += credits
                model_credits[model_name] += credits

        # Process pull requests
        if copilot_dotcom_pull_requests:
            for model_data in copilot_dotcom_pull_requests.get("models", []):
                model_name = model_data.get("name", "Unknown")
                credits = model_data.get("total_credits_consumed", 0) or 0
                total_credits += credits
                model_credits[model_name] += credits

    return {
        "total_credits": total_credits,
        "model_breakdown": model_credits
    }


def generate_report(report_data, billing_data, month_name, org):
    """
    Generate the usage report as a formatted string and CSV.
    """
    total_credits = report_data["total_credits"]
    unique_users = report_data["unique_users"]
    cost_center_breakdown = report_data["cost_center_breakdown"]
    model_breakdown = report_data["model_breakdown"]

    # Get pooled credits from billing data
    pooled_credits = "N/A"
    if billing_data:
        seat_count = billing_data.get("total_seats", 0)
        # Premium requests per seat per month (as per GitHub's allocation)
        premium_per_seat = billing_data.get("premium_requests_per_seat", 0)
        pooled_credits = seat_count * premium_per_seat if premium_per_seat else "N/A"

    report_lines = []
    report_lines.append(f"GitHub Copilot USAGE SUMMARY REPORT - {month_name.upper()}")
    report_lines.append("=" * 60)
    report_lines.append("")
    report_lines.append("OVERALL METRICS")
    report_lines.append("-" * 40)
    report_lines.append(f"  Organization:            {org}")
    report_lines.append(f"  Total AI Credits Used:   {total_credits:,.2f}")
    report_lines.append(f"  Total Unique Users:      {unique_users:,}")
    if pooled_credits != "N/A":
        report_lines.append(f"  Pooled Credits (Allocated): {pooled_credits:,}")
    else:
        report_lines.append(f"  Pooled Credits (Allocated): {pooled_credits}")
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
    all_users = set()
    for center, data in sorted_centers:
        credits = data["credits"]
        users = len(data["users"])
        total_center_credits += credits
        all_users.update(data["users"])
        report_lines.append(f"  {center:<30} {credits:>18,.2f} {users:>8}")

    report_lines.append(f"  {'-'*30} {'-'*18} {'-'*8}")
    report_lines.append(f"  {'TOTAL':<30} {total_center_credits:>18,.2f} {len(all_users):>8}")
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
    writer.writerow(["Total AI Credits Used", f"{total_credits:.2f}"])
    writer.writerow(["Total Unique Users", unique_users])
    writer.writerow(["Pooled Credits (Allocated)", pooled_credits])
    writer.writerow([])

    # Cost Center breakdown
    writer.writerow(["COST CENTER WISE AI CREDIT USAGE"])
    writer.writerow(["Cost Center", "Total AI Credits", "Users"])
    for center, data in sorted_centers:
        writer.writerow([center, f"{data['credits']:.2f}", len(data["users"])])
    writer.writerow(["TOTAL", f"{total_center_credits:.2f}", len(all_users)])
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
    org = os.environ.get("GITHUB_ORG")
    token = os.environ.get("GH_TOKEN")
    month_selection = os.environ.get("MONTH_SELECTION", "last_month")

    if not org:
        print("Error: GITHUB_ORG environment variable is required.")
        sys.exit(1)

    if not token:
        print("Error: GH_TOKEN environment variable is required.")
        sys.exit(1)

    # Calculate date range
    start_date, end_date = get_billing_month_dates(month_selection)
    month_name = start_date.strftime("%B %Y")

    print(f"Generating Copilot Usage Report for: {month_name}")
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print()

    # Fetch data from GitHub API
    print("Fetching Copilot usage data...")
    usage_data = fetch_copilot_usage(org, token, start_date, end_date)

    print("Fetching billing information...")
    billing_data = fetch_copilot_billing(org, token)

    print("Fetching metrics data...")
    metrics_data = fetch_copilot_metrics(org, token, start_date, end_date)

    # Process the data
    if usage_data:
        report_data = process_usage_data(usage_data)
    else:
        report_data = {
            "total_credits": 0,
            "unique_users": 0,
            "cost_center_breakdown": {},
            "model_breakdown": {}
        }

    # If metrics data is available, use it for model breakdown (more accurate)
    if metrics_data:
        metrics_processed = process_metrics_data(metrics_data)
        if metrics_processed["model_breakdown"]:
            report_data["model_breakdown"] = metrics_processed["model_breakdown"]
        if metrics_processed["total_credits"] > 0:
            report_data["total_credits"] = metrics_processed["total_credits"]

    # Generate the report
    report_text, csv_text = generate_report(report_data, billing_data, month_name, org)

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
