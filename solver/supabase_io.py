"""Read one campus-day from Supabase, solve it, and save the draft adjustments.

Uses only the Supabase REST API (stdlib urllib) with the service-role key, so it
runs inside the Vercel function. Nothing here knows a teacher, section or time:
all of it is read from the database.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date as _date

from .model import DAYS, Event, Rule, TeacherIndex, finish, hhmm_to_min, load_sheet
from .solve import Options, solve

CHANGE_OF_CODE = {"ROT": "rot", "SHF": "shf", "LAB": "lab", "ADJ": "adj", "SPORT": "sport", "! FIX": "fix"}
FILL_PRIORITY = ["fix", "lab", "sport", "adj", "shf", "rot"]  # same order as the web app


class Conflict(Exception):
    """A published adjustment already exists for this day."""


class Rest:
    def __init__(self, url: str | None = None, key: str | None = None):
        url = url or os.environ["SUPABASE_URL"]
        key = key or os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, body=None, extra: dict | None = None):
        headers = {**self.headers, **(extra or {})}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt else None

    def select(self, table: str, **filters) -> list[dict]:
        """GET all rows (paged by 1000). filters are PostgREST params, e.g. campus_id="eq.<id>"."""
        out, start = [], 0
        while True:
            q = urllib.parse.urlencode(filters, safe=".,()*")
            rows = self._call("GET", f"{table}?{q}", extra={"Range-Unit": "items", "Range": f"{start}-{start + 999}"})
            out += rows
            if len(rows) < 1000:
                return out
            start += 1000

    def upsert(self, table: str, rows: list[dict], on_conflict: str):
        return self._call("POST", f"{table}?on_conflict={on_conflict}", rows,
                          {"Prefer": "resolution=merge-duplicates,return=representation"})


def _hhmm(t: str | None) -> str | None:
    return t[:5] if t else None


def run_day(campus_id: str, day_iso: str, db: Rest | None = None, options: Options | None = None,
            overwrite_published: bool = False) -> dict:
    db = db or Rest()
    d = _date.fromisoformat(day_iso)
    day = DAYS[d.weekday()] if d.weekday() < 6 else None
    if day is None:
        raise ValueError("Sunday has no timetable")
    eq = lambda v: f"eq.{v}"

    # ---- which timetable versions cover this day (latest per year)
    versions = db.select("timetable_versions", campus_id=eq(campus_id), effective_from=f"lte.{day_iso}",
                         order="effective_from.desc")
    chosen: dict[str, dict] = {}
    for v in versions:
        if v["effective_to"] and v["effective_to"] < day_iso:
            continue
        if v["day_kinds"] and day not in v["day_kinds"]:
            continue
        chosen.setdefault(v["year_label"], v)
    if not chosen:
        raise ValueError(f"No timetable version covers {day} {day_iso}")
    versions = sorted(chosen.values(), key=lambda v: v["year_label"])

    # ---- teachers, labs, sections
    t_rows = db.select("teachers", campus_id=eq(campus_id))
    t_name = {t["id"]: t["name"] for t in t_rows}
    idx = TeacherIndex()
    for t in t_rows:
        idx.add(t["name"])
    sec_rows = db.select("sections", campus_id=eq(campus_id))
    sec_code = {s["id"]: s["code"] for s in sec_rows}
    sec_id = {s["code"]: s["id"] for s in sec_rows}

    # ---- rebuild each version as an extracted-sheet dict, then load it
    prob = {"periods": {}, "sections": {}, "lessons": [], "teachers": {}, "section_teachers": {}}
    tags: dict[str, dict] = {}  # "Y1" -> {"version": row, "period_ids": [uuid by schedule index]}
    for i, v in enumerate(versions):
        tag = f"Y{i + 1}"
        periods = sorted(db.select("periods", version_id=eq(v["id"])), key=lambda p: p["seq"])
        cells = db.select("timetable_cells", version_id=eq(v["id"]))
        cell_at = {(c["section_id"], c["period_id"]): c for c in cells}
        sheet_secs = [s for s in sec_rows if any(c["section_id"] == s["id"] for c in cells)]
        schedule = [{"period": p["number"], "kind": p["kind"],
                     "time": {"start": _hhmm(p["starts_at"]), "end": _hhmm(p["ends_at"])}} for p in periods]
        slot_periods = [p for p in periods if p["kind"] != "break"]
        sections = []
        for s in sorted(sheet_secs, key=lambda s: s["code"]):
            slots = []
            for p in slot_periods:
                c = cell_at.get((s["id"], p["id"]))
                entries = []
                for e in (c["entries"] if c else []):
                    e = dict(e)
                    e["teacher"] = t_name.get(e.get("teacher_id")) or e["teacher"]
                    entries.append(e)
                slots.append({"kind": p["kind"], "period": p["number"], "raw": c["raw"] if c else None,
                              "entries": entries})
            sections.append({"section": s["code"], "group": "+".join(s["groups"] or []), "slots": slots})
        sheet = {"sheet": v["label"], "schedule": schedule, "sections": sections}
        load_sheet({"sheets": [sheet], "teacher_aliases": {}}, v["label"], v["year_label"], day, idx, tag, prob)
        tags[tag] = {"version": v, "period_ids": [p["id"] for p in periods]}

    # ---- teachers outside the grid (lab in-charges, counsellors) keep their category
    categories = {}
    for t in t_rows:
        k = idx.resolve(t["name"])
        if k and t.get("category"):
            categories[k] = t["category"]
            prob["teachers"].setdefault(k, {"category": t["category"], "subjects": set()})

    def key(tid_or_name: str) -> str | None:
        return idx.resolve(t_name.get(tid_or_name, tid_or_name))

    events = []
    for e in db.select("daily_events", campus_id=eq(campus_id), day=eq(day_iso)):
        k = key(e["teacher_id"])
        if k:
            events.append(Event(k, e["type"], hhmm_to_min(e["from_time"]), hhmm_to_min(e["to_time"]), e.get("reason")))

    labs = {}
    for l in db.select("labs", campus_id=eq(campus_id)):
        labs[l["code"]] = {"name": l["name"], "incharge": key(l["incharge_id"]) if l.get("incharge_id") else None}

    rules = []
    for r in db.select("rules", campus_id=eq(campus_id), active="eq.true", confirmed="eq.true"):
        rule = Rule(r["rule_type"], r["kind"], r.get("params") or {},
                    [k for k in (key(t) for t in r["teacher_ids"]) if k],
                    [sec_code[s] for s in r["section_ids"] if s in sec_code],
                    r.get("weight", 0), r.get("valid_from"), r.get("valid_to"), True, True,
                    r.get("title", ""), r.get("source") or "")
        if rule.applies_on(day_iso):
            rules.append(rule)

    problem = finish(prob, day_iso, day, events, rules, labs, None, categories)
    result = solve(problem, options or Options(time_limit_s=40.0))

    # ---- turn the cells into per-version `changes`
    per_version: dict[str, list] = {t["version"]["id"]: [] for t in tags.values()}
    for c in result.get("cells", []):
        tag, seq = c["period_id"].split(":")
        info = tags[tag]
        kinds = [CHANGE_OF_CODE[x] for x in c["codes"]]
        kinds.sort(key=FILL_PRIORITY.index)
        change = {"section_id": sec_id[c["section"]], "period_id": info["period_ids"][int(seq) - 1],
                  "change": kinds[0], "before": c["before"], "after": c["after"] or "", "struck": c["struck"]}
        if len(kinds) > 1:
            change["also"] = kinds[1:]
        if c.get("reason"):
            change["reason"] = c["reason"]
        per_version[info["version"]["id"]].append(change)

    saved = []
    if result["status"] in ("OPTIMAL", "FEASIBLE"):
        existing = db.select("adjustments", campus_id=eq(campus_id), day=eq(day_iso))
        if any(a["status"] == "published" for a in existing) and not overwrite_published:
            raise Conflict("A published adjustment exists for this day; pass overwrite to replace it with a new draft")
        notes = {"status": result["status"], "objective": result["objective"], "summary": result["summary"],
                 "rules_used": result["rules_used"], "base_conflicts": result.get("base_conflicts")}
        rows = [{"campus_id": campus_id, "day": day_iso, "version_id": vid, "status": "draft",
                 "changes": ch, "solver_notes": notes, "published_at": None}
                for vid, ch in per_version.items()]
        saved = db.upsert("adjustments", rows, "campus_id,day,version_id")
    return {"ok": result["status"] in ("OPTIMAL", "FEASIBLE"), "status": result["status"],
            "summary": result.get("summary"), "fix_cells": result.get("summary", {}).get("! FIX", 0),
            "adjustments": [a["id"] for a in saved], "rules_applied": len(rules)}
