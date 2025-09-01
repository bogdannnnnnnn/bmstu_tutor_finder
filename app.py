from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
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


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = APP_SECRET

    @app.context_processor
    def inject_globals() -> Dict[str, Any]:
        return {
            "cookie_set": bool(session.get("lks_cookie")),
        }

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
        if q:
            return redirect(url_for("search", q=q))
        return render_template("index.html")

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

        # Day mapping per BMSTU: 1=Mon ... 7=Sun; API shows ints 1..7 (in sample 4=Thu, 5=Fri, 6=Sat)
        ru_days = {1: "Понедельник", 2: "Вторник", 3: "Среда", 4: "Четверг", 5: "Пятница", 6: "Суббота", 7: "Воскресенье"}

        def to_week_label(val: str) -> str:
            if val == "all":
                return "все"
            # На сайте используются термины "числитель/знаменатель"
            # API кодирует недели как ch/zn. Интерпретируем:
            if val == "ch":
                return "числитель"
            if val == "zn":
                return "знаменатель"
            return val or ""

        return render_template(
            "teacher.html",
            info=payload,
            schedule=schedule_sorted,
            ru_days=ru_days,
            to_week_label=to_week_label,
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


