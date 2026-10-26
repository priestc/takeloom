"""What a release (or daily build) is doing: which package is building right
now, and how long each one took or has taken so far, next to how long it
usually takes.

Read from GitHub Actions' own job records through the GitHub CLI (`gh`),
so it works for any run, finished or not. Used by `takeloom release-status`
and `make-release --watch`, and by the workflows' final "summary" job, which
writes the table onto the run's page (`--markdown`). Standard library only,
so the workflows can run it straight from a checkout:

    python3 -m takeloom.release_status --run RUN_ID --markdown

"Usually" is the median of the same job in the workflow's last few
successful runs. See docs/releasing.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import statistics
import subprocess
import sys
import time

REPO = "priestc/takeloom"
HISTORY_RUNS = 5

# Job name prefix -> package. Reusable workflows' jobs are named
# "<caller job> / <job> (<matrix values>)", e.g. "deb / build (amd64, ubuntu-24.04)".
_PACKAGES = {
    "release": "GitHub release",
    "daily": "GitHub pre-release",
    "pypi": "PyPI",
    "homebrew": "Homebrew",
    "deb": ".deb",
    "flatpak": "Flatpak",
    "site": "Package repositories",
}
# A reusable workflow's job -> how it reads after the package name.
_STEPS = {
    "build": "",
    "test": "test",
    "attach": "attach to release",
    "deploy": "publish",
    "test-apt": "install test (apt)",
    "test-flatpak": "install test (Flatpak)",
}
_SKIP = {"summary"}  # the job that writes this table


class StatusError(Exception):
    pass


def _gh_json(path: str) -> dict:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        raise StatusError(f"gh api {path} failed: {result.stderr.strip() or 'is gh installed and logged in?'}")
    return json.loads(result.stdout)


def _ts(value: str | None) -> float | None:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def _label(job_name: str) -> str:
    """"deb / test (amd64, ubuntu-24.04, debian:12)" -> ".deb test amd64, debian:12"."""
    head, _, rest = job_name.partition(" / ")
    package = _PACKAGES.get(head, head)
    if not rest:
        return package
    step, _, matrix = rest.partition(" (")
    parts = [p.strip() for p in matrix.rstrip(")").split(",") if p.strip() and not p.strip().startswith("ubuntu-")]
    label = f"{package} {_STEPS.get(step, step)}".strip()
    return f"{label} {', '.join(parts)}".strip() if parts else label


def latest_run(workflow: str) -> dict:
    runs = _gh_json(f"repos/{REPO}/actions/workflows/{workflow}/runs?per_page=1")["workflow_runs"]
    if not runs:
        raise StatusError(f"No {workflow} runs yet.")
    return runs[0]


def run_info(run_id: int) -> dict:
    return _gh_json(f"repos/{REPO}/actions/runs/{run_id}")


def jobs(run_id: int) -> list[dict]:
    """The run's jobs, each at its latest attempt (a re-run job replaces
    its failed try)."""
    data = _gh_json(f"repos/{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    return [j for j in data["jobs"] if j["name"] not in _SKIP]


def usual_durations(workflow: str, exclude_run: int) -> dict[str, float]:
    """job name -> median seconds over the workflow's last few successful
    runs (other than exclude_run)."""
    runs = _gh_json(f"repos/{REPO}/actions/workflows/{workflow}/runs?status=success&per_page={HISTORY_RUNS + 1}")
    samples: dict[str, list[float]] = {}
    for run in runs["workflow_runs"]:
        if run["id"] == exclude_run:
            continue
        for job in jobs(run["id"]):
            start, end = _ts(job.get("started_at")), _ts(job.get("completed_at"))
            if job.get("conclusion") == "success" and start and end:
                samples.setdefault(job["name"], []).append(end - start)
    return {name: statistics.median(values) for name, values in samples.items()}


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def rows(run_jobs: list[dict], usual: dict[str, float], now: float | None = None) -> list[dict]:
    """One row per job: label, state ("done"/"building"/"waiting"/"failed"/
    "skipped"/"cancelled"), seconds taken (so far), usual seconds."""
    now = now or time.time()
    out = []
    for job in run_jobs:
        start, end = _ts(job.get("started_at")), _ts(job.get("completed_at"))
        if job["status"] == "completed":
            state = {"success": "done", "failure": "failed", "skipped": "skipped",
                     "cancelled": "cancelled"}.get(job.get("conclusion"), job.get("conclusion") or "done")
            taken = (end - start) if (start and end and state != "skipped") else None
        elif job["status"] == "in_progress":
            state, taken = "building", (now - start) if start else None
        else:
            state, taken = "waiting", None
        out.append({"label": _label(job["name"]), "state": state, "seconds": taken,
                    "usual": usual.get(job["name"]), "name": job["name"], "start": start, "end": end})
    # Pipeline order: by when each started, the not-yet-started last.
    out.sort(key=lambda r: (r["start"] is None, r["start"] or 0, r["label"]))
    return out


_ICON = {"done": "✓", "building": "▶", "waiting": "·", "failed": "✗", "skipped": "-", "cancelled": "✗"}


def render_text(run: dict, table: list[dict]) -> str:
    # From the first job's start to the last one's end, not the run's own
    # timestamps (a re-run moves those to the re-run).
    finished = run.get("status") == "completed"
    starts = [r["start"] for r in table if r["start"]] or [_ts(run.get("created_at"))]
    ends = [r["end"] for r in table if r["end"]]
    started, end = min(starts), (max(ends) if finished and ends else time.time())
    headline = f"{run['name']} #{run['run_number']} ({run['head_sha'][:7]}): " + (
        f"{run.get('conclusion')}, took {_fmt(end - started)}" if finished else f"in progress, {_fmt(end - started)} so far"
    )
    width = max((len(r["label"]) for r in table), default=10)
    lines = [headline, run["html_url"], ""]
    for r in table:
        timing = _fmt(r["seconds"])
        if r["state"] == "building":
            timing += " so far"
        usual = f"(usually {_fmt(r['usual'])})" if r["usual"] and r["state"] != "skipped" else ""
        lines.append(f"  {_ICON.get(r['state'], '?')} {r['label']:<{width}}  {r['state']:<9} {timing:<14} {usual}".rstrip())
    building = [r["label"] for r in table if r["state"] == "building"]
    lines += ["", "Building now: " + (", ".join(building) if building else "nothing" if finished else "(between jobs)")]
    return "\n".join(lines)


def render_markdown(run: dict, table: list[dict]) -> str:
    lines = ["### Package build times", "", "| Package | Result | Took | Usually |", "|---|---|---|---|"]
    for r in table:
        lines.append(f"| {r['label']} | {_ICON.get(r['state'], '')} {r['state']} | {_fmt(r['seconds'])} | {_fmt(r['usual'])} |")
    return "\n".join(lines) + "\n"


def status(run_id: int | None = None, workflow: str = "release.yml") -> tuple[dict, list[dict]]:
    run = run_info(run_id) if run_id else latest_run(workflow)
    workflow_file = run.get("path", f".github/workflows/{workflow}").rsplit("/", 1)[-1]
    usual = usual_durations(workflow_file, exclude_run=run["id"])
    return run, rows(jobs(run["id"]), usual)


def watch(run_id: int | None = None, workflow: str = "release.yml", interval: float = 15.0, out=print) -> str:
    """Re-print the status whenever it changes until the run finishes.
    Returns the run's conclusion."""
    run = run_info(run_id) if run_id else latest_run(workflow)
    workflow_file = run.get("path", "").rsplit("/", 1)[-1] or workflow
    usual = usual_durations(workflow_file, exclude_run=run["id"])
    last_states = None
    while True:
        run = run_info(run["id"])
        table = rows(jobs(run["id"]), usual)
        states = [(r["name"], r["state"]) for r in table]
        if states != last_states or run["status"] == "completed":
            out(render_text(run, table) + "\n")
            last_states = states
        if run["status"] == "completed":
            return run.get("conclusion") or "unknown"
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", type=int, help="run id (default: the workflow's latest run)")
    parser.add_argument("--workflow", default="release.yml")
    parser.add_argument("--markdown", action="store_true", help="a table for the run's summary page")
    args = parser.parse_args(argv)
    try:
        run, table = status(args.run, args.workflow)
    except StatusError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(render_markdown(run, table) if args.markdown else render_text(run, table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
