<!-- Subject and body follow the commit rules: `Scope: descriptive summary`,
     then why, edge cases, and what the tests cover. -->

## What it does the instant it is live

<!-- "Nothing until an admin sets the channel on Config → …" is a good answer.
     Name any switch someone must flip after the deploy. -->

## Checklist — design_guide.md Part 4

- [ ] Tests at the logic layer, in the same commit, covering every guard
- [ ] Behaviour change ⇒ the matching spec in `docs/` (and its `INDEX.md` row)
- [ ] UI change ⇒ `src/web_server/static/manual.html`
- [ ] New per-user data ⇒ `docs/data_register.md` row
- [ ] Ships dark: a new dial defaults off, a new table starts empty
- [ ] `python scripts/gate.py --quick` green locally; eslint/stylelint if JS or CSS changed
- [ ] User-facing ⇒ the commit body ends with a `Testing:` section for a volunteer tester
