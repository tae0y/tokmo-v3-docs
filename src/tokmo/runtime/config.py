"""Private env-file loader; never reads process environment.

Reason semantics:
- ``config_missing``    — file absent, or a required key absent
- ``config_invalid``    — file loaded but content is wrong (syntax, duplicate
                          key, placeholder value, unsupported mode)
- ``config_load_error`` — the file could not be read at the OS level
"""

from dataclasses import dataclass, field
from pathlib import Path

REQUIRED_KEYS = ("MODE", "EIA_API_KEY")
# SEC Tracer 1a: the declared User-Agent must identify the product/operator
# and carry a real contact address, so a value without "@" is invalid.
SEC_USER_AGENT_KEY = "SEC_USER_AGENT"
# OpenDART Tracer 1a: the API credential env key.
OPENDART_API_KEY_KEY = "OPENDART_API_KEY"
KRX_API_KEY_KEY = "KRX_API_KEY"
# Toss holdings tracer: the client-credentials pair. The display account
# number is deliberately not a key here; this tracer has no use for it.
TOSS_CLIENT_ID_KEY = "TOSS_CLIENT_ID"
TOSS_CLIENT_SECRET_KEY = "TOSS_CLIENT_SECRET"
PLACEHOLDER_VALUES = frozenset({"replace-me", "placeholder", "changeme"})
TRACER_MODE = "tracer"
# Live contract §4: the live runner accepts only MODE=tracer-live via
# load_live_config, and the fixture loader keeps rejecting it.
LIVE_MODE = "tracer-live"


@dataclass(frozen=True)
class TracerConfig:
    mode: str
    eia_api_key: str


@dataclass(frozen=True)
class SecConfig:
    user_agent: str = field(repr=False)


@dataclass(frozen=True)
class OpenDartConfig:
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class KrxConfig:
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class TossConfig:
    """Both halves stay out of ``repr``; only the auth adapter ever sees them."""

    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


class ConfigError(Exception):
    """Typed config failure. Carries a contract reason and never a value."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def load_config(path: Path) -> TracerConfig:
    return _config_from_lines(_read_env_lines(path), required_mode=TRACER_MODE)


def load_live_config(path: Path) -> TracerConfig:
    """Live loader (§4): identical grammar and secrecy, MODE=tracer-live only."""
    return _config_from_lines(_read_env_lines(path), required_mode=LIVE_MODE)


def load_sec_config(path: Path) -> SecConfig:
    """SEC loader: same env grammar; requires only SEC_USER_AGENT."""
    values = _parse_env_lines(_read_env_lines(path))
    if SEC_USER_AGENT_KEY not in values:
        raise ConfigError("config_missing")
    user_agent = values[SEC_USER_AGENT_KEY]
    if (
        not user_agent
        or user_agent.lower() in PLACEHOLDER_VALUES
        or "@" not in user_agent
    ):
        raise ConfigError("config_invalid")
    return SecConfig(user_agent=user_agent)


def load_opendart_config(path: Path) -> OpenDartConfig:
    """OpenDART loader: same env grammar; requires only OPENDART_API_KEY."""
    values = _parse_env_lines(_read_env_lines(path))
    if OPENDART_API_KEY_KEY not in values:
        raise ConfigError("config_missing")
    api_key = values[OPENDART_API_KEY_KEY]
    if not api_key or api_key.lower() in PLACEHOLDER_VALUES:
        raise ConfigError("config_invalid")
    return OpenDartConfig(api_key=api_key)


def load_krx_config(path: Path) -> KrxConfig:
    values = _parse_env_lines(_read_env_lines(path))
    if KRX_API_KEY_KEY not in values:
        raise ConfigError("config_missing")
    api_key = values[KRX_API_KEY_KEY]
    if not api_key or api_key.lower() in PLACEHOLDER_VALUES:
        raise ConfigError("config_invalid")
    return KrxConfig(api_key=api_key)


def load_toss_config(path: Path) -> TossConfig:
    """Toss loader: same env grammar; requires both credential halves.

    The empty template written by setup therefore fails as ``config_invalid``
    until the operator fills it in by editing the file directly.
    """
    values = _parse_env_lines(_read_env_lines(path))
    for key in (TOSS_CLIENT_ID_KEY, TOSS_CLIENT_SECRET_KEY):
        if key not in values:
            raise ConfigError("config_missing")
    client_id = values[TOSS_CLIENT_ID_KEY]
    client_secret = values[TOSS_CLIENT_SECRET_KEY]
    for value in (client_id, client_secret):
        if not value or value.lower() in PLACEHOLDER_VALUES:
            raise ConfigError("config_invalid")
    return TossConfig(client_id=client_id, client_secret=client_secret)


def live_config_from_text(text: str) -> TracerConfig:
    """Live loader over already-read text, for fd-anchored bounded reads."""
    return _config_from_lines(text.splitlines(), required_mode=LIVE_MODE)


def _read_env_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except FileNotFoundError:
        raise ConfigError("config_missing") from None
    except OSError:
        raise ConfigError("config_load_error") from None


def _config_from_lines(lines: list[str], *, required_mode: str) -> TracerConfig:
    values = _parse_env_lines(lines)
    for key in REQUIRED_KEYS:
        if key not in values:
            raise ConfigError("config_missing")
    if values["MODE"] != required_mode:
        raise ConfigError("config_invalid")

    api_key = values["EIA_API_KEY"]
    if not api_key or api_key.lower() in PLACEHOLDER_VALUES:
        raise ConfigError("config_invalid")
    return TracerConfig(mode=values["MODE"], eia_api_key=api_key)


def _parse_env_lines(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            raise ConfigError("config_invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in values:
            raise ConfigError("config_invalid")
        values[key] = value.strip()
    return values
