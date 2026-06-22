- **Critical** — would make the result fundamentally wrong, broken, unverifiable,
  or unsafe; must be fixed.
- **Major** — a real defect or a non-trivial risk of one (wrong or missing
  behavior, poor error handling, a test gap including plan-echo / non-falsifiable
  tests, or a significant correctness/maintainability problem); must be fixed.
- **Minor** — a real but non-blocking improvement; correctness is intact.
- **Nit** — cosmetic, stylistic, or wording only.

Critical and Major are blocking; Minor and Nit are advisory. Severity tracks
behavioral impact, not file type — a defect in configuration, a template, or a
prompt is classified by its effect, and a real defect is never filed as Minor to
avoid a rework.
