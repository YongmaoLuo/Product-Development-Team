import os
from pathlib import Path
from typing import Dict

try:
    from dotenv import load_dotenv

    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

REQUIRED_VARS = [
    "ANTHROPIC_API_KEY",
    "NOTION_TOKEN",
    "NOTION_PARENT_PAGE_ID",
]

DEFAULTS = {
    "TIMEZONE": "Asia/Shanghai",
}


def load_env_config(env_path: str = None) -> Dict[str, str]:
    if env_path is None:
        env_path = str(Path(__file__).parent / ".env")

    if Path(env_path).exists() and DOTENV_AVAILABLE:
        load_dotenv(env_path)

    config = {}
    for key in REQUIRED_VARS:
        value = os.environ.get(key)
        if value is None:
            raise KeyError(key)
        config[key] = value

    for key, default in DEFAULTS.items():
        config[key] = os.environ.get(key, default)

    return config


def load_env(env_path: str = None) -> Dict[str, str]:
    """Entry-point helper that fails loudly on missing required env vars.

    Wraps :func:`load_env_config` so that the surface every CLI
    ``main()`` calls surfaces a ``RuntimeError`` (with the missing
    key name in the message) instead of a bare ``KeyError``. The
    KeyError form is too quiet — operators see only the key string
    with no context, while the RuntimeError form makes it obvious
    that startup-time configuration is the problem.

    The return value is the same populated config dict, so callers
    can still use the result if they want.
    """
    try:
        return load_env_config(env_path)
    except KeyError as exc:
        missing_key = exc.args[0] if exc.args else "UNKNOWN"
        raise RuntimeError(
            f"Missing required environment variable: {missing_key}. "
            f"Populate backend/.env (see backend/.env.example) or export "
            f"the variable in your shell before starting this module."
        ) from exc


def validate_config(config: Dict[str, str]) -> None:
    api_key = config.get("ANTHROPIC_API_KEY", "")
    if not api_key.startswith("sk-ant-"):
        raise ValueError(
            f"ANTHROPIC_API_KEY must start with 'sk-ant-', got '{api_key[:10]}...'"
        )
