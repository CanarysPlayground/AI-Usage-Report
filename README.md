# GitHub Copilot Detailed Usage Report

Automated workflow to generate and email a detailed GitHub Copilot usage report on the 1st of every month at 8:30 AM IST.

## Report Contents

The report includes:
- **Overall Metrics**: Total AI Credits Used, Total Unique Users (licensed users), Pooled Credits allocated
- **Cost Center Wise AI Credit Usage**: Breakdown by team/cost center with credits and user count
- **Model Wise AI Credit Usage**: Breakdown by AI model with total credits consumed

## Schedule

- **Automatic**: Runs on the 1st of every month at 8:30 AM IST (3:00 AM UTC)
- **Manual**: Can be triggered manually via GitHub Actions with a month selection dropdown

## Manual Trigger Options

When running manually, you can select:
- `last_month` - Previous month's report
- `current_month` - Current month's report (partial)
- Specific months like `2026-06`, `2026-05`, `2026-04`, etc.

## Required Secrets

Configure these secrets in your repository settings (`Settings > Secrets and variables > Actions`):

| Secret | Description |
|--------|-------------|
| `COPILOT_ADMIN_TOKEN` | GitHub Personal Access Token with `manage_billing:copilot`, `read:org`, and `copilot` scopes |
| `SMTP_SERVER` | SMTP server address (e.g., `smtp.gmail.com`, `smtp.office365.com`) |
| `SMTP_PORT` | SMTP port (e.g., `587` for TLS) |
| `SMTP_USERNAME` | SMTP authentication username |
| `SMTP_PASSWORD` | SMTP authentication password or app password |
| `SMTP_FROM_EMAIL` | Sender email address |
| `REPORT_RECIPIENTS` | Comma-separated list of email recipients (you and your team leader) |

## Setup Instructions

1. **Create a GitHub Personal Access Token (PAT)**:
   - Go to GitHub Settings > Developer settings > Personal access tokens > Fine-grained tokens
   - Create a token with the following permissions for your organization:
     - `Organization permissions`: Copilot (Read), Members (Read), Administration (Read)
   - Or use a classic token with scopes: `manage_billing:copilot`, `read:org`

2. **Configure SMTP Settings**:
   - For Gmail: Use `smtp.gmail.com` port `587` with an [App Password](https://support.google.com/accounts/answer/185833)
   - For Outlook/Office 365: Use `smtp.office365.com` port `587`

3. **Add Repository Secrets**:
   - Navigate to your repository > Settings > Secrets and variables > Actions
   - Add all required secrets listed above

4. **Set Recipients**:
   - In the `REPORT_RECIPIENTS` secret, add comma-separated emails:
     ```
     your.email@company.com,teamleader.email@company.com
     ```

5. **Test the Workflow**:
   - Go to Actions tab > "GitHub Copilot Usage Report" > "Run workflow"
   - Select the desired month and click "Run workflow"

## Output

- Report is displayed in the GitHub Actions job summary
- Text and CSV files are uploaded as workflow artifacts (retained for 90 days)
- Email is sent to configured recipients with report attached

## File Structure

```
├── .github/workflows/
│   └── copilot-usage-report.yml    # GitHub Actions workflow
├── scripts/
│   └── generate_copilot_report.py  # Report generation script
└── README.md                        # This file
```
