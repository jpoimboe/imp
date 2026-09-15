# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""A pool of in-character status phrases, drawn one at a time by the live status
footer while a step's `rotate_phrase` flag is set."""
from __future__ import annotations

import random

RANDOM_STATUS_PHRASES: tuple[str, ...] = (
    "scheming", "plotting", "brooding", "hatching plans", "laying groundwork",
    "setting traps", "laying snares", "baiting hooks", "fermenting plots",
    "eyeing angles", "casting bones", "scrying", "reading omens", "reading smoke",
    "weighing portents", "reading tea leaves", "consulting runes", "divining",
    "tracing sigils", "watching tides", "following hunches", "chasing glimmers",
    "conjuring", "whispering incantations", "muttering spells", "humming spell",
    "keeping counsel", "mulling", "weighing odds", "chewing over", "puzzling out",
    "piecing together", "untangling threads", "unpicking seams",
    "tugging loose thread", "sharpening wits", "whetting blade", "peering",
    "leaning in", "sizing up", "taking measure", "sounding depths",
    "narrowing eyes", "probing corners", "testing wards", "testing wind",
    "mapping terrain", "charting course", "measuring twice", "tracing edges",
    "lurking", "prowling", "skulking", "stalking quietly", "creeping closer",
    "haunting corners", "sniffing around", "catching scent", "following trail",
    "nosing about", "poking around", "rooting through drawers", "rifling shelves",
    "thumbing ledgers", "dusting tomes", "mixing inks", "sharpening quill",
    "picking lock", "prying boards", "jimmying window", "greasing hinges",
    "foraging", "pricking ears", "twitching whiskers", "flicking tail",
    "sharpening claws", "drumming claws", "steepling fingers", "stroking chin",
    "cracking knuckles", "tilting head", "prodding gently", "turning gears",
    "whittling away", "stirring cauldron", "tending embers", "trimming wick",
    "stoking fire", "kindling sparks", "rummaging", "pacing threshold",
    "circling problem",
)


def random_status_phrase() -> str:
    """A uniformly-random phrase from the pool, chosen once per call."""
    return random.choice(RANDOM_STATUS_PHRASES)
