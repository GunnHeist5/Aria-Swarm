"""CSV output for the dry run — the artifact Phase 1 is validated against."""

from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .plan import FIELD_NAMES, DryRunRow


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_csv(rows: list[DryRunRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FIELD_NAMES)
        for row in rows:
            data = asdict(row)
            writer.writerow([_cell(data[name]) for name in FIELD_NAMES])
