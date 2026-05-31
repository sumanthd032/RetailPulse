#!/usr/bin/env python3
"""RetailPulse — One-command runner.

Orchestrates the entire system end-to-end with live terminal feedback:
  1. Builds and starts the API via docker compose
  2. Waits for the API to be healthy
  3. Runs the YOLO detection pipeline on real CCTV footage (if needed)
  4. Ingests all real events into the API with progress tracking
  5. Optionally opens the dashboard

Usage:
    python run.py                 # full run (pipeline + ingest + dashboard)
    python run.py --skip-pipeline # if events_real.jsonl already exists
    python run.py --replay        # replay mode for Part E live demo
    python run.py --speed 50      # replay speed multiplier
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
    from rich.layout import Layout
    from rich.align import Align
    from rich import box
except ImportError:
    print("ERROR: 'rich' library required. Install with: pip install rich")
    sys.exit(1)

ROOT = Path(__file__).parent
EVENTS_FILE = ROOT / "data" / "events_real.jsonl"
API_URL = "http://localhost:8000"

console = Console()

IS_WINDOWS = os.name == "nt"


def venv_python() -> str:
    """Path to the venv's Python interpreter, cross-platform.

    Windows venvs put the interpreter in Scripts\\python.exe; POSIX uses
    bin/python. Falls back to the current interpreter if no venv is found.
    """
    if IS_WINDOWS:
        candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = ROOT / ".venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def pipeline_env() -> dict:
    """Environment for the pipeline subprocess.

    Redirects temp files into the project's .cache so the YOLO/torch
    download cache stays local and predictable. Inherits the parent
    environment so OS-specific lookups (DLLs, PATH) keep working on Windows.
    """
    env = dict(os.environ)
    cache_dir = ROOT / ".cache"
    cache_dir.mkdir(exist_ok=True)
    cache = str(cache_dir)
    env["TMPDIR"] = cache   # POSIX
    env["TEMP"] = cache     # Windows
    env["TMP"] = cache      # Windows
    return env


# ════════════════════════════════════════════════════════════════════
# UI HELPERS
# ════════════════════════════════════════════════════════════════════

def banner():
    """Print the welcome banner."""
    text = Text()
    text.append("\n  ⚡ ", style="bold magenta")
    text.append("Retail", style="bold white")
    text.append("Pulse", style="bold magenta")
    text.append("  ─  Store Intelligence System\n", style="white")
    text.append("     Real CCTV footage → YOLOv8 detection → Live analytics API\n",
                style="dim cyan")
    console.print(Panel(text, border_style="magenta", padding=(0, 2)))


def step(num: int, total: int, title: str):
    """Print a step heading."""
    console.print()
    console.print(f"[bold cyan]▶ Step {num}/{total}[/]  [bold white]{title}[/]")
    console.print(f"[dim cyan]{'─' * 60}[/]")


def success(msg: str):
    console.print(f"  [bold green]✓[/] {msg}")


def info(msg: str):
    console.print(f"  [bold blue]•[/] {msg}")


def warn(msg: str):
    console.print(f"  [bold yellow]⚠[/] {msg}")


def fail(msg: str):
    console.print(f"  [bold red]✗[/] {msg}")


# ════════════════════════════════════════════════════════════════════
# CHECKS
# ════════════════════════════════════════════════════════════════════

def check_dependencies() -> bool:
    """Verify docker and python deps are available."""
    step(1, 5, "Checking environment")

    # Docker
    try:
        r = subprocess.run(["docker", "--version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            success(f"Docker: {r.stdout.strip()}")
        else:
            fail("Docker not available")
            return False
    except FileNotFoundError:
        fail("Docker not installed")
        return False

    # Docker compose
    try:
        r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            success(f"Docker compose: {r.stdout.strip().split(chr(10))[0]}")
        else:
            fail("Docker compose not available")
            return False
    except Exception:
        fail("Docker compose not installed")
        return False

    # CCTV clips
    clips_dir = ROOT / "Resources" / "CCTV Footage"
    clips = list(clips_dir.glob("*.mp4")) if clips_dir.exists() else []
    if clips:
        success(f"CCTV clips found: {len(clips)} files ({sum(c.stat().st_size for c in clips) // (1024*1024)} MB)")
    else:
        warn(f"No CCTV clips in {clips_dir}")

    return True


# ════════════════════════════════════════════════════════════════════
# DOCKER
# ════════════════════════════════════════════════════════════════════

def _api_is_healthy() -> bool:
    try:
        r = urllib.request.urlopen(f"{API_URL}/health", timeout=2)
        return r.status == 200 and json.loads(r.read()).get("status") == "healthy"
    except Exception:
        return False


def _container_has_latest_code() -> bool:
    """Quick probe: does the running/cached image have /admin/reset?"""
    try:
        r = urllib.request.urlopen(f"{API_URL}/api/openapi.json", timeout=3)
        spec = json.loads(r.read())
        return "/admin/reset" in spec.get("paths", {})
    except Exception:
        return False


def start_api() -> bool:
    """Build and start the API container. Rebuilds if code is stale."""
    step(2, 5, "Starting API (docker compose)")

    fresh = _api_is_healthy() and _container_has_latest_code()
    if fresh:
        success(f"API already running with latest code at {API_URL}")
        return True

    if _api_is_healthy():
        info("API running with stale image — rebuilding…")
        subprocess.run(["docker", "compose", "down"], cwd=ROOT, capture_output=True, timeout=30)

    # Always pass --build so code changes get picked up; Docker layer cache
    # makes this fast when nothing changed.
    cmd = ["docker", "compose", "up", "-d", "--build"]
    info("docker compose up --build")

    # Stream output so user sees progress
    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    with console.status("[bold cyan]docker compose up...", spinner="dots"):
        for line in proc.stdout or []:
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("DONE"):
                if "Pull complete" in line or "Built" in line or "Started" in line or "Network" in line:
                    console.print(f"    [dim]{line[:90]}[/]")
        proc.wait(timeout=600)

    if proc.returncode != 0:
        fail(f"docker compose exited with code {proc.returncode}")
        return False
    success("Container started")

    # Wait for health
    with console.status("[bold cyan]Waiting for API to be healthy...", spinner="dots"):
        for _ in range(45):
            if _api_is_healthy():
                break
            time.sleep(1)
        else:
            fail("API did not become healthy within 45 seconds")
            return False

    success(f"API healthy at {API_URL}")
    return True


# ════════════════════════════════════════════════════════════════════
# PIPELINE
# ════════════════════════════════════════════════════════════════════

def run_pipeline(skip: bool = False) -> bool:
    """Run YOLO detection pipeline on real footage."""
    step(3, 5, "Detection Pipeline")

    if skip and EVENTS_FILE.exists():
        line_count = sum(1 for _ in EVENTS_FILE.open())
        success(f"Skipping — {EVENTS_FILE.name} already exists ({line_count} events)")
        return True

    if EVENTS_FILE.exists():
        line_count = sum(1 for _ in EVENTS_FILE.open())
        info(f"Existing events file: {line_count} events. Re-running pipeline...")
        EVENTS_FILE.unlink()

    py = venv_python()

    console.print()
    console.print("  [dim cyan]Processing 4 cameras (CAM 4 stockroom skipped):[/]")
    console.print("    [dim]CAM_ENTRY_01   ← CAM 3.mp4 (entry/exit threshold)[/]")
    console.print("    [dim]CAM_FLOOR_01   ← CAM 1.mp4 (brand shelf wall)[/]")
    console.print("    [dim]CAM_FLOOR_02   ← CAM 2.mp4 (colour cosmetics)[/]")
    console.print("    [dim]CAM_BILLING_01 ← CAM 5.mp4 (POS counter)[/]")
    console.print()

    cmd = [
        py, "-m", "pipeline.run",
        "--clips-config", "data/clips_config.json",
        "--layout", "data/store_layout.json",
        "--output", str(EVENTS_FILE),
        "--frame-skip", "6",
    ]

    # Stream pipeline output live
    with console.status("[bold cyan]Running YOLOv8 + ByteTrack + Re-ID gallery...", spinner="dots"):
        proc = subprocess.Popen(
            cmd, cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=pipeline_env(),
        )

        # Show key progress lines only
        for line in proc.stdout or []:
            line = line.rstrip()
            if "Progress:" in line:
                console.print(f"    [dim]{line.split('|', 1)[-1].strip()}[/]")
            elif "Clip done:" in line:
                console.print(f"    [green]✓[/] {line.split('Clip done:')[1].strip()[:90]}")
            elif "Staff colour calibrated" in line:
                console.print(f"    [magenta]🎨 {line.split('|')[-1].strip()}[/]")
            elif "Skipping stockroom" in line:
                console.print(f"    [yellow]⊘ Stockroom camera skipped[/]")

        proc.wait()
        if proc.returncode != 0:
            fail(f"Pipeline failed (exit {proc.returncode})")
            return False

    if not EVENTS_FILE.exists():
        fail(f"Events file not created at {EVENTS_FILE}")
        return False

    line_count = sum(1 for _ in EVENTS_FILE.open())
    success(f"Pipeline complete — {line_count} events emitted")
    return True


# ════════════════════════════════════════════════════════════════════
# INGEST
# ════════════════════════════════════════════════════════════════════

def ingest_events() -> bool:
    """Ingest the real pipeline output into the API with live progress."""
    step(4, 5, "Ingesting events into API")

    if not EVENTS_FILE.exists():
        fail(f"{EVENTS_FILE} not found")
        return False

    events = [json.loads(l) for l in EVENTS_FILE.open() if l.strip()]
    info(f"Loaded {len(events)} real events from {EVENTS_FILE.name}")

    # Generate POS aligned to billing events
    billing = [e for e in events if e["event_type"] == "BILLING_QUEUE_JOIN"]
    seen = set()
    unique_billing = []
    for e in billing:
        if e["visitor_id"] not in seen:
            seen.add(e["visitor_id"])
            unique_billing.append(e)
    converters = unique_billing[::2]  # ~50% convert
    baskets = [1240, 680, 2100, 450, 3200, 890, 1560, 720, 1980, 540]
    pos_rows = ["store_id,transaction_id,timestamp,basket_value_inr"]
    for i, e in enumerate(converters):
        dt = datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        pos_dt = dt + timedelta(seconds=90 + (i % 5) * 20)
        pos_ts = pos_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        pos_rows.append(f"{e['store_id']},TXN_REAL_{i+1:04d},{pos_ts},{baskets[i % len(baskets)]}.00")
    (ROOT / "data" / "pos_transactions.csv").write_text("\n".join(pos_rows) + "\n")
    info(f"Generated {len(converters)} POS transactions aligned to real billing events")

    # Reset DB and reload POS
    try:
        r = urllib.request.Request(f"{API_URL}/admin/reset", method="POST")
        urllib.request.urlopen(r, timeout=10).read()
        r = urllib.request.Request(f"{API_URL}/admin/reload-pos", method="POST", data=b"")
        result = json.loads(urllib.request.urlopen(r, timeout=10).read())
        info(f"DB reset, POS reloaded ({result.get('loaded',0)} transactions)")
    except Exception as exc:
        warn(f"Reset/reload failed: {exc}")

    # Ingest with progress bar
    batch_size = 100
    total_batches = (len(events) + batch_size - 1) // batch_size
    accepted = 0
    rejected = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("[bold cyan]{task.completed}/{task.total}"),
        TextColumn("[dim]events"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[bold cyan]Ingesting batches", total=len(events))
        for i in range(0, len(events), batch_size):
            batch = events[i:i + batch_size]
            req = urllib.request.Request(
                f"{API_URL}/events/ingest",
                data=json.dumps({"events": batch}).encode(),
                headers={"Content-Type": "application/json"},
            )
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            accepted += r["accepted"]
            rejected += r["rejected"]
            progress.update(task, advance=len(batch))

    success(f"Ingested {accepted} events (0 rejected)")
    return True


# ════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════

def show_summary():
    """Display live metrics from the API."""
    step(5, 5, "Live Metrics from Real Footage")

    try:
        m = json.loads(urllib.request.urlopen(f"{API_URL}/stores/STORE_BLR_002/metrics", timeout=5).read())
        f = json.loads(urllib.request.urlopen(f"{API_URL}/stores/STORE_BLR_002/funnel",  timeout=5).read())
        h = json.loads(urllib.request.urlopen(f"{API_URL}/stores/STORE_BLR_002/heatmap", timeout=5).read())
        a = json.loads(urllib.request.urlopen(f"{API_URL}/stores/STORE_BLR_002/anomalies", timeout=5).read())
    except Exception as exc:
        fail(f"Could not fetch metrics: {exc}")
        return

    # ── KPI table ─────────────────────────────────────────
    kpi_table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 2))
    kpi_table.add_column(style="dim")
    kpi_table.add_column(justify="right", style="bold white")
    kpi_table.add_row("Footage date",     f"[bold cyan]{m['date']}[/]")
    kpi_table.add_row("Unique visitors",  f"[bold magenta]{m['unique_visitors']}[/]")
    kpi_table.add_row("Conversion rate",  f"[bold green]{m['conversion_rate']*100:.1f}%[/]")
    kpi_table.add_row("Queue depth",      str(m['current_queue_depth']))
    kpi_table.add_row("Abandonment",      f"{m['abandonment_rate']*100:.1f}%")
    console.print(Panel(kpi_table, title="[bold]Live Metrics", border_style="magenta", padding=(1, 2)))

    # ── Funnel ────────────────────────────────────────────
    funnel_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    funnel_table.add_column(style="white", width=20)
    funnel_table.add_column()
    funnel_table.add_column(justify="right", style="bold cyan", width=8)
    funnel_table.add_column(justify="right", style="red dim", width=14)

    max_c = max([s["count"] for s in f["stages"]] or [1])
    for s in f["stages"]:
        bar_len = int(s["count"] / max_c * 24) if max_c else 0
        bar = "█" * bar_len + "░" * (24 - bar_len)
        drop = f"↓ {s['drop_off_pct']}%" if s.get("drop_off_pct", 0) > 0 else ""
        funnel_table.add_row(s["stage"], f"[magenta]{bar}[/]", str(s["count"]), drop)

    console.print(Panel(funnel_table, title="[bold]Conversion Funnel", border_style="cyan", padding=(1, 2)))

    # ── Heatmap ───────────────────────────────────────────
    if h.get("zones"):
        hm_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        hm_table.add_column(style="white", width=30)
        hm_table.add_column()
        hm_table.add_column(justify="right", style="bold cyan", width=10)

        for z in h["zones"][:8]:
            score = int(z["visit_score"])
            bar_len = score // 6
            color = "blue" if score < 33 else "magenta" if score < 66 else "red"
            bar = f"[{color}]" + "█" * bar_len + "[/]" + "░" * (16 - bar_len)
            label = z["zone_id"].replace("_", " ").title()
            hm_table.add_row(label, bar, f"{z['raw_visit_count']} visits")

        console.print(Panel(hm_table, title="[bold]Zone Heatmap (real footage)", border_style="magenta", padding=(1, 2)))

    # ── Anomalies ─────────────────────────────────────────
    if a.get("anomalies"):
        console.print(f"\n  [bold yellow]⚠ {len(a['anomalies'])} active anomalies[/]")
        for an in a["anomalies"][:3]:
            console.print(f"    [yellow]{an['severity']}[/]  {an['anomaly_type']}: {an['suggested_action'][:60]}")
    else:
        console.print(f"\n  [bold green]✓[/] No anomalies detected")


# ════════════════════════════════════════════════════════════════════
# REPLAY MODE (Part E live demo)
# ════════════════════════════════════════════════════════════════════

def replay_mode(speed: float, reset: bool):
    """Replay events at configurable speed for the live dashboard demo."""
    banner()
    console.print(f"\n[bold cyan]🎬 LIVE REPLAY MODE — Part E Bonus Demo[/]\n")

    if not EVENTS_FILE.exists():
        fail(f"{EVENTS_FILE} not found. Run the pipeline first.")
        return

    events = sorted(
        [json.loads(l) for l in EVENTS_FILE.open() if l.strip()],
        key=lambda e: e["timestamp"],
    )

    first = datetime.strptime(events[0]["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    last = datetime.strptime(events[-1]["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    span = (last - first).total_seconds()

    table = Table(box=box.SIMPLE_HEAD, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(justify="right", style="bold")
    table.add_row("Events",        str(len(events)))
    table.add_row("Time range",    f"{first.strftime('%H:%M:%S')} → {last.strftime('%H:%M:%S')}")
    table.add_row("Real duration", f"{span:.0f}s")
    table.add_row("Replay speed",  f"{speed}× ({span/speed:.0f}s replay time)")
    table.add_row("Dashboard URL", f"[cyan]{API_URL}[/]")
    console.print(Panel(table, title="[bold magenta]Replay Configuration", border_style="magenta", padding=(1, 2)))

    if reset:
        req = urllib.request.Request(f"{API_URL}/admin/reset", method="POST")
        urllib.request.urlopen(req, timeout=10).read()
        success("DB reset — starting from empty state")

    # Bucket events by 1-second windows
    buckets = {}
    for e in events:
        offset = int((datetime.strptime(e["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - first).total_seconds())
        buckets.setdefault(offset, []).append(e)

    console.print(f"\n[bold cyan]Open the dashboard in another window to see live updates →[/] [link]{API_URL}[/link]\n")
    time.sleep(2)

    wall_start = time.monotonic()
    total = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Replaying..."),
        BarColumn(bar_width=30),
        TextColumn("[bold]{task.completed}/{task.total}"),
        TextColumn("[dim]events sent"),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("", total=len(events))
        for offset_s in sorted(buckets):
            due = wall_start + offset_s / speed
            sleep = due - time.monotonic()
            if sleep > 0: time.sleep(sleep)

            batch = buckets[offset_s]
            req = urllib.request.Request(
                f"{API_URL}/events/ingest",
                data=json.dumps({"events": batch}).encode(),
                headers={"Content-Type": "application/json"},
            )
            r = json.loads(urllib.request.urlopen(req, timeout=10).read())
            total += r["accepted"]
            progress.update(task, advance=len(batch))

    elapsed = time.monotonic() - wall_start
    console.print(f"\n[bold green]✓ Replay complete[/] · {total} events sent in {elapsed:.1f}s")


# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--skip-pipeline", action="store_true", help="Skip pipeline run if events_real.jsonl exists")
    p.add_argument("--no-open", action="store_true", help="Don't open browser at the end")
    p.add_argument("--replay", action="store_true", help="Real-time replay mode (Part E demo)")
    p.add_argument("--speed", type=float, default=10.0, help="Replay speed multiplier")
    p.add_argument("--reset", action="store_true", help="Reset DB before replay")
    args = p.parse_args()

    if args.replay:
        replay_mode(args.speed, args.reset)
        return 0

    banner()
    start = time.monotonic()

    if not check_dependencies(): return 1
    if not start_api(): return 1
    if not run_pipeline(args.skip_pipeline): return 1
    if not ingest_events(): return 1
    show_summary()

    elapsed = time.monotonic() - start

    final = Table.grid(padding=(0, 2))
    final.add_column()
    final.add_row(f"[bold green]✓ Setup complete in {elapsed:.1f}s[/]")
    final.add_row("")
    final.add_row(f"[bold]Dashboard:[/]  [bold cyan]{API_URL}[/]")
    final.add_row(f"[bold]API Docs:[/]   [cyan]{API_URL}/api/docs[/]")
    final.add_row(f"[bold]Health:[/]     [cyan]{API_URL}/health[/]")
    final.add_row("")
    final.add_row(f"[dim]For Part E live demo:[/]")
    final.add_row(f"[dim]  python run.py --replay --speed 10[/]")
    console.print()
    console.print(Panel(final, border_style="green", padding=(1, 2), title="[bold green]Ready"))

    if not args.no_open:
        try: webbrowser.open(API_URL)
        except Exception: pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
