"""Terminal entry point for the LinkedIn extraction engine.

Runs the SAME pipeline as the web app (prompt → search → score → persist →
export) entirely headless, with live progress printed to the terminal — so a
scrape can be launched and watched over SSH on a server, with no GUI and no web
dashboard required. The Flask web app (app.py) remains available and unchanged.

Usage:
    python cli.py "Extract NGO sector junior accountant roles in India" --max 100
    python cli.py "Remote python developers" -n 50 --format xlsx,csv,json

The browser is always headless here regardless of PLAYWRIGHT_HEADLESS, since a
terminal/server run must never try to open a window.
"""

from __future__ import annotations

import argparse
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli.py",
        description="Headless, terminal-driven LinkedIn job extraction.",
    )
    p.add_argument("prompt", help="Plain-English extraction request.")
    p.add_argument("-n", "--max", "--max-jobs", dest="max_jobs", type=int, default=50,
                   help="Target number of jobs to collect (default: 50).")
    p.add_argument("-f", "--format", dest="formats", default="xlsx,csv,json",
                   help="Comma-separated export formats: xlsx,csv,json (default: all).")
    p.add_argument("--headed", action="store_true",
                   help="Open a visible browser window (local debugging only; "
                        "do NOT use on a headless server).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    prompt = args.prompt.strip()
    if not prompt:
        print("error: prompt is required", file=sys.stderr)
        return 2

    # Force headless before importing config/engine (config reads env at import).
    # --headed is an explicit local-only override.
    os.environ["PLAYWRIGHT_HEADLESS"] = "false" if args.headed else "true"

    # Imported after the env is set so config picks up the headless choice.
    from engine.prompt_parser import parse
    from engine.self_refinement import run as run_pipeline
    from engine.exporter import export_to_files
    from config import DRY_RUN

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    valid = [f for f in formats if f in {"xlsx", "csv", "json"}]
    if not valid:
        print(f"error: no valid export formats in {args.formats!r} "
              f"(choose from xlsx,csv,json)", file=sys.stderr)
        return 2

    mode = "DRY-RUN (mock data, no network)" if DRY_RUN else "LIVE"
    print(f"[cli] mode={mode} headless={not args.headed} "
          f"target={args.max_jobs} formats={','.join(valid)}", flush=True)
    print(f"[cli] prompt: {prompt!r}", flush=True)

    # Run the full pipeline. self_refinement.run() is a generator that yields
    # Progress and prints its own live [scrape] log lines; we just drain it.
    parsed = parse(prompt, args.max_jobs)
    gen = run_pipeline(prompt, parsed, args.max_jobs)
    run_id = None
    jobs: list = []
    try:
        while True:
            progress = next(gen)
            run_id = progress.run_id
    except StopIteration as stop:
        jobs = stop.value or []
    except KeyboardInterrupt:
        print("\n[cli] interrupted by user — partial results kept in DB.", file=sys.stderr)
    except Exception as e:
        print(f"[cli] pipeline error: {e}", file=sys.stderr)

    if run_id is None:
        print("[cli] no run was created; nothing to export.", file=sys.stderr)
        return 1

    paths = export_to_files(run_id, valid)
    print(f"[cli] collected {len(jobs)} job(s). Exports written:", flush=True)
    for path in paths:
        print(f"  - {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
