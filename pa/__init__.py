"""Experiment Autopilot for Adapty."""
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def project_path(value: str) -> Path:
    """Resolve a configured path against the project root, so the tools work from any working directory."""
    p = Path(value).expanduser()
    return p if p.is_absolute() else (ROOT / p).resolve()


def out_dir() -> Path:
    """Where reports, the audit log and the Slack preview go: <project>/out, or AUTOPILOT_OUT_DIR. Read at call time
    and independent of the working directory, so a cron job writes the same files the dashboard reads."""
    import os  # noqa: PLC0415
    return project_path(os.getenv("AUTOPILOT_OUT_DIR", "out"))
