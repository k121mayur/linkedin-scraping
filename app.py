"""Flask app — prompt-driven LinkedIn job & grant extraction with SSE progress.

Auth model: a hardcoded admin (config ADMIN_EMAIL/ADMIN_PASSWORD) plus regular
users stored in SQLite, managed by the admin from the web UI. Every page and
API except /login and /health requires a session.
"""

from __future__ import annotations

import json
import queue
import threading
from functools import wraps

from flask import (
    Flask, request, jsonify, Response, render_template, send_file,
    session, redirect, url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash

from config import (
    FLASK_DEBUG, FLASK_PORT, SECRET_KEY,
    ADMIN_NAME, ADMIN_EMAIL, ADMIN_PASSWORD,
)
from engine.prompt_parser import parse
from engine.self_refinement import run as run_jobs_pipeline
from engine.grants_pipeline import run as run_grants_pipeline
from engine import database as db
from engine.database import get_run, get_run_jobs
from engine.exporter import export_json, export_csv, export_xlsx_bytes
from engine.grants_exporter import (
    export_grants_json, export_grants_csv, export_grants_xlsx_bytes,
)
from engine.mailer import send_welcome_email, mail_configured

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Active scraper progress queues: run_id → queue.Queue
_progress_queues: dict[int, queue.Queue] = {}
# Cooperative stop signals: run_id → threading.Event (set() → pipeline halts)
_stop_events: dict[int, threading.Event] = {}


# ── auth helpers ─────────────────────────────────────────────

def _current_user():
    return session.get("user")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _current_user() is None:
            if request.accept_mimetypes.best == "application/json" or request.path.startswith(
                ("/scrape", "/stream", "/stop", "/runs", "/admin")
            ):
                return jsonify({"error": "authentication required"}), 401
            return redirect(url_for("login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _current_user()
        if user is None:
            return jsonify({"error": "authentication required"}), 401
        if user.get("role") != "admin":
            return jsonify({"error": "admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── auth routes ──────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if _current_user():
            return redirect(url_for("index"))
        return render_template("login.html")

    data = request.get_json(silent=True) or request.form
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400

    # Hardcoded admin first, then the users table.
    if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
        session["user"] = {"name": ADMIN_NAME, "email": email, "role": "admin"}
        return jsonify({"ok": True, "role": "admin"})

    user = db.get_user_by_email(email)
    if user and check_password_hash(user["password_hash"], password):
        session["user"] = {"name": user["name"], "email": user["email"], "role": "user"}
        return jsonify({"ok": True, "role": "user"})

    return jsonify({"error": "invalid email or password"}), 401


@app.route("/logout", methods=["POST", "GET"])
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


# ── admin: user management ───────────────────────────────────

@app.route("/admin/users", methods=["GET"])
@admin_required
def admin_list_users():
    return jsonify({"users": db.list_users(), "mail_configured": mail_configured()})


@app.route("/admin/users", methods=["POST"])
@admin_required
def admin_add_user():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not name or not email or not password:
        return jsonify({"error": "name, email and password are all required"}), 400
    if "@" not in email or "." not in email.split("@")[-1]:
        return jsonify({"error": "invalid email address"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400
    if email == ADMIN_EMAIL.lower() or db.get_user_by_email(email):
        return jsonify({"error": "a user with this email already exists"}), 409

    user_id = db.add_user(name, email, generate_password_hash(password))
    emailed = send_welcome_email(name, email, password)
    return jsonify({
        "ok": True,
        "user": {"id": user_id, "name": name, "email": email},
        "emailed": emailed,
    })


@app.route("/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id: int):
    if db.delete_user(user_id):
        return jsonify({"ok": True})
    return jsonify({"error": "user not found"}), 404


# ── scraping ─────────────────────────────────────────────────

def _scrape_worker(run_id: int, prompt: str, max_items: int, mode: str, profile: str = ""):
    """Background thread that runs the selected pipeline and pushes progress."""
    q = queue.Queue()
    _progress_queues[run_id] = q
    stop_event = threading.Event()
    _stop_events[run_id] = stop_event
    try:
        if mode == "grants":
            pipeline = run_grants_pipeline(prompt, max_items, run_id,
                                           should_stop=stop_event.is_set,
                                           profile=profile)
        else:
            parsed = parse(prompt, max_items)
            pipeline = run_jobs_pipeline(prompt, parsed, max_items, run_id,
                                         should_stop=stop_event.is_set)
        for progress in pipeline:
            q.put(progress)
    except Exception as e:
        q.put({"error": str(e)})
    finally:
        q.put(None)  # Sentinel for completion
        _progress_queues.pop(run_id, None)
        _stop_events.pop(run_id, None)


@app.route("/")
@login_required
def index():
    user = _current_user()
    return render_template("index.html", user=user,
                           is_admin=user.get("role") == "admin")


@app.route("/scrape", methods=["POST"])
@login_required
def scrape():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    max_items = int(data.get("max_jobs", 50))
    mode = data.get("mode", "jobs")
    # Organisation profile — optional, grants mode only. Plain text / markdown.
    profile = (data.get("profile") or "").strip()
    if mode not in ("jobs", "grants"):
        return jsonify({"error": f"unknown mode: {mode}"}), 400
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    run_id = db.create_run(prompt, max_items, run_type=mode)
    threading.Thread(target=_scrape_worker, args=(run_id, prompt, max_items, mode, profile),
                     daemon=True).start()
    return jsonify({"run_id": run_id, "status": "started", "mode": mode})


@app.route("/stop/<int:run_id>", methods=["POST"])
@login_required
def stop(run_id: int):
    """Request a cooperative stop of a running scrape.

    The pipeline halts before the next pass/item; everything collected so far is
    already persisted and immediately downloadable via /download/<run_id>/<fmt>.
    """
    ev = _stop_events.get(run_id)
    if ev is not None:
        ev.set()
        return jsonify({"run_id": run_id, "status": "stopping"})
    return jsonify({"run_id": run_id, "status": "not_running"})


@app.route("/stream/<int:run_id>")
@login_required
def stream(run_id: int):
    """SSE endpoint for live progress on a scrape."""
    def generate():
        q = _progress_queues.get(run_id)
        if q is None:
            yield f"data: {json.dumps({'error': 'run not found or already finished'})}\n\n"
            return
        while True:
            try:
                item = q.get(timeout=300)
            except queue.Empty:
                yield f"data: {json.dumps({'error': 'timeout waiting for progress'})}\n\n"
                return
            if item is None:
                yield f"data: {json.dumps({'status': 'done'})}\n\n"
                return
            if isinstance(item, dict) and "error" in item:
                yield f"data: {json.dumps(item)}\n\n"
                return
            # Progress dataclass → dict
            yield f"data: {json.dumps(item.__dict__ if hasattr(item, '__dict__') else item)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _download_stem(run_id: int) -> str:
    """Return a filesystem-safe name derived from the run's prompt, e.g. 'full_stack_developer'."""
    import re
    run = get_run(run_id)
    if run and run.get("prompt"):
        slug = run["prompt"].strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", slug)   # non-alphanum → underscore
        slug = slug.strip("_")[:60]                 # trim and cap length
        if slug:
            return slug
    return f"run_{run_id}"


@app.route("/download/<int:run_id>/<fmt>")
@login_required
def download(run_id: int, fmt: str):
    stem = _download_stem(run_id)
    run = get_run(run_id)
    is_grants = bool(run and run.get("run_type") == "grants")

    if fmt == "json":
        data = export_grants_json(run_id) if is_grants else export_json(run_id)
        return Response(data, mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename={stem}.json"})
    elif fmt == "csv":
        data = export_grants_csv(run_id) if is_grants else export_csv(run_id)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={stem}.csv"})
    elif fmt == "xlsx":
        data = export_grants_xlsx_bytes(run_id) if is_grants else export_xlsx_bytes(run_id)
        return send_file(
            __import__("io").BytesIO(data),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"{stem}.xlsx",
        )
    return jsonify({"error": f"unknown format: {fmt}"}), 400


@app.route("/runs/<int:run_id>")
@login_required
def runs(run_id: int):
    run = get_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    if run.get("run_type") == "grants":
        return jsonify({"run": run, "grants": db.get_run_grants(run_id)})
    jobs = get_run_jobs(run_id)
    return jsonify({"run": run, "jobs": jobs})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG)

