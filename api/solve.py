"""Candace solver service — Vercel Python function at /api/solve.

GET  /api/solve  -> health check: {"ok": true, ...}. Public, solves nothing.
POST /api/solve  -> solve one campus-day. Requires header `x-solver-secret`.
                    Body: {"campus_id": "...", "date": "YYYY-MM-DD"}
                    (Reading from Supabase and saving the draft arrive in the next step;
                    until then POST answers 501.)

Secrets (Vercel env vars, never in code): SOLVER_SECRET, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY.
"""
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make the `solver` package importable


def _json(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class handler(BaseHTTPRequestHandler):  # Vercel looks for a class named `handler`
    def do_GET(self):
        try:
            import ortools  # noqa: F401  (proves the solver library installed on Vercel)
            from solver import solve as _  # noqa: F401
            ok, version = True, ortools.__version__
        except Exception as e:  # pragma: no cover
            ok, version = False, f"import failed: {type(e).__name__}"
        _json(self, 200 if ok else 500, {
            "ok": ok,
            "service": "candace-solver",
            "ortools": version,
            "configured": {
                "SOLVER_SECRET": bool(os.environ.get("SOLVER_SECRET")),
                "SUPABASE_URL": bool(os.environ.get("SUPABASE_URL")),
                "SUPABASE_SERVICE_ROLE_KEY": bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY")),
            },
        })

    def do_POST(self):
        secret = os.environ.get("SOLVER_SECRET", "")
        given = self.headers.get("x-solver-secret", "")
        if not secret or not hmac.compare_digest(secret, given):
            return _json(self, 401, {"ok": False, "error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return _json(self, 400, {"ok": False, "error": "bad json"})
        if not body.get("campus_id") or not body.get("date"):
            return _json(self, 400, {"ok": False, "error": "campus_id and date are required"})
        return _json(self, 501, {"ok": False, "error": "solving from Supabase is not wired yet"})
