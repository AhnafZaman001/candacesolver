"""Problem data for the Candace daily timetable solver.

Everything about a campus (teachers, sections, periods, timings, rules) arrives
as data. Nothing here knows a teacher name, section code or time of day.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]

# Subject keywords -> lab code, and which subjects count as "science" (for
# "5th lecture: science first, arts last"). Data-level vocabulary, not rules.
LAB_OF_SUBJECT = {"PHY": "PL", "PHYSICS": "PL", "CHEM": "CL", "CHEMISTRY": "CL",
                  "BIO": "BL", "BIOLOGY": "BL", "COMP": "COL", "COMPUTER": "COL", "COMP SCI": "COL"}
SCIENCE_WORDS = ("PHY", "CHEM", "BIO", "MATH", "COMP", "STAT")
LOCKABLE_SUBJECT_WORDS = {"PAK STUDIES": "PS", "P.S": "PS", "TQ": "TQ"}


def norm(name: str) -> str:
    s = re.sub(r"^P\s*[.,]\s*", "", (name or "").strip(), flags=re.I)
    return re.sub(r"[.\s]+", " ", s).strip().upper()


def squash(name: str) -> str:
    return norm(name).replace(" ", "")


def subject_key(subject: str | None) -> str:
    return (subject or "").strip().upper()


def is_science(subject: str | None) -> bool:
    s = subject_key(subject)
    return any(w in s for w in SCIENCE_WORDS)


def lab_for(subject: str | None) -> str | None:
    s = subject_key(subject)
    for k, v in LAB_OF_SUBJECT.items():
        if s.startswith(k):
            return v
    return None


def hhmm_to_min(t: str | None) -> int | None:
    if not t:
        return None
    h, m = t.split(":")[:2]
    return int(h) * 60 + int(m)


@dataclass
class Period:
    id: str            # unique within a sheet, e.g. "1Y:P3"
    sheet: str
    seq: int
    kind: str          # class | test | break
    number: int | None
    start: int | None  # minutes since midnight
    end: int | None


@dataclass
class Entry:
    subject: str | None
    teacher: str       # canonical teacher key
    raw: str


@dataclass
class Lesson:
    """What a section has in one period today (one or more entries = split class)."""
    id: str
    section: str
    period: str
    entries: list[Entry]
    raw: str | None
    rotation: bool = False   # came from a day-tagged (rotation) cell
    test: bool = False

    @property
    def teachers(self) -> list[str]:
        return [e.teacher for e in self.entries]

    @property
    def split(self) -> bool:
        return len(self.entries) > 1


@dataclass
class Section:
    code: str
    sheet: str
    year: str
    groups: list[str]
    periods: list[str]  # class period ids in day order (breaks/tests excluded)
    blocks: list[list[str]]  # class periods split by breaks


@dataclass
class Event:
    teacher: str
    type: str          # absent | late | early_leave | free_block | duty
    start: int | None = None
    end: int | None = None
    reason: str | None = None


@dataclass
class Rule:
    rule_type: str
    kind: str          # hard | soft | temporary
    params: dict
    teachers: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    weight: int = 0
    valid_from: str | None = None
    valid_to: str | None = None
    active: bool = True
    confirmed: bool = False
    title: str = ""
    source: str = ""

    def applies_on(self, date: str) -> bool:
        return (not self.valid_from or self.valid_from <= date) and (not self.valid_to or self.valid_to >= date)


@dataclass
class Problem:
    date: str
    day: str
    periods: dict[str, Period]
    sections: dict[str, Section]
    lessons: list[Lesson]
    teachers: dict[str, dict]      # key -> {"category": science|arts|lab_incharge|counsellor|..., "subjects": set}
    section_teachers: dict[str, set[str]]
    events: list[Event]
    rules: list[Rule]
    labs: dict[str, dict]          # code -> {"name":..., "incharge": teacher key or None}
    lab_needs: dict[str, set[str]]  # section -> lab codes still pending this week


# --------------------------------------------------------------- loading
class TeacherIndex:
    """Resolves the many spellings of a teacher to one key."""

    def __init__(self):
        self.keys: dict[str, str] = {}

    def add(self, name: str, canonical: str | None = None) -> str:
        key = norm(canonical or name)
        self.keys.setdefault(squash(name), key)
        self.keys.setdefault(squash(key), key)
        return key

    def resolve(self, name: str) -> str | None:
        sq = squash(name)
        if sq in self.keys:
            return self.keys[sq]
        n = norm(name).split()
        canon = set(self.keys.values())
        hits = [c for c in canon if len(c.split()) >= len(n) and all(c.split()[i].startswith(n[i]) for i in range(len(n)))]
        if not hits:
            hits = [c for c in canon if c.replace(" ", "").startswith(sq) and len(sq) >= 4]
        if not hits:
            hits = [c for c in canon if difflib.SequenceMatcher(None, c.replace(" ", ""), sq).ratio() >= 0.88]
        return hits[0] if len(set(hits)) == 1 else None


def load_sheet(data: dict, sheet_name: str, year: str, day: str, idx: TeacherIndex, tag: str, prob: dict):
    """Adds one extracted timetable sheet (tools/extract_timetable.py output) to the problem."""
    sheet = next(s for s in data["sheets"] if s["sheet"] == sheet_name)
    aliases = data.get("teacher_aliases", {})
    for alias, target in aliases.items():
        idx.add(alias, target or alias)

    pids = []
    for seq, slot in enumerate(sheet["schedule"], 1):
        t = slot.get("time") or {}
        pid = f"{tag}:{seq}"
        prob["periods"][pid] = Period(pid, sheet_name, seq, slot["kind"], slot.get("period"),
                                      hhmm_to_min(t.get("start")), hhmm_to_min(t.get("end")))
        pids.append(pid)
    slot_pids = [p for p in pids if prob["periods"][p].kind != "break"]

    for sec in sheet["sections"]:
        code = sec["section"]
        groups = [g for g in re.split(r"[+\s]+", sec.get("group") or "") if g]
        class_pids, blocks, cur = [], [], []
        for p in pids:
            per = prob["periods"][p]
            if per.kind == "break":
                if cur:
                    blocks.append(cur)
                cur = []
            elif per.kind == "class":
                class_pids.append(p)
                cur.append(p)
        if cur:
            blocks.append(cur)
        prob["sections"][code] = Section(code, sheet_name, year, groups, class_pids, blocks)
        prob["section_teachers"].setdefault(code, set())

        for i, cell in enumerate(sec["slots"]):
            pid = slot_pids[i]
            per = prob["periods"][pid]
            raw_entries = cell.get("entries") or cell.get("invigilators") or []
            tagged = any(e.get("days") for e in raw_entries)
            today = [e for e in raw_entries if not e.get("days") or day in e["days"]]
            entries = []
            for e in today:
                key = idx.resolve(e["teacher"]) or idx.add(e["teacher"])
                entries.append(Entry(e.get("subject"), key, e.get("teacher_raw", e["teacher"])))
            # Every teacher who teaches this section on any day counts as "own section".
            for e in raw_entries:
                key = idx.resolve(e["teacher"]) or idx.add(e["teacher"])
                if cell["kind"] == "class":
                    prob["section_teachers"][code].add(key)
                    t = prob["teachers"].setdefault(key, {"category": None, "subjects": set()})
                    if e.get("subject"):
                        t["subjects"].add(subject_key(e["subject"]))
            prob["lessons"].append(Lesson(f"{code}@{pid}", code, pid, entries, cell.get("raw"),
                                          rotation=tagged, test=per.kind == "test"))


def finish(prob: dict, date: str, day: str, events: list[Event], rules: list[Rule],
           labs: dict, lab_needs: dict | None, categories: dict | None) -> Problem:
    for key, t in prob["teachers"].items():
        if categories and key in categories:
            t["category"] = categories[key]
        elif t["category"] is None:
            t["category"] = "science" if any(is_science(s) for s in t["subjects"]) else "arts"
    needs = {}
    for code in prob["sections"]:
        if lab_needs is not None and code in lab_needs:
            needs[code] = set(lab_needs[code])
        else:  # default: every lab subject the section is taught
            subs = {e.subject for l in prob["lessons"] if l.section == code for e in l.entries}
            needs[code] = {lab_for(s) for s in subs if lab_for(s)}
    return Problem(date, day, prob["periods"], prob["sections"], prob["lessons"], prob["teachers"],
                   prob["section_teachers"], events, rules, labs, needs)


def load_case(case_path: str | Path, include_unconfirmed: bool = False) -> Problem:
    """A case file names the date, the extracted timetable sheets, the day's events,
    labs, lab needs and the rules file to use."""
    case_path = Path(case_path)
    case = json.loads(case_path.read_text(encoding="utf-8"))
    base = case_path.parent
    date, day = case["date"], case["day"]
    idx = TeacherIndex()
    prob = {"periods": {}, "sections": {}, "lessons": [], "teachers": {}, "section_teachers": {}}
    for i, sh in enumerate(case["sheets"]):
        data = json.loads((base / sh["file"]).read_text(encoding="utf-8"))
        load_sheet(data, sh["sheet"], sh["year"], day, idx, f"Y{i + 1}", prob)

    def key(name: str) -> str:
        k = idx.resolve(name)
        if not k:
            raise ValueError(f"Unknown teacher in case: {name!r}")
        return k

    for extra in case.get("extra_teachers", []):  # counsellors, lab incharges not in the grid
        k = idx.add(extra["name"])
        prob["teachers"].setdefault(k, {"category": extra.get("category"), "subjects": set()})
        prob["teachers"][k]["category"] = extra.get("category")

    events = [Event(key(e["teacher"]), e["type"], hhmm_to_min(e.get("from")), hhmm_to_min(e.get("to")), e.get("reason"))
              for e in case.get("events", [])]
    labs = {code: {"name": l["name"], "incharge": key(l["incharge"]) if l.get("incharge") else None}
            for code, l in case.get("labs", {}).items()}
    rules_raw = json.loads((base / case["rules"]).read_text(encoding="utf-8"))["rules"] if case.get("rules") else []
    rules = []
    for r in rules_raw:
        rule = Rule(r["rule_type"], r["kind"], r.get("params", {}), [key(t) for t in r.get("teachers", [])],
                    r.get("sections", []), r.get("weight", 0), r.get("valid_from"), r.get("valid_to"),
                    r.get("active", True), r.get("confirmed", False), r.get("title", ""), r.get("source", ""))
        if rule.active and rule.applies_on(date) and (rule.confirmed or include_unconfirmed):
            rules.append(rule)
    categories = {key(n): c for n, c in case.get("categories", {}).items()}
    return finish(prob, date, day, events, rules, labs, case.get("lab_needs"), categories)
