"""Flask app — prompt-driven LinkedIn job extraction with SSE progress."""

from __future__ import annotations

import json
import queue
import threading
from flask import Flask, request, jsonify, Response, render_template, send_file
from config import FLASK_DEBUG, FLASK_PORT
from engine.prompt_parser import parse
from engine.self_refinement import run as run_pipeline
from engine.database import get_run, get_run_jobs
from engine.exporter import export_json, export_csv, export_xlsx_bytes

app = Flask(__name__)

# Active scraper progress queues: run_id → queue.Queue
_progress_queues: dict[int, queue.Queue] = {}


def _scrape_worker(run_id: int, prompt: str, max_jobs: int):
    """Background thread that runs the pipeline and pushes progress."""
    q = queue.Queue()
    _progress_queues[run_id] = q
    try:
        parsed = parse(prompt, max_jobs)
        for progress in run_pipeline(prompt, parsed, max_jobs, run_id):
            q.put(progress)
    except Exception as e:
        q.put({"error": str(e)})
    finally:
        q.put(None)  # Sentinel for completion
        _progress_queues.pop(run_id, None)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
def scrape():
    data = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    max_jobs = int(data.get("max_jobs", 50))
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    # Create run placeholder
    from engine.database import create_run
    run_id = create_run(prompt, max_jobs)

    # Start scraper in background
    threading.Thread(target=_scrape_worker, args=(run_id, prompt, max_jobs), daemon=True).start()

    return jsonify({"run_id": run_id, "status": "started"})


@app.route("/stream/<int:run_id>")
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


@app.route("/download/<int:run_id>/<fmt>")
def download(run_id: int, fmt: str):
    if fmt == "json":
        data = export_json(run_id)
        return Response(data, mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename=run_{run_id}.json"})
    elif fmt == "csv":
        data = export_csv(run_id)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=run_{run_id}.csv"})
    elif fmt == "xlsx":
        data = export_xlsx_bytes(run_id)
        return send_file(
            __import__("io").BytesIO(data),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"run_{run_id}.xlsx",
        )
    return jsonify({"error": f"unknown format: {fmt}"}), 400


@app.route("/runs/<int:run_id>")
def runs(run_id: int):
    run = get_run(run_id)
    if not run:
        return jsonify({"error": "run not found"}), 404
    jobs = get_run_jobs(run_id)
    return jsonify({"run": run, "jobs": jobs})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=FLASK_DEBUG)
