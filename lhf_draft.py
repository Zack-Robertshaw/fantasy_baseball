"""
LHF redraft: assign drafted players to lineup slots, compute remaining needs,
and score Yahoo OR-ranked players with a positional-need boost.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lhf_league_config import LHFLeagueConfig, get_lhf_config


# Positions that count as outfield eligibility in Yahoo
_OF_ALIASES = frozenset({"OF", "LF", "CF", "RF"})
_PITCH = frozenset({"SP", "RP", "P"})


def normalize_yahoo_positions(positions: list[str]) -> set[str]:
    """Normalize Yahoo position strings to LHF slot keys."""
    out: set[str] = set()
    expanded: list[str] = []
    for raw in positions:
        if not raw or raw == "?":
            continue
        if isinstance(raw, str) and "," in raw:
            expanded.extend([x.strip() for x in raw.split(",") if x.strip()])
        else:
            expanded.append(raw)

    for raw in expanded:
        p = raw.strip().upper()
        if p in _OF_ALIASES:
            out.add("OF")
        elif p == "DH":
            out.add("Util")
        elif p in _PITCH:
            out.add(p)
            if p in ("SP", "RP"):
                out.add("P")
        else:
            out.add(p)
    return out


def is_pitcher_only(pos: set[str]) -> bool:
    if not pos:
        return False
    return pos.issubset(_PITCH) or (pos & _PITCH and not (pos - _PITCH))


def is_hitter(pos: set[str]) -> bool:
    if not pos:
        return False
    hitter_marks = pos & {"C", "1B", "2B", "3B", "SS", "OF", "Util", "DH"}
    return bool(hitter_marks)


def can_fill_util(pos: set[str]) -> bool:
    """Util accepts any batter; not pure pitchers."""
    if pos.issubset(_PITCH):
        return False
    return is_hitter(pos) or bool(pos & {"Util", "DH"})


@dataclass
class SlotState:
    """Remaining lineup slot counts (mutable during assignment)."""

    c: int = 1
    b1: int = 1
    b2: int = 1
    b3: int = 1
    ss: int = 1
    of: int = 3
    util: int = 2
    sp: int = 3
    rp: int = 3
    p: int = 4
    bn: int = 6
    il: int = 2

    @classmethod
    def from_config(cls, cfg: LHFLeagueConfig) -> SlotState:
        r = cfg.roster_slots
        return cls(
            c=r.get("C", 1),
            b1=r.get("1B", 1),
            b2=r.get("2B", 1),
            b3=r.get("3B", 1),
            ss=r.get("SS", 1),
            of=r.get("OF", 3),
            util=r.get("Util", 2),
            sp=r.get("SP", 3),
            rp=r.get("RP", 3),
            p=r.get("P", 4),
            bn=r.get("BN", 6),
            il=r.get("IL", 2),
        )

    def remaining_hitter_slots(self) -> int:
        return self.c + self.b1 + self.b2 + self.b3 + self.ss + self.of + self.util

    def remaining_pitcher_slots(self) -> int:
        return self.sp + self.rp + self.p

    def remaining_flex_slots(self) -> int:
        return self.bn + self.il


def _take_batter_slot(pos: set[str], slots: SlotState) -> str | None:
    """Greedy: C, 1B, 2B, 3B, SS, OF×3, Util×2."""
    order = [
        ("C", "c", lambda: "C" in pos),
        ("1B", "b1", lambda: "1B" in pos),
        ("2B", "b2", lambda: "2B" in pos),
        ("3B", "b3", lambda: "3B" in pos),
        ("SS", "ss", lambda: "SS" in pos),
    ]
    for label, attr, ok in order:
        if ok() and getattr(slots, attr) > 0:
            setattr(slots, attr, getattr(slots, attr) - 1)
            return label
    if "OF" in pos and slots.of > 0:
        slots.of -= 1
        return "OF"
    if can_fill_util(pos) and slots.util > 0:
        slots.util -= 1
        return "Util"
    return None


def _take_pitcher_slot(pos: set[str], slots: SlotState) -> str | None:
    if "SP" in pos and slots.sp > 0:
        slots.sp -= 1
        return "SP"
    if "RP" in pos and slots.rp > 0:
        slots.rp -= 1
        return "RP"
    if bool(pos & _PITCH) and slots.p > 0:
        slots.p -= 1
        return "P"
    return None


def _take_bench(slots: SlotState) -> str | None:
    if slots.bn > 0:
        slots.bn -= 1
        return "BN"
    if slots.il > 0:
        slots.il -= 1
        return "IL"
    return None


def assign_pick_to_slot(pos: set[str], slots: SlotState) -> str:
    """
    Assign one player to the next open slot. Two-way players prefer batting first
    if any hitting slot matches, else pitching.
    """
    po = is_pitcher_only(pos)
    hi = is_hitter(pos)

    if hi and not po:
        s = _take_batter_slot(pos, slots)
        if s:
            return s
        s = _take_pitcher_slot(pos, slots)
        if s:
            return s
        b = _take_bench(slots)
        return b or "OVER"

    if po and not hi:
        s = _take_pitcher_slot(pos, slots)
        if s:
            return s
        b = _take_bench(slots)
        return b or "OVER"

    # Two-way or ambiguous
    s = _take_batter_slot(pos, slots)
    if s:
        return s
    s = _take_pitcher_slot(pos, slots)
    if s:
        return s
    b = _take_bench(slots)
    return b or "OVER"


@dataclass
class DraftPick:
    player_key: str
    name: str = ""
    positions: list[str] = field(default_factory=list)


def analyze_draft_picks(
    picks: list[DraftPick | dict],
    cfg: LHFLeagueConfig | None = None,
) -> dict:
    """
    Process picks in order; return remaining slot counts, assignments, and need summary.
    """
    cfg = cfg or get_lhf_config()
    slots = SlotState.from_config(cfg)
    assignments: list[dict] = []

    for raw in picks:
        if isinstance(raw, DraftPick):
            pick = raw
        else:
            pick = DraftPick(
                player_key=raw.get("player_key", ""),
                name=raw.get("name", ""),
                positions=list(raw.get("positions") or []),
            )
        pos = normalize_yahoo_positions(pick.positions)
        slot = assign_pick_to_slot(pos, slots)
        assignments.append(
            {
                "player_key": pick.player_key,
                "name": pick.name,
                "positions": pick.positions,
                "normalized_positions": sorted(pos),
                "assigned_slot": slot,
            }
        )

    remaining = {
        "C": slots.c,
        "1B": slots.b1,
        "2B": slots.b2,
        "3B": slots.b3,
        "SS": slots.ss,
        "OF": slots.of,
        "Util": slots.util,
        "SP": slots.sp,
        "RP": slots.rp,
        "P": slots.p,
        "BN": slots.bn,
        "IL": slots.il,
    }

    # Positions still needed (flatten OF/Util counts)
    positions_of_need: list[str] = []
    for key, n in [
        ("C", slots.c),
        ("1B", slots.b1),
        ("2B", slots.b2),
        ("3B", slots.b3),
        ("SS", slots.ss),
    ]:
        positions_of_need.extend([key] * n)
    positions_of_need.extend(["OF"] * slots.of)
    positions_of_need.extend(["Util"] * slots.util)
    positions_of_need.extend(["SP"] * slots.sp)
    positions_of_need.extend(["RP"] * slots.rp)
    positions_of_need.extend(["P"] * slots.p)

    return {
        "assignments": assignments,
        "remaining_slots": remaining,
        "positions_of_need": positions_of_need,
        "summary": {
            "hitter_lineup_slots_open": slots.c + slots.b1 + slots.b2 + slots.b3 + slots.ss + slots.of + slots.util,
            "pitcher_lineup_slots_open": slots.sp + slots.rp + slots.p,
            "bench_il_open": slots.bn + slots.il,
        },
    }


def positional_need_boost(
    player_positions: list[str],
    positions_of_need: list[str],
    *,
    need_weight: float = 8.0,
) -> float:
    """
    Additive boost to a 0-100 style score when player fills a scarce position.
    Overlapping needs (e.g. need SS and OF) — max boost if any eligible slot matches.
    """
    if not positions_of_need:
        return 0.0
    pos = normalize_yahoo_positions(player_positions)
    need_set = set(positions_of_need)
    score = 0.0

    for need in need_set:
        if need == "C" and "C" in pos:
            score = max(score, need_weight)
        elif need == "1B" and "1B" in pos:
            score = max(score, need_weight)
        elif need == "2B" and "2B" in pos:
            score = max(score, need_weight)
        elif need == "3B" and "3B" in pos:
            score = max(score, need_weight)
        elif need == "SS" and "SS" in pos:
            score = max(score, need_weight)
        elif need == "OF" and "OF" in pos:
            score = max(score, need_weight * 0.9)
        elif need == "Util" and can_fill_util(pos):
            score = max(score, need_weight * 0.7)
        elif need == "SP" and "SP" in pos:
            score = max(score, need_weight * 0.85)
        elif need == "RP" and "RP" in pos:
            score = max(score, need_weight * 0.85)
        elif need == "P" and bool(pos & _PITCH):
            score = max(score, need_weight * 0.75)

    return round(score, 2)


def score_candidate(
    or_rank: int,
    player_positions: list[str],
    positions_of_need: list[str],
) -> dict:
    """
    Combine Yahoo overall rank (lower is better) with positional need.
    base_score: higher = better (0-100 scale from rank).
    """
    base = max(0.0, 100.0 - (or_rank - 1) * 0.25)
    boost = positional_need_boost(player_positions, positions_of_need)
    combined = min(100.0, base + boost)
    return {
        "or_rank": or_rank,
        "base_score": round(base, 2),
        "positional_boost": boost,
        "combined_score": round(combined, 2),
    }
