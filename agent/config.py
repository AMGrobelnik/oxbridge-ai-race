"""Settings shared by the whole agent.

Secrets come from .env (git-ignored) or the real environment. Never hard-code them.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env() -> None:
    """Loads .env into os.environ. Variables that are already set in the environment win."""
    load_dotenv(ENV_FILE, override=False)


def get_port() -> int:
    """The port the HTTP service listens on (PORT, default 3000)."""
    try:
        return int(os.environ.get("PORT") or 3000)
    except ValueError:
        return 3000
