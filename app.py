from flask import Flask, render_template, redirect, current_app

from datetime import datetime, time
from dataclasses import dataclass
from typing import Callable, Any
import json

app = Flask(__name__)

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

@app.route("/mainpage")
@app.route("/")
def mainpage():
    return render_template("mainpage.html",
        name=name,
        time_bucket=current_time_bucket(),
        title="Homepage"
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

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=10000,
        debug=False,
    )
