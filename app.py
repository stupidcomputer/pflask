from flask import (
    current_app,
    Flask,
    redirect,
    render_template,
    request
)
import urllib.parse
from datetime import datetime, date, time, timedelta
from dataclasses import dataclass
from typing import Callable, Any
import json

from org import attach_org

app = Flask(__name__)

ORG_FILES = [
    "/home/usr/org/main.org",
    "/home/usr/org/body.org",
    "/home/usr/org/inbox.org",
    "/home/usr/org/agenda.org",
    "/home/usr/org/pco.org",
    "/home/usr/org/tfb.org",
    "/home/usr/org/school-calendar.org",
    "/home/usr/org/phone-inbox.org",
]

org_store = attach_org(app, ORG_FILES)

@dataclass
class CachedSource:
    resource: str
    postprocessing: Callable | None
    staletime: int

    _cached_content: str | None = None
    _cached_processing: Any | None = None
    _last_fetched: datetime | None = None
    _last_processed: datetime | None = None
    def __post_init__(self):
        self.cached_content = None

    def _is_fetched_stale(self):
        if self._last_fetched:
            return (datetime.now() - self._last_fetched).seconds > self.staletime
        return True

    def _is_processed_stale(self):
        if self._last_processed:
            return (datetime.now() - self._last_processed).seconds > self.staletime
        return True

    def fetch(self):
        if not self._cached_content and self._is_fetched_stale():
            with open(self.resource, "r") as file:
                self._cached_content = file.read()

        return self._cached_content

    def process(self):
        self.fetch()
        if self.postprocessing and not self._cached_processing and self._is_processed_stale():
            self._cached_processing = self.postprocessing(self._cached_content)

        return self._cached_processing

def weather_postprocessing(weather_data: str):
    data = json.loads(weather_data)
    data = data["properties"]["periods"]

    now = datetime.now().astimezone()

    for period in data:
        period["startTime"] = datetime.fromisoformat(period["startTime"])
        period["endTime"] = datetime.fromisoformat(period["endTime"])

        if period["startTime"] < now and  period["endTime"] > now:
            return period

    return None
        
weather = CachedSource(
    resource="/home/usr/.cache/nws_weather_forecast.json",
    postprocessing=weather_postprocessing,
    staletime=100
)

def current_time_bucket(current_time: datetime = datetime.now()):
    buckets = {
        time(hour=0): "morning",
        time(hour=12): "afternoon",
        time(hour=17): "evening",
        time(hour=22): "night"
    }

    result = None

    for bucket in buckets:
        if current_time.time() > bucket: result = buckets[bucket]
        else: break

    return result

name = "Ryan"


def parse_iso_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def build_day_timeline(events: list[dict]) -> tuple[list[dict], int, int, int, int]:
    """Convert timed events into positioned blocks for a day-view timeline."""
    start_hour = 0
    end_hour = 24
    total_minutes = (end_hour - start_hour) * 60
    pixels_per_minute = 2
    min_render_minutes = 20
    total_height_px = total_minutes * pixels_per_minute

    if not events:
        return [], start_hour, end_hour, total_height_px, pixels_per_minute

    enriched = []
    for index, event in enumerate(events):
        start_minutes = (event["start"].hour - start_hour) * 60 + event["start"].minute
        end_minutes = (event["end"].hour - start_hour) * 60 + event["end"].minute
        if end_minutes <= start_minutes:
            end_minutes = start_minutes + 30
        render_end_minutes = start_minutes + max(min_render_minutes, end_minutes - start_minutes)

        enriched.append({
            "index": index,
            "start_minutes": start_minutes,
            "end_minutes": end_minutes,
            "render_end_minutes": render_end_minutes,
            "column": 0,
            "total_columns": 1,
        })

    # Build connected overlap groups first.
    enriched.sort(key=lambda event: (event["start_minutes"], event["end_minutes"]))
    groups: list[list[dict]] = []
    current_group: list[dict] = []
    current_group_end = -1

    for event in enriched:
        if not current_group:
            current_group = [event]
            current_group_end = event["render_end_minutes"]
            continue

        if event["start_minutes"] < current_group_end:
            current_group.append(event)
            current_group_end = max(current_group_end, event["render_end_minutes"])
        else:
            groups.append(current_group)
            current_group = [event]
            current_group_end = event["render_end_minutes"]

    if current_group:
        groups.append(current_group)

    # Assign columns inside each overlap group.
    for group in groups:
        active: list[dict] = []
        max_columns = 0

        for event in sorted(group, key=lambda item: (item["start_minutes"], item["end_minutes"])):
            active = [item for item in active if item["render_end_minutes"] > event["start_minutes"]]
            used_columns = {item["column"] for item in active}

            column = 0
            while column in used_columns:
                column += 1

            event["column"] = column
            active.append(event)
            max_columns = max(max_columns, column + 1)

        for event in group:
            event["total_columns"] = max_columns

    enriched_by_index = {event["index"]: event for event in enriched}

    blocks = []
    for index, event in enumerate(events):
        positioned = enriched_by_index[index]
        start_minutes = positioned["start_minutes"]
        end_minutes = positioned["end_minutes"]
        duration = max(min_render_minutes, end_minutes - start_minutes)

        blocks.append({
            **event,
            "time_label": f"{event['start'].strftime('%H:%M')} – {event['end'].strftime('%H:%M')}",
            "top_px": start_minutes * pixels_per_minute,
            "height_px": duration * pixels_per_minute,
            "left_pct": (positioned["column"] / positioned["total_columns"]) * 100,
            "width_pct": 100 / positioned["total_columns"],
        })

    return blocks, start_hour, end_hour, total_height_px, pixels_per_minute


def build_day_todo_widget(target_day: date) -> dict:
    """Build TODO-only entries for a specific day, including overdue/deadline labels."""
    raw_entries = org_store.agenda_for_day(target_day)
    todo_entries = [entry for entry in raw_entries if entry["task"].todo]

    kind_order = {
        "scheduled-overdue": 0,
        "deadline-overdue": 1,
        "scheduled": 2,
        "deadline": 3,
        "appointment": 4,
    }

    todo_entries.sort(
        key=lambda entry: (
            kind_order.get(entry.get("kind"), 99),
            entry["task"].heading.lower(),
        )
    )

    return {
        "todo_widget_entries": todo_entries,
        "todo_widget_count": len(todo_entries),
    }


def build_schedule_context(target_day: date) -> dict:
    timed_events = org_store.timed_events_for_day(target_day)
    timeline_events, timeline_start_hour, timeline_end_hour, timeline_height_px, pixels_per_minute = build_day_timeline(timed_events)

    now = datetime.now()
    timeline_total_minutes = max(1, (timeline_end_hour - timeline_start_hour) * 60)
    now_minutes = (now.hour - timeline_start_hour) * 60 + now.minute
    now_visible = (target_day == date.today()) and (0 <= now_minutes <= timeline_total_minutes)
    now_line_top_px = max(0, min(timeline_height_px, now_minutes * pixels_per_minute))

    return {
        "target_day": target_day,
        "timed_events": timed_events,
        "timeline_events": timeline_events,
        "timeline_start_hour": timeline_start_hour,
        "timeline_end_hour": timeline_end_hour,
        "timeline_hour_lines": (timeline_end_hour - timeline_start_hour + 1),
        "timeline_height_px": timeline_height_px,
        "timeline_pixels_per_minute": pixels_per_minute,
        "now_visible": now_visible,
        "now_line_top_px": now_line_top_px,
        "now_time_label": now.strftime("%H:%M"),
        **build_day_todo_widget(target_day),
    }

@app.route("/mainpage")
@app.route("/")
def mainpage():
    schedule_context = build_schedule_context(date.today())

    return render_template("mainpage.html",
        name=name,
        time_bucket=current_time_bucket(),
        title="Homepage",
        **schedule_context,
    )


@app.route("/schedule")
@app.route("/schedule/<day_iso>")
def schedule(day_iso: str | None = None):
    query_day = parse_iso_day(request.args.get("day"))
    path_day = parse_iso_day(day_iso)
    target_day = query_day or path_day or date.today()

    schedule_context = build_schedule_context(target_day)
    return render_template(
        "schedule.html",
        title="Schedule",
        prev_day=(target_day - timedelta(days=1)).isoformat(),
        next_day=(target_day + timedelta(days=1)).isoformat(),
        **schedule_context,
    )

@app.route("/style.css")
def static_style_css():
    return current_app.send_static_file("style.css")

@app.route("/cups")
def redirect_to_cups():
    return redirect("http://localhost:631", code=302)

@app.route("/syncthing")
def redirect_to_syncthing():
    return redirect("http://localhost:8384", code=302)

@app.route("/hledger")
def redirect_to_hledger():
    return redirect("http://localhost:5000", code=302)

@app.route("/school/schoolmail")
def redirect_to_schoolmail():
    return redirect("https://mail.google.com/mail/u/0/#inbox", code=302)

@app.route("/school/schooldrive")
def redirect_to_schooldrive():
    return redirect("https://drive.google.com/drive/recent", code=302)

@app.route("/school/open_in_profile")
def open_in_school_profile():
    target_url = request.args.get('url')
    def is_url(url_string):
        try:
            result = urllib.parse.urlparse(url_string)
            return all([result.scheme, result.netloc]) 
        except ValueError:
            return False

    if target_url and is_url(target_url):
        return redirect(target_url, code=302)
    elif target_url:
        return redirect("https://google.com/search?" + urllib.parse.urlencode({"q": target_url}), code=302)

    return "URL not specified"

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=10000,
        debug=False,
    )
