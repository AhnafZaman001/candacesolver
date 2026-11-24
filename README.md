# candace-solver

The timetable solver behind [Candace](https://github.com/AhnafZaman001/Candace), run as its own service.
Given a day's base timetable, who is absent / late / leaving early, and the campus's confirmed rules, it builds
the adjusted timetable with Google OR-Tools (CP-SAT): keep, shuffle (SHF), cover (ADJ), lab (LAB),
sports (SPORT), or `! FIX` for the admin.

```
Candace web app ("Generate")  ──POST + x-solver-secret──►  candace-solver /api/solve  (Vercel, Mumbai)
                                                              ├─ reads timetable, events, rules (Supabase)
                                                              ├─ solves (OR-Tools)
                                                              └─ writes the draft into `adjustments`
```

## Layout
| Path | What |
|---|---|
| `api/solve.py` | Vercel Python function. `GET` = health check, `POST` = solve (needs `x-solver-secret`) |
| `solver/` | The solver: `model.py` (data), `solve.py` (CP-SAT), `cli.py`, `RULES.md` (rule types) |
| `solver/rules/` | Rule data (each rule has `confirmed`; only confirmed rules are applied) |
| `solver/cases/` | Benchmark cases, e.g. `2026-11-12` |
| `data/` | Extracted timetables used by the benchmark cases |
| `vercel.json` | Runs in `bom1` (next to Supabase in Mumbai), up to 60 s per run |
| `requirements.txt` | `ortools`, installed by Vercel |

## Run locally
```bash
pip install -r requirements.txt
python -m solver.cli solver/cases/2026-11-12/case.json
```

## Deploy (Vercel)
1. Import this repo as a Vercel project. Framework preset **Other**, root directory `./`, no build command.
2. Environment variables (Production + Preview, Sensitive):
   - `SUPABASE_URL` — the Candace Supabase project URL
   - `SUPABASE_SERVICE_ROLE_KEY` — Supabase *secret* key (server-only; never in the web app)
   - `SOLVER_SECRET` — long random string shared with the Candace web app
3. Deploy, then open `/api/solve`: expect `{"ok": true, ...}` with all three `configured` flags `true`.
4. In the Candace web project set `SOLVER_URL` (`https://<this-domain>/api/solve`) and the same `SOLVER_SECRET`.

Never commit secrets: `.env*` files are ignored.
