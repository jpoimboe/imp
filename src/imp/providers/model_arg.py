"""Parse a /model or --model argument into provider/model parts."""
from __future__ import annotations

from dataclasses import dataclass


class ModelArgError(ValueError):
    """A syntactically malformed /model or --model argument."""


@dataclass(frozen=True, slots=True)
class ParsedModelArg:
    provider: str | None
    model: str


def parse_model_arg(arg: str) -> ParsedModelArg:
    before, sep, after = arg.partition(":")
    if sep:
        provider, model = before.strip(), after.strip()
        if not provider or not model:
            raise ModelArgError(
                f"malformed model argument {arg!r} — expected 'provider:model'")
        return ParsedModelArg(provider=provider, model=model)
    model = before.strip()
    if not model:
        raise ModelArgError(f"empty model argument {arg!r}")
    return ParsedModelArg(provider=None, model=model)
