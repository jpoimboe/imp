"""A pool of in-character status phrases, drawn one at a time by the live status
footer while a step's `rotate_phrase` flag is set."""
from __future__ import annotations

import random

RANDOM_STATUS_PHRASES: tuple[str, ...] = (
    "scheming", "plotting", "brooding", "hatching plans", "laying groundwork",
    "setting traps", "laying snares", "baiting hooks", "fermenting plots",
    "eyeing angles", "biding time", "casting bones", "scrying", "reading omens",
    "charting constellations", "reading smoke", "weighing portents",
    "reading tea leaves", "consulting runes", "divining", "tracing sigils",
    "watching tides", "taking bearings", "following hunches", "chasing glimmers",
    "conjuring", "whispering incantations", "muttering spells", "humming spell",
    "whispering charms", "trusting instinct", "keeping counsel", "mulling",
    "musing", "weighing options", "weighing odds", "turning over", "chewing over",
    "puzzling out", "piecing together", "untangling threads", "connecting dots",
    "collecting thoughts", "shuffling ideas", "distilling essence", "recalibrating",
    "sharpening wits", "honing edge", "whetting blade", "scrutinizing", "peering",
    "leaning in", "surveying", "sizing up", "taking measure", "gauging depth",
    "sounding depths", "testing waters", "narrowing eyes", "studying patterns",
    "probing corners", "testing wind", "mapping terrain", "charting course",
    "drawing maps", "sketching diagrams", "measuring twice", "tracing edges",
    "lurking", "prowling", "skulking", "pacing", "stalking quietly",
    "creeping closer", "haunting corners", "keeping watch", "sniffing around",
    "catching scent", "following trail", "nosing about", "poking around",
    "rooting through drawers", "rifling shelves", "thumbing ledgers", "dusting tomes",
    "picking lock", "foraging", "pricking ears", "twitching whiskers", "flicking tail",
    "sharpening claws", "flexing claws", "drumming claws", "steepling fingers",
    "stroking chin", "cracking knuckles", "tilting head", "prodding gently",
    "turning gears", "whittling away", "brewing ideas", "stirring cauldron",
    "tending embers", "stoking fire", "kindling sparks", "rummaging",
    "pacing threshold", "circling problem",
)


def random_status_phrase() -> str:
    """A uniformly-random phrase from the pool, chosen once per call."""
    return random.choice(RANDOM_STATUS_PHRASES)
