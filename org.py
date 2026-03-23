import os
import re
import hashlib
from datetime import datetime, date, timedelta, time as dtime
from dataclasses import dataclass, field
from typing import Optional

import orgparse
from flask import Blueprint, render_template

# How many days before a deadline org-agenda starts warning about it
DEADLINE_WARNING_DAYS = 14
TERMINAL_TODO_STATES = {
    "DONE",
    "CANCELLED",
    "CANCELED",
    "WONTDO",
    "OBE",
    "DUEPASSED",
}


def _normalize_todo(todo: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", todo.upper())

# ---------------------------------------------------------------------------
# Timestamp regexes
# ---------------------------------------------------------------------------

_STATE_CHANGE_RE = re.compile(
    r'- State\s+"(\w+)"\s+from\s+"(\w+)"\s+\[(\d{4}-\d{2}-\d{2} \w+ \d{2}:\d{2})\]'
)
_ACTIVE_TS_RE = re.compile(
    r'<(\d{4}-\d{2}-\d{2})[^>]*>'
)
_ACTIVE_TS_WITH_TIME_RE = re.compile(
    r'<(\d{4}-\d{2}-\d{2})(?:\s+\w{3})?\s+(\d{1,2}:\d{2})(?:-(\d{1,2}:\d{2}))?[^>]*>'
)
_HEADING_TODO_RE = re.compile(r'^(?:\[#.\]\s+)?([A-Z][A-Z0-9-]*)(?:\s+|$)')


def _extract_todo_from_heading(heading: str) -> tuple[Optional[str], str]:
    """
    Best-effort fallback for custom org TODO keywords when orgparse doesn't parse them.
    Returns (todo_or_none, cleaned_heading).
    """
    stripped = heading.strip()
    match = _HEADING_TODO_RE.match(stripped)
    if not match:
        return None, heading

    candidate = match.group(1)
    normalized = _normalize_todo(candidate)

    known_states = {
        "TODO",
        "NEXT",
        "WAITING",
        "STARTED",
        "INPROGRESS",
    } | TERMINAL_TODO_STATES

    if normalized not in known_states:
        return None, heading

    cleaned = stripped[len(candidate):].lstrip()
    return candidate, (cleaned if cleaned else heading)

@dataclass
class OrgTask:
    heading: str
    todo: Optional[str]                # current TODO keyword, or None for plain entries
    tags: frozenset
    scheduled: Optional[date]
    deadline: Optional[date]
    properties: dict
    state_history: list                # [{"to", "from", "when"}]
    body_dates: list                   # [datetime]

    @property
    def is_done(self):
        if not self.todo:
            return False
        return _normalize_todo(self.todo) in TERMINAL_TODO_STATES

    @property
    def next_date(self) -> Optional[date]:
        """Earliest upcoming date among scheduled, deadline, and body dates."""
        today = date.today()
        candidates = []
        if self.scheduled and self.scheduled >= today:
            candidates.append(self.scheduled)
        if self.deadline and self.deadline >= today:
            candidates.append(self.deadline)
        for d in self.body_dates:
            if d.date() >= today:
                candidates.append(d.date())
        return min(candidates) if candidates else None

    def scheduled_on(self, day: date) -> bool:
        """Appears as scheduled: on *day* or is overdue-scheduled (past, not done)."""
        if self.is_done or not self.scheduled:
            return False
        return self.scheduled <= day

    def deadline_on(self, day: date) -> bool:
        """Deadline is on *day* or within the warning window, or overdue."""
        if self.is_done or not self.deadline:
            return False
        return self.deadline - timedelta(days=DEADLINE_WARNING_DAYS) <= day

    def appointment_on(self, day: date) -> bool:
        """Has an active timestamp in the body that falls exactly on *day*."""
        if self.is_done:
            return False
        return any(d.date() == day for d in self.body_dates)

    def agenda_label(self, day: date) -> str:
        """Short label describing why this task appears for *day*."""
        if self.scheduled and self.scheduled < day and not self.is_done:
            return f"Scheduled: {(day - self.scheduled).days}d ago"
        if self.scheduled and self.scheduled == day:
            return "Scheduled"
        if self.deadline:
            delta = (self.deadline - day).days
            if delta < 0:
                return f"OVERDUE deadline ({-delta}d ago)"
            if delta == 0:
                return "Deadline: today"
            return f"Deadline in {delta}d"
        return "Appointment"

def _parse_state_changes(body: str) -> list:
    changes = []
    for m in _STATE_CHANGE_RE.finditer(body):
        to_state, from_state, raw = m.groups()
        changes.append({
            "to":   to_state,
            "from": from_state,
            "when": datetime.strptime(raw, "%Y-%m-%d %a %H:%M"),
        })
    return changes


def _parse_active_dates(text: str) -> list:
    dates = []
    for m in _ACTIVE_TS_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            dates.append(datetime.strptime(raw, "%Y-%m-%d"))
        except ValueError:
            pass
    return dates


def _parse_timed_timestamps(text: str) -> list[tuple[date, dtime, Optional[dtime]]]:
    """Extract <YYYY-MM-DD [Day] HH:MM[-HH:MM]> timestamps from text."""
    parsed: list[tuple[date, dtime, Optional[dtime]]] = []
    for match in _ACTIVE_TS_WITH_TIME_RE.finditer(text):
        day_raw, start_raw, end_raw = match.groups()
        try:
            day = datetime.strptime(day_raw, "%Y-%m-%d").date()
            start_t = datetime.strptime(start_raw, "%H:%M").time()
            end_t = datetime.strptime(end_raw, "%H:%M").time() if end_raw else None
            parsed.append((day, start_t, end_t))
        except ValueError:
            continue
    return parsed


def _strip_active_timestamps(text: str) -> str:
    return re.sub(r'\s*<\d{4}-\d{2}-\d{2}[^>]*>', '', text).strip()


def _to_date(value) -> Optional[date]:
    """Coerce an orgparse OrgDate/datetime/date to a plain date, or return None."""
    if value is None:
        return None
    # orgparse OrgDate objects have a .start attribute which may be a datetime or date
    if hasattr(value, 'start'):
        value = value.start
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _node_to_task(node) -> Optional[OrgTask]:
    scheduled = _to_date(node.scheduled)
    deadline  = _to_date(node.deadline)
    heading_dates = _parse_active_dates(node.heading)
    body_dates = _parse_active_dates(node.body)
    all_active_dates = heading_dates + body_dates
    inferred_todo, cleaned_heading = _extract_todo_from_heading(node.heading)
    resolved_todo = node.todo or inferred_todo

    has_date = scheduled or deadline or all_active_dates
    if not resolved_todo and not has_date:
        return None

    # Collect state-change history from both the regex (regular tasks) and
    # orgparse's repeated_tasks list (habit/repeating tasks stored in LOGBOOK).
    history = _parse_state_changes(node.body)
    seen_whens = {entry["when"] for entry in history}
    for rt in node.repeated_tasks:
        try:
            when = rt.start if isinstance(rt.start, datetime) else datetime.combine(rt.start, dtime(0, 0))
        except Exception:
            continue
        if when not in seen_whens:
            history.append({"to": rt.after, "from": rt.before, "when": when})
            seen_whens.add(when)

    return OrgTask(
        heading=cleaned_heading if inferred_todo and not node.todo else node.heading,
        todo=resolved_todo,
        tags=node.tags,
        scheduled=scheduled,
        deadline=deadline,
        properties=dict(node.properties),
        state_history=history,
        body_dates=all_active_dates,
    )


def _parse_file(path: str) -> list[OrgTask]:
    org = orgparse.load(path)
    tasks = []
    for node in org[1:]:
        task = _node_to_task(node)
        if task is not None:
            tasks.append(task)
    return tasks


def _file_digest(path: str) -> str:
    """Stable content hash used for robust change detection."""
    hasher = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as file:
        while True:
            chunk = file.read(1024 * 64)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()

@dataclass
class _FileCache:
    path: str
    signature: tuple[int, int, int] | None = None
    digest: str | None = None
    tasks: list = field(default_factory=list)

    def refresh_if_changed(self):
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return

        current_signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        current_digest = _file_digest(self.path)

        if current_signature != self.signature or current_digest != self.digest:
            self.tasks = _parse_file(self.path)
            self.signature = current_signature
            self.digest = current_digest


class OrgStore:
    def __init__(self, paths: list[str]):
        self._caches: dict[str, _FileCache] = {
            p: _FileCache(path=p) for p in paths
        }

    def refresh(self):
        for cache in self._caches.values():
            cache.refresh_if_changed()

    def all_tasks(self) -> list[OrgTask]:
        self.refresh()
        tasks = []
        for cache in self._caches.values():
            tasks.extend(cache.tasks)
        return tasks

    def pending(self) -> list[OrgTask]:
        return [t for t in self.all_tasks() if not t.is_done and "habits" not in t.tags]

    def by_date(self) -> dict[date, list[OrgTask]]:
        result: dict[date, list[OrgTask]] = {}
        for task in self.pending():
            d = task.next_date
            if d is not None:
                result.setdefault(d, []).append(task)
        return dict(sorted(result.items()))

    def agenda_for_day(self, day: date) -> list[dict]:
        seen: set[int] = set()
        entries = []

        def _add(task, kind, label):
            tid = id(task)
            if tid not in seen:
                seen.add(tid)
                entries.append({"task": task, "label": label, "kind": kind})

        all_tasks = [t for t in self.all_tasks() if "habits" not in t.tags]

        for t in all_tasks:
            if t.scheduled_on(day):
                kind = "scheduled-overdue" if t.scheduled < day else "scheduled"
                _add(t, kind, t.agenda_label(day))

        for t in all_tasks:
            if t.deadline_on(day):
                kind = "deadline-overdue" if t.deadline < day else "deadline"
                _add(t, kind, t.agenda_label(day))

        for t in all_tasks:
            if t.appointment_on(day):
                _add(t, "appointment", t.agenda_label(day))

        return entries

    def habit_tracker_data(self) -> list[dict]:
        """
        Return one entry per task tagged 'habits', with its full completion history.
        A "completion" is any state-history entry whose `to` state is a terminal state.
        Returns list of:
            {
                "task":        OrgTask,
                "title":       str,           # cleaned heading
                "completions": [date, ...],   # sorted ascending
            }
        """
        self.refresh()
        results = []
        for task in self.all_tasks():
            if "habits" not in task.tags:
                continue
            completions = sorted(
                {
                    change["when"].date()
                    for change in task.state_history
                    if _normalize_todo(change["to"]) in TERMINAL_TODO_STATES
                }
            )
            results.append({
                "task": task,
                "title": _strip_active_timestamps(task.heading),
                "completions": completions,
            })
        results.sort(key=lambda h: h["title"].lower())
        return results

    def timed_events_for_day(self, day: date) -> list[dict]:
        """Return events with explicit time for one day, sorted by start time."""
        self.refresh()
        events: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        for task in self.all_tasks():
            if task.is_done:
                continue
            if "habits" in task.tags:
                continue

            text_sources = [task.heading]
            for text in text_sources:
                for event_day, start_t, end_t in _parse_timed_timestamps(text):
                    if event_day != day:
                        continue

                    if end_t is None:
                        end_dt = datetime.combine(day, start_t) + timedelta(minutes=30)
                        end_t = end_dt.time()

                    title = _strip_active_timestamps(task.heading)
                    key = (title, start_t.isoformat(), end_t.isoformat())
                    if key in seen:
                        continue
                    seen.add(key)

                    events.append({
                        "title": title,
                        "start": start_t,
                        "end": end_t,
                        "todo": task.todo,
                        "tags": list(task.tags),
                    })

        return sorted(events, key=lambda event: (event["start"], event["end"], event["title"]))

def create_org_blueprint(store: OrgStore) -> Blueprint:
    bp = Blueprint("org", __name__, url_prefix="/org")

    @bp.route("/")
    @bp.route("/calendar")
    def calendar():
        today   = date.today()
        agenda_days = []
        for offset in range(8):
            day = today + timedelta(days=offset)
            entries = store.agenda_for_day(day)
            agenda_days.append({"day": day, "entries": entries})
        return render_template(
            "org_calendar.html",
            title="Org Agenda",
            agenda_days=agenda_days,
            today=today,
        )

    return bp


def attach_org(app, org_files: list[str]):
    store = OrgStore(org_files)
    bp    = create_org_blueprint(store)
    app.register_blueprint(bp)
    return store
