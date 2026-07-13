- **Critical** — makes the result fundamentally wrong, broken, unverifiable, or
  unsafe; must fix.
- **Major** — a real defect, or a non-trivial risk of one: wrong or missing
  behavior, poor error handling, a test gap (including plan-echo / non-falsifiable
  tests), or a significant correctness/maintainability problem; must fix.
- **Minor** — a real but non-blocking improvement; correctness is intact.
- **Nit** — cosmetic, stylistic, or wording only.

Critical and Major block; Minor and Nit are advisory. Severity tracks
behavioral impact, not file type: classify a defect in config, a template, or a
prompt by its effect. Never downgrade a real defect to Minor to dodge a rework.
