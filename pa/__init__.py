"""Experiment Autopilot for Adapty."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def project_path(value: str) -> Path:
    """Resolve a configured path against the project root, so the tools work from any working directory."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()
