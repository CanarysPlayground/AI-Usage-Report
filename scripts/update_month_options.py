"""
Update the month dropdown options in the Copilot usage report workflow.

Run on the 1st of each month (via update-report-options.yml).
Adds the month that has just become inaccessible via 'last_month'
(i.e. two calendar months before today) as an explicit YYYY-MM entry
in the copilot-usage-report.yml options list.

This keeps the dropdown up-to-date automatically:
  - August  2026  →  adds '2026-06'
  - September 2026 →  adds '2026-07'
  … and so on.

The earliest month that carries AI-credits data is June 2026.
No entries are added for dates before that.
"""

import re
import sys
from datetime import datetime, timedelta

WORKFLOW_FILE = ".github/workflows/copilot-usage-report.yml"
# AI credits billing model started June 2026; don't add earlier months.
AI_CREDITS_MIN_YEAR = 2026
AI_CREDITS_MIN_MONTH = 6


def two_months_ago(today=None):
    """Return a (year, month) tuple for the month two calendar months before today."""
    if today is None:
        today = datetime.now()
    first_of_current = today.replace(day=1)
    first_of_last = (first_of_current - timedelta(days=1)).replace(day=1)
    two_ago = (first_of_last - timedelta(days=1)).replace(day=1)
    return two_ago.year, two_ago.month


def main():
    year, month = two_months_ago()
    month_str = f"{year}-{month:02d}"

    # Skip months that predate the AI-credits model.
    if (year, month) < (AI_CREDITS_MIN_YEAR, AI_CREDITS_MIN_MONTH):
        print(f"Month {month_str} predates AI credits billing; nothing to add.")
        return

    with open(WORKFLOW_FILE, "r") as fh:
        content = fh.read()

    # Idempotent: skip if already present (with or without quotes).
    if f"- '{month_str}'" in content or f'- "{month_str}"' in content:
        print(f"Month {month_str} is already listed in {WORKFLOW_FILE}. No change needed.")
        return

    # Insert the new month option directly after the 'current_month' entry.
    # The options block looks like:
    #   options:
    #     - last_month
    #     - current_month
    #     - '2026-07'   ← new entries appear here, newest first
    #     - '2026-06'
    new_entry = f"          - '{month_str}'\n"
    updated = re.sub(
        r"(          - current_month\n)",
        r"\1" + new_entry,
        content,
        count=1,
    )

    if updated == content:
        print(
            f"Warning: Could not find '- current_month' anchor in {WORKFLOW_FILE}. "
            f"Manual update required to add {month_str}.",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(WORKFLOW_FILE, "w") as fh:
        fh.write(updated)

    print(f"Added '{month_str}' to {WORKFLOW_FILE} options.")


if __name__ == "__main__":
    main()
