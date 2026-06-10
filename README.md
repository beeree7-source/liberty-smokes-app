# Streamlit Supabase Seat Manager

A Streamlit app that manages seat check-ins from a Supabase table named `seats`.

## Table schema

Use this table in Supabase:

- `seat_number` (int, primary key)
- `customer_name` (text)
- `drinks_consumed` (int)
- `alcoholic_drinks` (int)
- `non_alcoholic_drinks` (int)
- `drink_breakdown` (jsonb)
- `is_occupied` (bool)

You can run the SQL in `supabase/create_seats_table.sql` to create and seed rows.
For member tracking, also run `supabase/create_members_table.sql`.
For monthly gift card refill tracking, run `supabase/create_member_monthly_refills_table.sql`.
For the daily cash and credit ledger, run `supabase/create_daily_sales_ledger_table.sql`.

### Monthly member drink tracking

The app tracks member drink totals by month in `member_monthly_drinks`:

- Counts are updated from the Seats page whenever a seated member gets a drink.
- Totals reset automatically each month because data is stored per `month_start`.
- Prior months remain available so you can compare trends and pricing impact.

Drink catalog and chair-level tracking:

- Manage drinks in Settings with `name`, `cost`, and category (`alcoholic` or `non_alcoholic`).
- On each occupied seat, select a configured drink and add it to that chair.
- Seat cards show a per-drink quantity breakdown and estimated drink cost for that visit.

Member purchase margins and discounts:

- CigarPOS integration settings include a direct portal link once a URL is configured.
- Member discount and margin analysis is based on POS purchase history and grouped by month.
- Monthly reports include regular-price sales, member-discount sales, cost total, and margin.
- History is preserved across months and can be exported for pricing reviews.

### Monthly gift card refill tracking

The app tracks gift card refills by month in `member_monthly_refills`:

- Each member can only have one refill record per month.
- Use the Members page to mark members as refilled, view pending members, and undo mistakes.
- Save and update each member's gift card number so lost cards can be replaced and re-entered.
- The gift card tracker is organized by locker number instead of member ID.
- This helps prevent double refills in the same month.

### Daily sales ledger

The app includes a Sales Ledger page backed by `daily_sales_ledger`:

- Record cash sales and credit sales for each day.
- Log multiple cash deductions from the drawer during the day.
- Choose which register day to deduct from, including prior closed days.
- Track the final cash deposit for the day and compare it to expected cash.
- Review an audit trail of ledger saves, deduction adds, deduction deletes, and day deletes.
- Export each month's ledger rows to CSV.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
  This project uses the lightweight `postgrest` client (not the full `supabase` meta-client),
  which avoids pulling optional storage dependencies such as `pyiceberg`.
3. Create Streamlit secrets file:
   - Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`
   - Add values for:
     - `SUPABASE_URL`
     - `SUPABASE_KEY`

## Run

```bash
streamlit run app.py
```

## Build Windows EXE

This project includes a repeatable PyInstaller build script:

```powershell
pwsh -File .\build_exe.ps1
```

This creates a versioned folder and EXE name using a timestamp, for example:

```text
.\dist\LibertySmokes-20260610-1315\LibertySmokes-20260610-1315.exe
```

You can provide your own version tag:

```powershell
pwsh -File .\build_exe.ps1 -VersionTag v1.0.0
```

Build a no-console (windowed) EXE:

```powershell
pwsh -File .\build_exe_windowed.ps1
```

Or with a custom tag:

```powershell
pwsh -File .\build_exe_windowed.ps1 -VersionTag v1.0.0
```

Create a Desktop shortcut to the latest built EXE:

```powershell
pwsh -File .\create_desktop_shortcut.ps1 -Force
```

The EXE is created at:

```text
.\dist\LibertySmokes-<version>\LibertySmokes-<version>.exe
```

Notes:

- Keep editing `app.py` and other source files as usual.
- Re-run `build_exe.ps1` whenever you want a fresh EXE with new code changes.
- Each build removes previous `dist` and `build` folders, then creates a fresh output.

## External Scheduler For Reminder Emails

The app now includes a standalone scheduler runner so reminder emails can be sent even when Streamlit is not open.

Files:

- `scheduler/run_email_reminders.py` - Runs one reminder cycle.
- `scheduler/register_reminder_task.ps1` - Registers a Windows Scheduled Task.

### One-time setup

1. Configure SMTP and reminder templates in the app Settings page.
2. In the app Settings, enable `Automated Reminder Emails`.
3. Register the Windows task (PowerShell):

```powershell
pwsh -File .\scheduler\register_reminder_task.ps1 -EveryMinutes 15 -Force
```

4. Optionally run once immediately:

```powershell
schtasks /Run /TN "LibertySmokes-ReminderEmails"
```

### Manual runner (without task)

```powershell
.\.venv\Scripts\python.exe .\scheduler\run_email_reminders.py
```

Useful flags:

- `--force` runs even if automation is disabled in settings.
- `--ignore-interval` runs even if the configured interval has not elapsed.
- `--dry-run` evaluates reminders without sending emails.

## Behavior

- The UI renders seats in a 4-column card grid.
- Empty seat card:
  - Name input
  - `Check In` button
- Occupied seat card:
  - Name display
  - Drink counter
  - `+` button (disabled when `drinks_consumed >= 3`)
  - `Clear` button to reset the seat
