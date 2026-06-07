import re

from imp.engine.naming import random_title


def test_random_title_three_lowercase_words() -> None:
    title = random_title()
    # Shape only — never assert literal RNG output (spec §11 "shape-valid").
    assert re.fullmatch(r"[a-z]+ [a-z]+ [a-z]+", title), title


def test_random_title_varies() -> None:
    # Over many draws we should see more than one distinct phrase.
    seen = {random_title() for _ in range(50)}
    assert len(seen) > 1


def test_random_title_seeded_is_deterministic() -> None:
    import random
    assert random_title(rng=random.Random(0)) == random_title(rng=random.Random(0))
