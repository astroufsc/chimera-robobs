# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2014-present chimera-robobs authors

"""``chimera-robobs status``: the current state of the night, rendered in
the style of chimera-ctl.

A thin client of ``RobObs.status()``: one bus call returns the whole
snapshot - robobs/scheduler state, tonight's queue, pending timed
occurrences and the observing-log tail - and this module only renders it.  No sqlite file is touched, so the command works
from any machine that reaches the chimera server.  ``--json`` dumps the raw
snapshot for scripts, ``--watch`` redraws the panel in place.
"""

import datetime as dt
import json
import time

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from chimera_robobs.scheduling.dates import datetime_from_mjd

#: queue-state display styles
_STATE_STYLES = {
    "running": "bold green",
    "handed": "cyan",
    "queued": "",
    "done": "dim",
}


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = abs(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _fmt_relative(now_mjd: float, then_mjd: float) -> str:
    delta = (then_mjd - now_mjd) * 86400.0
    if abs(delta) < 1:
        return "now"
    if delta > 0:
        return f"in {_fmt_duration(delta)}"
    return f"{_fmt_duration(delta)} ago"


def _fmt_mjd(mjd: float | None, fmt: str = "%m-%d %H:%M:%S") -> str:
    """Times are shown as UT; the raw MJD stays in --json.  0/None is the
    "no constraint" sentinel, not a date in 1858."""
    if not mjd:
        return "-"
    return datetime_from_mjd(mjd).strftime(fmt)


# ----------------------------------------------------------------------
# rendering (chimera-ctl look: rules, plain grids, box.SIMPLE tables)
# ----------------------------------------------------------------------


def _table(title: str, *columns: str) -> Table:
    table = Table(
        title=title,
        title_justify="left",
        title_style="bold",
        box=box.SIMPLE,
        pad_edge=False,
    )
    for column in columns:
        table.add_column(column)
    return table


def _table_or_empty(table: Table, empty_message: str):
    if table.row_count:
        return table
    return Group(Text(str(table.title), style="bold"), Text(empty_message, style="dim"))


def _grid() -> Table:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column()
    return grid


def _render_robobs(snapshot: dict, server: str) -> list:
    robobs = snapshot.get("robobs", {})
    sched = snapshot.get("scheduler", {})

    grid = _grid()
    grid.add_row("server", server)

    state = robobs.get("state")
    state_markup = (
        f"[green]{state}[/green]" if state == "ON" else f"[yellow]{state}[/yellow]"
    )
    machine = robobs.get("machine")
    events = "" if robobs.get("events_connected", True) else " [red](no events!)[/red]"
    grid.add_row("robobs", f"{state_markup}  [dim]machine[/dim] {machine}{events}")

    if "consecutive_errors" in robobs:
        errors = robobs["consecutive_errors"]
        limit = robobs.get("max_consecutive_errors")
        style = "red" if errors else "dim"
        grid.add_row("errors", f"[{style}]{errors}/{limit} consecutive[/{style}]")
    if robobs.get("no_program_on_queue"):
        grid.add_row("queue", "[yellow]robobs found nothing to observe[/yellow]")
    if robobs.get("database"):
        grid.add_row("database", str(robobs["database"]))

    if sched.get("error"):
        grid.add_row("scheduler", f"[red]{sched['error']}[/red]")
    elif sched:
        queue_len = sched.get("queue_len")
        pending = "" if queue_len is None else f", {queue_len} queued"
        grid.add_row(
            "scheduler",
            f"{sched.get('state', '-')}{pending} [dim]({sched.get('location')})[/dim]",
        )
    program = sched.get("current_program")
    if program:
        grid.add_row(
            "program",
            f"[bold green]{program.get('name')}[/bold green] "
            f"[dim]#{program.get('id')} pi:{program.get('pi')}[/dim]",
        )
    action = sched.get("current_action")
    if action:
        grid.add_row(
            "action",
            f"{action.get('description')} [dim]({action.get('type')})[/dim]",
        )
    for section, message in (snapshot.get("errors") or {}).items():
        grid.add_row(section, f"[red]{message}[/red]")
    return [Rule("[bold]RobObs[/bold]"), grid, Text()]


def _render_night(snapshot: dict) -> list:
    night = snapshot.get("night")
    if not night:
        return []
    now = night["now"]
    grid = _grid()
    grid.add_row(
        "time",
        f"{_fmt_mjd(now, '%Y-%m-%d %H:%M:%S')} UT  "
        f"[dim]MJD {now:.5f}, LST[/dim] {night['lst_hours']:.2f} h",
    )
    grid.add_row(
        "sun",
        f"{night['sun_altitude']:+.1f} deg  "
        f"[dim]moon[/dim] {night['moon_phase'] * 100:.0f}%",
    )
    label = "" if night["is_night"] else " [yellow](daytime: next night)[/yellow]"
    grid.add_row(
        "dusk",
        f"{_fmt_mjd(night['dusk'], '%Y-%m-%d %H:%M')} UT  "
        f"({_fmt_relative(now, night['dusk'])}){label}",
    )
    grid.add_row(
        "dawn",
        f"{_fmt_mjd(night['dawn'], '%Y-%m-%d %H:%M')} UT  "
        f"({_fmt_relative(now, night['dawn'])})",
    )
    return [Rule("[bold]Night[/bold]"), grid, Text()]


def _render_queue(snapshot: dict) -> list:
    programs = snapshot.get("programs", [])
    night = snapshot.get("night") or {}
    now = night.get("now")

    counts = {}
    for entry in programs:
        counts[entry["state"]] = counts.get(entry["state"], 0) + 1
    summary = ", ".join(
        f"{counts[state]} {state}"
        for state in ("running", "handed", "queued", "done")
        if state in counts
    )
    title = f"Queue ({len(programs)}{': ' + summary if summary else ''})"
    table = _table(
        title, "slew (UT)", "pid", "target", "prio", "algorithm", "length", "state"
    )
    for entry in programs:
        slew = _fmt_mjd(entry["slew_at"])
        if (
            entry["state"] == "queued"
            and entry["slew_at"]
            and now is not None
            and entry["slew_at"] < now
        ):
            slew = f"[yellow]{slew} (overdue)[/yellow]"
        style = _STATE_STYLES.get(entry["state"], "")
        state = f"[{style}]{entry['state']}[/{style}]" if style else entry["state"]
        table.add_row(
            slew,
            str(entry["pid"] or "-"),
            str(entry["name"] or "-"),
            str(entry["priority"]),
            entry["algorithm"] or "-",
            _fmt_duration(entry["length"]) if entry["length"] else "-",
            state,
        )
    parts = [
        Rule("[bold]Tonight[/bold]"),
        _table_or_empty(table, "no programs in tonight's queue"),
    ]

    occurrences = snapshot.get("occurrences") or {}
    if occurrences:
        pieces = []
        for pid in sorted(occurrences):
            entry = occurrences[pid]
            piece = f"{pid} {entry['pending']} pending"
            if entry.get("committed"):
                piece += f" +{entry['committed']} committed"
            if entry.get("next_execute_at"):
                piece += f" (next {_fmt_mjd(entry['next_execute_at'], '%H:%M')} UT)"
            pieces.append(piece)
        parts.append(Text())
        parts.append(
            Text.from_markup(f"[dim]timed occurrences:[/dim] {' | '.join(pieces)}")
        )
    parts.append(Text())
    return parts


def _render_log(snapshot: dict) -> list:
    log = snapshot.get("log", [])
    table = _table(f"Observing log (last {len(log)})", "time (UT)", "target", "action")
    for entry in log:
        when = dt.datetime.fromisoformat(entry["time_utc"])
        action = entry["action"]
        style = "red" if "ERROR" in action or "ABORTED" in action else ""
        table.add_row(
            f"{when:%m-%d %H:%M:%S}",
            str(entry["name"]),
            f"[{style}]{action}[/{style}]" if style else action,
        )
    return [_table_or_empty(table, "no log entries"), Text()]


def render_status(snapshot: dict, server: str) -> Group:
    parts = []
    parts += _render_robobs(snapshot, server)
    parts += _render_night(snapshot)
    parts += _render_queue(snapshot)
    parts += _render_log(snapshot)
    return Group(*parts)


# ----------------------------------------------------------------------
# command
# ----------------------------------------------------------------------


def _fetch(proxy) -> dict:
    """The controller's snapshot; a minimal one on cores whose RobObs
    predates ``status()``."""
    try:
        return proxy.status()
    except Exception:
        text = str(proxy.state())  # "robstate=ON machine=BUSY"
        fields = dict(part.split("=", 1) for part in text.split() if "=" in part)
        return {
            "schema": 0,
            "time_utc": dt.datetime.now(dt.UTC).isoformat(),
            "robobs": {
                "state": fields.get("robstate"),
                "machine": fields.get("machine"),
            },
            "scheduler": {},
            "errors": {"status": "controller has no status(); showing state() only"},
        }


def cmd_status(args) -> int:
    """Show the current state of the night (chimera-ctl style panel)."""
    # imported here: robobs.py imports this module at load time
    from chimera_robobs.cli.robobs import _connect, _err

    server = f"{args.host}:{args.port}{args.robobs}"
    console = Console()
    bus = None
    try:
        try:
            bus, proxy = _connect(args, args.robobs)
        except Exception as e:
            _err(f"error: could not talk to robobs at {server}: {e}")
            return 1

        if args.json:
            print(json.dumps(_fetch(proxy), indent=2))
            return 0

        if args.watch is None:
            console.print(render_status(_fetch(proxy), server))
            return 0

        # --watch: redraw in place until interrupted; a fetch failure is
        # shown and retried, so a chimera restart comes back by itself
        with Live(console=console, auto_refresh=False, screen=True) as live:
            while True:
                try:
                    renderable = render_status(_fetch(proxy), server)
                except Exception as e:
                    renderable = Text(f"lost the server: {e} (retrying)", style="red")
                live.update(renderable, refresh=True)
                time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    finally:
        if bus is not None:
            bus.shutdown()
