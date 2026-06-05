from imp.evals.pricing import price_for


def test_codex_gpt_5_5_aliases_openai_gpt_5_5() -> None:
    codex = price_for("codex", "gpt-5.5")
    openai = price_for("openai", "gpt-5.5")
    # Notional alias of the openai row (per spec "task-0 pricing entry").
    assert codex == openai
