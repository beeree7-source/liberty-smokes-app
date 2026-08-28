## Summary

Describe what changed and why.

## Risk Level

- [ ] Low (UI text/layout only)
- [ ] Medium (logic updates without schema changes)
- [ ] High (Supabase schema/data flow, billing, reminders, or auth-related settings)

## Pre-Deploy Checklist

- [ ] Local app tested with `streamlit run app.py`
- [ ] Pre-deploy smoke test passed with `python scripts/predeploy_smoke_test.py`
- [ ] Supabase reads/writes tested for affected tables
- [ ] `requirements.txt` updated if dependencies changed
- [ ] `.streamlit/secrets.toml` was not committed
- [ ] Any SQL changes are included under `supabase/` and reviewed

## Database Impact

- [ ] No database impact
- [ ] Database impact (describe table/query changes and rollback plan below)

Rollback plan (required for DB-impacting changes):

<!-- Example: revert commit abc123, restore prior SQL script behavior, validate Seats and Members pages -->

## Validation Notes

List what you tested and the result.

## Screenshots (if UI changes)

Add before/after screenshots for Streamlit UI updates.
