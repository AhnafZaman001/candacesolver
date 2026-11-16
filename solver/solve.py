"""Candace daily timetable solver (Google OR-Tools CP-SAT).

Given the day's base timetable (both years together, since teachers and lab rooms
are shared), the day's events (absent / late / early leave / free block) and the
campus's confirmed rules, it decides for every section-period what happens:
the original lecture stays, a lecture moves within the section (SHF), a teacher
of that section covers (ADJ), a lab runs (LAB), a sports period (SPORT), a
counsellor period, or — if nothing valid is left — "! FIX" for the admin.

Built-in invariants (not admin preferences): a teacher is in one place at a
time, a section has one thing per period, a lab room holds one class at a time,
and covers come from teachers who already teach that section.
Everything else comes from rules (see solver/RULES.md).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .model import Problem, Rule, is_science, lab_for, subject_key

FIX_COST = 100_000
DROP_COST = 5_000  # a lecture whose teacher is present somewhere today should not vanish


@dataclass
class Options:
    time_limit_s: float = 20.0
    workers: int = 8


def _rules(p: Problem, t: str) -> list[Rule]:
    return [r for r in p.rules if r.rule_type == t]


def _rule(p: Problem, t: str) -> Rule | None:
    rs = _rules(p, t)
    return rs[0] if rs else None


def _applies(r: Rule, teacher: str) -> bool:
    return not r.teachers or teacher in r.teachers


def solve(p: Problem, opt: Options | None = None) -> dict:
    opt = opt or Options()
    m = cp_model.CpModel()
    P = p.periods
    cost_terms: list = []

    # ------------------------------------------------------------ availability
    unavailable: dict[str, set[str]] = defaultdict(set)  # teacher -> period ids
    class_periods = [pid for pid, per in P.items() if per.kind in ("class", "test")]
    for e in p.events:
        for pid in class_periods:
            per = P[pid]
            if e.type == "absent":
                unavailable[e.teacher].add(pid)
            elif e.type == "late" and e.start is not None and per.start is not None and per.start < e.start:
                unavailable[e.teacher].add(pid)
            elif e.type == "early_leave" and e.end is not None and per.end is not None and per.end > e.end:
                unavailable[e.teacher].add(pid)
            elif e.type == "free_block" and e.start is not None and e.end is not None and per.start is not None:
                if per.start < e.end and per.end > e.start:
                    unavailable[e.teacher].add(pid)
    for r in _rules(p, "available_window"):
        if r.params.get("days") and p.day not in r.params["days"]:
            continue
        nums = set(r.params.get("periods", []))
        frm = r.params.get("from_period")
        for t in r.teachers:
            for pid in class_periods:
                n = P[pid].number
                if P[pid].kind != "class" or n is None:
                    continue
                if (nums and n not in nums) or (frm is not None and n < frm):
                    unavailable[t].add(pid)

    def avail(t: str, pid: str) -> bool:
        return pid not in unavailable[t]

    lab_down = set()
    if _rule(p, "lab_needs_incharge"):
        absent = {e.teacher for e in p.events if e.type == "absent"}
        lab_down = {code for code, lab in p.labs.items() if lab.get("incharge") in absent}

    # ----------------------------------------------------------- what can move
    lock_subjects = set()
    for r in _rules(p, "lock_subjects"):
        lock_subjects |= {s.upper() for s in r.params.get("subjects", [])}
    lock_teachers = set()
    for r in _rules(p, "lock_teachers"):
        lock_teachers |= set(r.teachers)
    rotation_movable = bool(_rule(p, "allow_rotation_shuffle"))
    shuffle_rule = _rule(p, "shuffle_penalty")
    shuffle_cost = int(shuffle_rule.params.get("per_move", 30)) if shuffle_rule else None  # None = no shuffling

    def locked(lesson) -> bool:
        if lesson.split or lesson.test or not lesson.entries or shuffle_cost is None:
            return True
        if lesson.rotation and not rotation_movable:
            return True
        e = lesson.entries[0]
        if any(w in subject_key(e.subject) for w in lock_subjects) or e.teacher in lock_teachers:
            return True
        return False

    lessons_at = {(l.section, l.period): l for l in p.lessons}

    # assignment terms per teacher per period: (literal or 1, is_cover)
    load_terms: dict[str, list] = defaultdict(list)            # teacher -> literals (lectures today)
    occ: dict[tuple[str, str], list] = defaultdict(list)        # (teacher, pid) -> literals
    slot_terms: dict[tuple[str, str], list] = defaultdict(list)  # (section, pid) -> literals summing to 1
    fixed_occupied: set[tuple[str, str]] = set()
    record: dict = {"move": {}, "cover": {}, "lab": {}, "sports": {}, "couns": {}, "fix": {}, "glab": {},
                    "gsports": {}, "gfix": {}, "empty": {}}

    sec_periods = {code: [pid for pid in s.periods] for code, s in p.sections.items()}

    # Lessons: fixed in place, or movable within the section.
    for code, sec in p.sections.items():
        for pid in sec.periods:
            l = lessons_at.get((code, pid))
            if l is None or not l.entries:
                v = m.NewBoolVar(f"empty[{code},{pid}]")
                record["empty"][(code, pid)] = v
                slot_terms[(code, pid)].append(v)
                continue
            if locked(l):
                present = [e for e in l.entries if avail(e.teacher, pid)]
                if not present:
                    continue  # slot becomes a vacancy (needs a fill)
                fixed_occupied.add((code, pid))
                for e in present:
                    occ[(e.teacher, pid)].append(1)
                    load_terms[e.teacher].append(1)
                for e in l.entries:
                    if e not in present:  # one group of a split class is uncovered
                        _group_vacancy(m, p, record, code, pid, e, lab_down, cost_terms)
                continue
            t = l.entries[0].teacher
            options = []
            for q in sec_periods[code]:
                if not avail(t, q):
                    continue
                ql = lessons_at.get((code, q))
                if q != pid and ql is not None and ql.entries and locked(ql) and any(avail(x.teacher, q) for x in ql.entries):
                    continue  # target occupied by a fixed lesson
                y = m.NewBoolVar(f"y[{l.id}->{q}]")
                record["move"][(l.id, q)] = y
                options.append(y)
                slot_terms[(code, q)].append(y)
                occ[(t, q)].append(y)
                load_terms[t].append(y)
                if q != pid:
                    cost_terms.append(shuffle_cost * y)
            drop = m.NewBoolVar(f"drop[{l.id}]")
            m.Add(sum(options) + drop == 1)
            if options:
                cost_terms.append(DROP_COST * drop)

    # ------------------------------------------------------------------ fills
    fill_rules = {r.params.get("year"): r for r in _rules(p, "fill_costs")}
    default_costs = {"cover": 10, "cover_extra": 25, "lab": 20, "sports": 40, "counsellor": 60}
    no_extra = set()
    for r in _rules(p, "no_extra_lectures"):
        no_extra |= set(r.teachers)
    counsellor_rule = _rule(p, "counsellors")
    counsellors = list(counsellor_rule.teachers) if counsellor_rule else []
    prio = _rule(p, "extra_subject_priority")

    def costs_for(year: str) -> dict:
        r = fill_rules.get(year) or fill_rules.get(None)
        return {**default_costs, **(r.params.get("costs", {}) if r else {})}

    def subject_bonus(code: str, subjects: set[str]) -> int:
        if not prio:
            return 0
        groups = p.sections[code].groups
        prefer = set()
        for g in groups:
            prefer |= {s.upper() for s in prio.params.get("groups", {}).get(g.upper(), [])}
        last = {s.upper() for s in prio.params.get("last", [])}
        if any(any(s.startswith(x) for x in prefer) for s in subjects):
            return -int(prio.params.get("bonus", 4))
        if any(any(s.startswith(x) for x in last) for s in subjects):
            return int(prio.params.get("malus", 4))
        return 0

    for code, sec in p.sections.items():
        c = costs_for(sec.year)
        for pid in sec.periods:
            if (code, pid) in fixed_occupied:
                continue
            terms = slot_terms[(code, pid)]
            for t in sorted(p.section_teachers.get(code, ())):
                if t in no_extra or t in counsellors or not avail(t, pid):
                    continue
                subj = {s for s in p.teachers.get(t, {}).get("subjects", set())}
                v = m.NewBoolVar(f"cover[{code},{pid},{t}]")
                record["cover"][(code, pid, t)] = v
                terms.append(v)
                occ[(t, pid)].append(v)
                load_terms[t].append(v)
                cost_terms.append((c["cover"] + subject_bonus(code, subj)) * v)
            for lab in sorted(p.lab_needs.get(code, ())):
                if lab in lab_down or lab not in p.labs:
                    continue
                v = m.NewBoolVar(f"lab[{code},{pid},{lab}]")
                record["lab"][(code, pid, lab)] = v
                terms.append(v)
                cost_terms.append(c["lab"] * v)
            if c.get("sports") is not None:
                v = m.NewBoolVar(f"sports[{code},{pid}]")
                record["sports"][(code, pid)] = v
                terms.append(v)
                cost_terms.append(c["sports"] * v)
            for t in counsellors:
                if not avail(t, pid):
                    continue
                v = m.NewBoolVar(f"couns[{code},{pid},{t}]")
                record["couns"][(code, pid, t)] = v
                terms.append(v)
                occ[(t, pid)].append(v)
                load_terms[t].append(v)
                cost_terms.append(c["counsellor"] * v)
            fx = m.NewBoolVar(f"fix[{code},{pid}]")
            record["fix"][(code, pid)] = fx
            terms.append(fx)
            cost_terms.append(FIX_COST * fx)

    for (code, pid), terms in slot_terms.items():
        if (code, pid) in fixed_occupied:
            m.Add(sum(terms) == 0)
        else:
            m.Add(sum(terms) == 1)

    # ------------------------------------------------- one place at a time
    overlap = {pid: [q for q in class_periods
                     if P[q].start is not None and P[pid].start is not None
                     and P[q].start < P[pid].end and P[q].end > P[pid].start] for pid in class_periods}
    teachers = {t for (t, _) in occ}
    base_conflicts: list[str] = []
    for t in teachers:
        for pid in class_periods:
            lits = [x for q in overlap[pid] for x in occ.get((t, q), [])]
            fixed = sum(1 for x in lits if isinstance(x, int))
            vars_ = [x for x in lits if not isinstance(x, int)]
            if fixed > 1:
                base_conflicts.append(f"{t} is in {fixed} places at {P[pid].sheet} P{P[pid].number} in the base timetable")
            if vars_:
                m.Add(sum(vars_) <= max(0, 1 - fixed))

    # ------------------------------------------------------------- labs
    lab_lits = defaultdict(list)  # (lab, pid) -> literals
    for (code, pid, lab), v in record["lab"].items():
        lab_lits[(lab, pid)].append(v)
    for (code, pid, lab), v in record["glab"].items():
        lab_lits[(lab, pid)].append(v)
    for lab in p.labs:
        for pid in class_periods:
            lits = [x for q in overlap[pid] for x in lab_lits.get((lab, q), [])]
            if len(lits) > 1:
                m.Add(sum(lits) <= 1)
    per_sec_lab = defaultdict(list)
    for (code, pid, lab), v in list(record["lab"].items()) + list(record["glab"].items()):
        per_sec_lab[(code, lab)].append(v)
    for lits in per_sec_lab.values():
        m.Add(sum(lits) <= 1)  # the same lab twice in a day makes no sense for a section
    cap = _rule(p, "lab_daily_cap")
    if cap:
        per_type = int(cap.params.get("per_type", 5))
        for lab in p.labs:
            lits = [v for (c, q, l), v in list(record["lab"].items()) + list(record["glab"].items()) if l == lab]
            if lits:
                m.Add(sum(lits) <= per_type)

    # ------------------------------------------------------------ sports
    sl = _rule(p, "sports_limit")
    if sl:
        per_lecture = sl.params.get("per_lecture_per_year")
        per_section = sl.params.get("per_section_per_day")
        sports_all = list(record["sports"].items()) + [((c, q), v) for (c, q, _), v in record["gsports"].items()]
        if per_lecture is not None:
            buckets = defaultdict(list)
            for (code, pid), v in sports_all:
                buckets[(p.sections[code].year, P[pid].number)].append(v)
            for lits in buckets.values():
                m.Add(sum(lits) <= int(per_lecture))
        if per_section is not None:
            buckets = defaultdict(list)
            for (code, pid), v in sports_all:
                buckets[code].append(v)
            for lits in buckets.values():
                m.Add(sum(lits) <= int(per_section))
    av = _rule(p, "avoid_sports_period")
    if av:
        for (code, pid), v in record["sports"].items():
            if P[pid].number in av.params.get("numbers", []):
                cost_terms.append(int(av.weight or 15) * v)

    # ------------------------------------------- extra covers per section
    me = _rule(p, "max_extra_per_section")
    if me:
        buckets = defaultdict(list)
        for (code, pid, t), v in record["cover"].items():
            buckets[(code, t)].append(v)
        for lits in buckets.values():
            m.Add(sum(lits) <= int(me.params.get("max", 1)))

    if counsellor_rule:
        cmax = int(counsellor_rule.params.get("max_per_day", 2))
        for t in counsellors:
            lits = [v for (c, q, tt), v in record["couns"].items() if tt == t]
            if lits:
                m.Add(sum(lits) <= cmax)

    # --------------------------------------------------- daily load limits
    for r in _rules(p, "max_lectures_per_day"):
        mx = int(r.params["max"])
        extra_allowed = int(r.params.get("extra_allowed", 0))
        pen = r.params.get("extra_penalty", {"science": 30, "arts": 60})
        for t, terms in load_terms.items():
            if not _applies(r, t) or t in counsellors:
                continue
            fixed = sum(1 for x in terms if isinstance(x, int))
            lits = [x for x in terms if not isinstance(x, int)]
            if not lits:
                continue
            load = fixed + sum(lits)
            hard = max(mx + extra_allowed, fixed)  # never forbid what the base timetable already has
            m.Add(load <= hard)
            over = m.NewIntVar(0, 20, f"over[{t}]")
            m.Add(over >= load - max(mx, fixed))
            cat = p.teachers.get(t, {}).get("category") or "arts"
            cost_terms.append(int(pen.get(cat, pen.get("arts", 60))) * over)

    # ------------------------------------------------ consecutive lectures
    slot_keys = sorted({(P[q].start, P[q].end) for q in class_periods if P[q].kind == "class" and P[q].start is not None})
    blocks, cur = [], []
    for k in slot_keys:  # a gap between slots (the break) starts a new block
        if cur and k[0] > cur[-1][1] + 5:
            blocks.append(cur)
            cur = []
        cur.append(k)
    if cur:
        blocks.append(cur)
    by_key = defaultdict(list)
    for q in class_periods:
        if P[q].kind == "class":
            by_key[(P[q].start, P[q].end)].append(q)

    def busy(t, k):
        return [x for q in by_key[k] for x in occ.get((t, q), [])]

    hard_c = _rule(p, "consecutive_limit")
    soft_c = _rule(p, "avoid_consecutive")
    for t in teachers:
        for block in blocks:
            if hard_c:
                n = int(hard_c.params.get("max", 3)) + 1
                for i in range(len(block) - n + 1):
                    lits = [x for k in block[i:i + n] for x in busy(t, k)]
                    vars_ = [x for x in lits if not isinstance(x, int)]
                    fixed = sum(1 for x in lits if isinstance(x, int))
                    if vars_ and fixed + len(vars_) >= n:
                        # never make a run longer; tolerate one already in the base timetable
                        m.Add(fixed + sum(vars_) <= max(n - 1, fixed))
            if soft_c:
                n = int(soft_c.params.get("max", 2)) + 1
                for i in range(len(block) - n + 1):
                    lits = [x for k in block[i:i + n] for x in busy(t, k)]
                    vars_ = [x for x in lits if not isinstance(x, int)]
                    fixed = sum(1 for x in lits if isinstance(x, int))
                    if vars_ and fixed + len(vars_) >= n:
                        z = m.NewIntVar(0, len(lits), f"run[{t},{block[i]}]")
                        m.Add(fixed + sum(vars_) - (n - 1) <= z)
                        cost_terms.append(int(soft_c.weight or 20) * z)

    # ------------------------------------------- science out of the last period
    snl = _rule(p, "science_not_last")
    if snl:
        for code, sec in p.sections.items():
            last = sec.periods[-1] if sec.periods else None
            if not last:
                continue
            for (lid, q), y in record["move"].items():
                l = next(x for x in p.lessons if x.id == lid)
                if q == last and l.section == code and is_science(l.entries[0].subject):
                    cost_terms.append(int(snl.weight or 5) * y)

    m.Minimize(sum(cost_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = opt.time_limit_s
    solver.parameters.num_workers = opt.workers
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": solver.StatusName(status), "cells": [], "base_conflicts": sorted(set(base_conflicts))}
    out = _extract(p, solver, record, lessons_at, unavailable, status)
    out["base_conflicts"] = sorted(set(base_conflicts))
    return out


def _group_vacancy(m, p: Problem, record, code, pid, entry, lab_down, cost_terms):
    """One group of a split class lost its teacher: a lab for that group, sports, or FIX."""
    opts = []
    lab = lab_for(entry.subject)
    if lab and lab in p.labs and lab not in lab_down:
        v = m.NewBoolVar(f"glab[{code},{pid},{lab}]")
        record["glab"][(code, pid, lab)] = v
        opts.append(v)
        cost_terms.append(20 * v)
    v = m.NewBoolVar(f"gsports[{code},{pid},{entry.teacher}]")
    record["gsports"][(code, pid, entry.teacher)] = v
    opts.append(v)
    cost_terms.append(40 * v)
    fx = m.NewBoolVar(f"gfix[{code},{pid},{entry.teacher}]")
    record["gfix"][(code, pid, entry.teacher)] = fx
    opts.append(fx)
    cost_terms.append(FIX_COST * fx)
    m.Add(sum(opts) == 1)


def _extract(p: Problem, s: cp_model.CpSolver, record, lessons_at, unavailable, status) -> dict:
    P = p.periods
    cells = []
    moved_in = {}
    for (lid, q), y in record["move"].items():
        if s.Value(y):
            moved_in[(lid.split("@")[0], q)] = lid
    label = lambda l: " ".join(f"{e.subject or ''} {e.raw}".strip() for e in l.entries) if l else ""

    for code, sec in p.sections.items():
        for pid in sec.periods:
            orig = lessons_at.get((code, pid))
            codes, after, reason = [], None, None
            struck = [e.raw for e in (orig.entries if orig else []) if pid in unavailable[e.teacher]]
            lid = moved_in.get((code, pid))
            if lid and lid != f"{code}@{pid}":
                src = next(l for l in p.lessons if l.id == lid)
                codes.append("SHF")
                after = label(src)
                reason = f"moved from period {P[src.period].number}"
            elif lid:
                pass  # original stays
            else:
                for (c, q, t), v in record["cover"].items():
                    if c == code and q == pid and s.Value(v):
                        subj = sorted(p.teachers[t]["subjects"])[:1]
                        codes.append("ADJ")
                        after = f"{subj[0] if subj else ''} P.{t}".strip()
                for (c, q, lab), v in record["lab"].items():
                    if c == code and q == pid and s.Value(v):
                        codes.append("LAB")
                        after = p.labs[lab]["name"]
                if record["sports"].get((code, pid)) is not None and s.Value(record["sports"][(code, pid)]):
                    codes.append("SPORT")
                    after = "Sports period"
                for (c, q, t), v in record["couns"].items():
                    if c == code and q == pid and s.Value(v):
                        codes.append("ADJ")
                        after = f"P.{t} (Student Counsellor)"
                if record["fix"].get((code, pid)) is not None and s.Value(record["fix"][(code, pid)]):
                    codes.append("! FIX")
                    after = "Needs manual fix"
            for (c, q, lab), v in record["glab"].items():
                if c == code and q == pid and s.Value(v):
                    codes.append("LAB")
                    after = (after + " · " if after else "") + f"group: {p.labs[lab]['name']}"
            for (c, q, t), v in record["gsports"].items():
                if c == code and q == pid and s.Value(v):
                    codes.append("SPORT")
                    after = (after + " · " if after else "") + "group: Sports period"
            for (c, q, t), v in record["gfix"].items():
                if c == code and q == pid and s.Value(v):
                    codes.append("! FIX")
                    after = (after + " · " if after else "") + f"group of P.{t}: needs manual fix"
            locked_words = {w.upper() for r in p.rules if r.rule_type == "lock_subjects" for w in r.params.get("subjects", [])}
            is_ps_tq = orig and any(any(w in subject_key(e.subject) for w in locked_words) for e in orig.entries)
            if orig and orig.rotation and not codes and not is_ps_tq:
                codes.append("ROT")  # the day's teacher in a rotation cell (not the P.S/TQ alternation)
            if codes or struck:
                cells.append({
                    "section": code, "period": P[pid].number, "period_id": pid, "sheet": P[pid].sheet,
                    "codes": codes, "before": orig.raw if orig else None, "after": after,
                    "struck": struck, "reason": reason,
                })

    loads = defaultdict(int)
    for (lid, q), y in record["move"].items():
        if s.Value(y):
            l = next(x for x in p.lessons if x.id == lid)
            loads[l.entries[0].teacher] += 1
    for l in p.lessons:
        if not l.test:
            for e in l.entries:
                if (l.id, l.period) not in record["move"] and l.period not in unavailable[e.teacher]:
                    loads[e.teacher] += 1
    for (c, q, t), v in list(record["cover"].items()) + list(record["couns"].items()):
        if s.Value(v):
            loads[t] += 1
    count = lambda code: sum(1 for c in cells if code in c["codes"])
    return {
        "status": s.StatusName(status),
        "objective": s.ObjectiveValue(),
        "date": p.date, "day": p.day,
        "summary": {k: count(k) for k in ["SHF", "ADJ", "LAB", "SPORT", "! FIX", "ROT"]},
        "cells": cells,
        "teacher_loads": dict(sorted(loads.items())),
        "rules_used": sorted({r.rule_type for r in p.rules}),
    }
