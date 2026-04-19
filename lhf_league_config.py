"""
LHF league configuration loaded from LHF_data.csv (roster slots + category scoring).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_CSV = Path(__file__).resolve().parent / "LHF_data.csv"

ROSTER_ROW_KEYS = frozenset(
    {"C", "1B", "2B", "3B", "SS", "OF", "Util", "SP", "RP", "P", "BN", "IL"}
)


@dataclass
class LHFLeagueConfig:
    """Structured league rules from CSV."""

    roster_slots: dict[str, int]
    pitching_categories: list[str]
    batting_categories: list[str]
    source_path: str

    @property
    def active_hitter_slots(self) -> int:
        return sum(self.roster_slots.get(k, 0) for k in ("C", "1B", "2B", "3B", "SS", "OF", "Util"))

    @property
    def active_pitcher_slots(self) -> int:
        return sum(self.roster_slots.get(k, 0) for k in ("SP", "RP", "P"))

    @property
    def total_roster_slots(self) -> int:
        return sum(self.roster_slots.values())


def load_lhf_config(csv_path: Path | None = None) -> LHFLeagueConfig:
    """
    Parse LHF_data.csv:
      column A: position, B: roster spots, D: pitching category (parallel list), E: batting category.
    """
    path = csv_path or _DEFAULT_CSV
    if not path.is_file():
        raise FileNotFoundError(f"LHF data file not found: {path}")

    roster_slots: dict[str, int] = {}
    pitching_cats: list[str] = []
    batting_cats: list[str] = []

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            raise ValueError("Empty CSV")

        for row in reader:
            if not row or not row[0].strip():
                continue
            pos = row[0].strip()
            if pos in ROSTER_ROW_KEYS and len(row) > 1 and row[1].strip().isdigit():
                roster_slots[pos] = int(row[1].strip())

            # Categories in columns 4 and 5 (0-based index 3 and 4)
            if len(row) > 3 and row[3].strip():
                pitching_cats.append(row[3].strip())
            if len(row) > 4 and row[4].strip():
                batting_cats.append(row[4].strip())

    if not roster_slots:
        raise ValueError("No roster rows parsed from LHF CSV")

    return LHFLeagueConfig(
        roster_slots=roster_slots,
        pitching_categories=pitching_cats,
        batting_categories=batting_cats,
        source_path=str(path.resolve()),
    )


# Singleton for import-time use (reload server to pick up CSV edits)
_config_cache: LHFLeagueConfig | None = None


def get_lhf_config() -> LHFLeagueConfig:
    global _config_cache
    if _config_cache is None:
        _config_cache = load_lhf_config()
    return _config_cache


def clear_lhf_config_cache() -> None:
    """Tests / reload."""
    global _config_cache
    _config_cache = None
