from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

import requests
from collections import defaultdict
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


APP_SECRET = os.environ.get("SECRET_KEY", "dev-secret-change-me")
BACKEND_URL = os.environ.get("BACKEND_URL", "https://lks.bmstu.ru/lks-back/api/v1")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
LOGIN_URL = os.environ.get(
    "LOGIN_URL",
    "https://lks.bmstu.ru/portal3/login?back=https%3A%2F%2Flks.bmstu.ru%2Fprofile",
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = APP_SECRET

    @app.context_processor
    def inject_globals() -> Dict[str, Any]:
        return {
            "cookie_set": bool(session.get("lks_cookie")),
            "login_url": LOGIN_URL,
        }

    def extract_cookie_from_query() -> str:
        raw_query = request.query_string or b""
        if not raw_query:
            return ""
        for chunk in raw_query.split(b"&"):
            if not chunk:
                continue
            if chunk.startswith(b"cookie="):
                raw_value = chunk[len(b"cookie=") :]
                try:
                    encoded = raw_value.decode("ascii")
                except UnicodeDecodeError:
                    encoded = raw_value.decode("utf-8", errors="ignore")
                return unquote(encoded).strip()
        return ""

    def get_auth_headers() -> Dict[str, str]:
        headers = {
            "User-Agent": UA,
            "Accept": "application/json",
        }
        cookie = session.get("lks_cookie")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def search_teachers(query: str) -> List[Dict[str, Any]]:
        url = f"{BACKEND_URL}/schedules/search"
        r = requests.get(url, params={"s": query}, headers=get_auth_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        # API returns a list of entries {title, uuid, type}
        return [row for row in data if str(row.get("type")) == "teacher"]

    def fetch_teacher_schedule(teacher_uuid: str) -> Dict[str, Any]:
        url = f"{BACKEND_URL}/schedules/teacher/{teacher_uuid}"
        r = requests.get(url, headers=get_auth_headers(), timeout=45)
        r.raise_for_status()
        return r.json()

    @app.get("/")
    def index() -> str:
        q = request.args.get("q", "").strip()
        cookie_from_link = extract_cookie_from_query()
        if not cookie_from_link:
            cookie_from_link = request.args.get("cookie", "").strip()
        if cookie_from_link:
            session["lks_cookie"] = cookie_from_link
            flash("Cookie установлены из ссылки", "ok")
            if q:
                return redirect(url_for("search", q=q))
            return redirect(url_for("index"))
        if q:
            return redirect(url_for("search", q=q))
        return render_template("index.html", show_header_search=False, cookie_prefill=session.get("lks_cookie", ""))

    @app.post("/cookie")
    def set_cookie() -> str:
        cookie_val = request.form.get("cookie", "").strip()
        if not cookie_val:
            flash("Пустая Cookie", "warn")
            return redirect(url_for("index"))
        session["lks_cookie"] = cookie_val
        flash("Cookie сохранена для текущей сессии", "ok")
        return redirect(url_for("index"))

    @app.get("/cookie/clear")
    def clear_cookie() -> str:
        session.pop("lks_cookie", None)
        flash("Cookie удалена", "ok")
        return redirect(url_for("index"))

    @app.get("/search")
    def search() -> str:
        q = request.args.get("q", "").strip()
        if not q:
            flash("Введите запрос", "warn")
            return redirect(url_for("index"))
        try:
            results = search_teachers(q)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                flash("Нужна авторизация. Вставьте Cookie.", "err")
                return redirect(url_for("index"))
            raise
        if len(results) == 1:
            return redirect(url_for("teacher", uuid=results[0]["uuid"]))
        return render_template("results.html", q=q, results=results)

    @app.get("/teacher/<uuid>")
    def teacher(uuid: str) -> str:
        try:
            data = fetch_teacher_schedule(uuid)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                flash("Нужна авторизация. Вставьте Cookie.", "err")
                return redirect(url_for("index"))
            raise

        payload = data.get("data", {})
        schedule = payload.get("schedule", [])
        # Sort by (day, time)


        schedule_sorted = sorted(
            schedule,
            key=lambda x: (int(x.get("day", 0)), int(x.get("time", 0))),
        )

        ru_days = {1: "Понедельник", 2: "Вторник", 3: "Среда", 4: "Четверг", 5: "Пятница", 6: "Суббота", 7: "Воскресенье"}
        week_names = {"all": "Все недели", "ch": "Числитель", "zn": "Знаменатель"}

        def pick_week_key(raw: Optional[str]) -> str:
            val = (raw or "all").lower()
            if val not in ("ch", "zn"):
                return "all"
            return val

        def time_sort_key(values: Tuple[str, str, str]) -> Tuple[str, str, str]:
            start_label, end_label, pair_label = values
            return (start_label or "", end_label or "", pair_label or "")

        grid: Dict[Tuple[str, str, str], Dict[int, Dict[str, List[Dict[str, Any]]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
        time_keys: List[Tuple[str, str, str]] = []
        seen_keys: set[Tuple[str, str, str]] = set()

        for item in schedule_sorted:
            time_key = (
                item.get("startTime") or "",
                item.get("endTime") or "",
                str(item.get("time") or ""),
            )
            if time_key not in seen_keys:
                seen_keys.add(time_key)
                time_keys.append(time_key)
            day_key = int(item.get("day", 0) or 0)
            grid[time_key][day_key][pick_week_key(item.get("week"))].append(item)

        time_keys.sort(key=time_sort_key)
        day_order = [1, 2, 3, 4, 5, 6, 7]
        timetable_rows = []
        for key_start, key_end, pair_label in time_keys:
            cell_map: Dict[int, Dict[str, Any]] = {}
            for day in day_order:
                cell_weeks = grid[(key_start, key_end, pair_label)].get(day, {})
                cell_other = []
                for wk, items in cell_weeks.items():
                    if wk in {"all", "ch", "zn"} or not items:
                        continue
                    cell_other.append(
                        {
                            "key": wk,
                            "label": week_names.get(wk, wk or ""),
                            "items": list(items),
                        }
                    )
                cell_map[day] = {
                    "all": list(cell_weeks.get("all", [])),
                    "ch": list(cell_weeks.get("ch", [])),
                    "zn": list(cell_weeks.get("zn", [])),
                    "other": cell_other,
                }
                cell_map[day]["has_content"] = bool(cell_map[day]["all"] or cell_map[day]["ch"] or cell_map[day]["zn"] or cell_other)
            timetable_rows.append(
                {
                    "start": key_start,
                    "end": key_end,
                    "pair": pair_label,
                    "cells": cell_map,
                }
            )

        def to_week_label(val: str) -> str:
            return week_names.get(val, val or "")

        return render_template(
            "teacher.html",
            info=payload,
            schedule=schedule_sorted,
            ru_days=ru_days,
            to_week_label=to_week_label,
            day_order=day_order,
            timetable_rows=timetable_rows,
            week_names=week_names,
        )

    @app.get("/api/teacher/<uuid>")
    def api_teacher(uuid: str):  # noqa: ANN001 (Flask handler)
        data = fetch_teacher_schedule(uuid)
        return jsonify(data)

    return app


app = create_app()


if __name__ == "__main__":
    # Run development server
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), debug=True)


