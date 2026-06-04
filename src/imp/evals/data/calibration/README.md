# Calibration label sets

Two human-labeled JSON files drive the oracle calibration (spec Grader §). Both
must reach **≥20–30 items**, and the judge set must **span the providers and
structures scored** (include non-Anthropic and panel output) so judge agreement
can be reported per output-style.

- `judge_items.json` — list of CalibrationItem dicts:
  `{output_style, expected_symbol, expected_locus, expected_description,
    expected_severity, review_prose, human_caught}`
- `normalization_items.json` — list of NormalizationItem dicts:
  `{output_style, review_prose, human_findings: [ExpectedFinding...]}`

**Oracle independence:** the judge and normalizer models must NOT be the same
family being scored (don't judge gpt-5.5 reviews with a gpt-5.5 judge).
