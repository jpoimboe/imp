"""V1 provider registry. Importing this package eagerly registers builtin
providers in providers.resolve._PROVIDERS so resolve_role() can find them.
"""

from imp.providers import anthropic as _anthropic
from imp.providers.resolve import register_provider

register_provider("anthropic", env_check=_anthropic._env_check, factory=_anthropic._factory)
