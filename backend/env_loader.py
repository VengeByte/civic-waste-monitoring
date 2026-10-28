"""
Small, dependency-free .env loader shared by the VLM router and SMTP delivery.

Values already present in the process environment always win, so production
deployments can keep using real environment variables while local development can
rely on a gitignored ``.env`` file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


def _default_paths() -> list[Path]:
    project_root = Path(__file__).resolve().parent.parent
    return [Path.cwd() / ".env", project_root / ".env"]


def load_env_file(paths: Optional[Iterable[Path | str]] = None) -> int:
    """
    Load ``KEY=VALUE`` pairs from one or more .env files into ``os.environ``.

    Existing environment variables are never overwritten. Blank lines, comments,
    a leading ``export``, and single/double quoted values are supported.

    Args:
        paths: Optional explicit file paths. Defaults to ``./.env`` and the
            project-root ``.env``.

    Returns:
        Number of variables newly applied.
    """
    applied = 0
    seen: set[Path] = set()

    for raw_path in paths if paths is not None else _default_paths():
        path = Path(raw_path)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)

        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]

            if key not in os.environ:
                os.environ[key] = value
                applied += 1

    return applied


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Parse a float environment variable, clamped to a minimum."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default
