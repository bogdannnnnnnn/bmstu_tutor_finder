#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BMSTU Teachers Tool: парсинг расписаний групп -> JSON-индекс преподавателей + поиск

Возможности:
- Build: обойти список расписаний, скачать .ics для каждой группы, распарсить, сохранить JSON
- Search: быстро искать по преподавателю (подстрокой + "похожим" совпадением)

Зависимости: requests (pip install requests)
Python >= 3.10 (zoneinfo)

Источник данных:
- Страница со списком расписаний: https://lks.bmstu.ru/schedule/list
- Страница конкретной группы:  https://lks.bmstu.ru/schedule/<UUID>
- ICS у группы:                    .../schedule/<UUID>.ics

Примечания по данным:
- ФИО чаще всего в DESCRIPTION; иногда в SUMMARY после номера аудитории.
- Время в ICS обычно в UTC (оканчивается на Z); конвертируем в Europe/Moscow.

Автор: под ваши нужды можно легко расширить (Flask API, сохранение в БД, и т.д.)
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
MOSCOW = ZoneInfo("Europe/Moscow")

# Глобальные заголовки для HTTP-запросов. Можно дополнить Cookie из CLI.
EXTRA_HEADERS = {"User-Agent": UA}

UUID_RE = re.compile(r"/schedule/([0-9a-fA-F\-]{36})")
ICS_LINE_FOLD_RE = re.compile(r"^\s")  # строки, начинающиеся с пробела = продолжение предыдущей
# типы занятий для отделения предмета в SUMMARY
TYPES = ["лек.", "лек", "лаб.", "лаб", "пр.", "пр", "сем.", "сем", "конс.", "экз.", "зач.", "курс.пр."]
TYPES_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in TYPES) + r")\b", flags=re.IGNORECASE)

# шаблоны для ФИО
RE_TEACHER_FROM_DESC = re.compile(
    r"(?:Преподаватель[^:]*:\s*)?([А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)?(?:\s+[А-ЯЁ][а-яё\-]+)?)"
)
RE_TEACHER_AFTER_ROOM = re.compile(
    r"(?:\b\d{1,2}[-–]?\d{1,3}[А-Яа-яA-Za-z]?)\s+([А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)?)"
)
RE_FIO_INITIALS = re.compile(r"\b[А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.){1,2}\b")

RU_DOW = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


@dataclasses.dataclass
class Lesson:
    group: str
    subject: str
    day: str
    time: str
    room: Optional[str]
    teacher: str


def http_get(url: str, timeout: float = 30.0) -> Optional[str]:
    try:
        r = requests.get(url, headers=EXTRA_HEADERS, timeout=timeout)
        if r.status_code == 200 and r.text:
            # На некоторых .ics Content-Type не text/calendar — полагаемся на .text
            return r.text
        return None
    except requests.RequestException:
        return None


def find_group_ids_from_seed(seed_url: str) -> List[str]:
    """
    Загружает страницу-список и вытаскивает все UUID расписаний из href="/schedule/<uuid>".
    Если страница динамическая — часто всё равно SSR содержит ссылки; если нет — используйте --urls-file.
    """
    html = http_get(seed_url)
    if not html:
        return []
    ids = set(m.group(1) for m in UUID_RE.finditer(html))
    return sorted(ids)


def to_group_url(uuid: str) -> str:
    return f"https://lks.bmstu.ru/schedule/{uuid}"


def to_ics_url(uuid_or_url: str) -> str:
    if uuid_or_url.startswith("http"):
        base = uuid_or_url.split("?")[0].rstrip("/")
        return base + ".ics" if not base.endswith(".ics") else base
    # если пришёл чистый uuid
    return f"https://lks.bmstu.ru/schedule/{uuid_or_url}.ics"


def unfold_ics(text: str) -> List[str]:
    """
    Склейка перенесённых строк ICS (строки, начинающиеся с пробела — это продолжение предыдущей).
    """
    lines = text.splitlines()
    res: List[str] = []
    for line in lines:
        if line.startswith(" "):
            if res:
                res[-1] += line[1:]
        else:
            res.append(line)
    return res


def parse_dt(value: str) -> Optional[datetime]:
    """
    Поддержка формата 20240208T071500Z (UTC) и 20240208T071500 (локальный).
    """
    value = value.strip()
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(MOSCOW)
        # иногда бывает DATE без времени — игнорируем
        if len(value) == 8:
            return None
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=MOSCOW)
    except Exception:
        return None


def extract_subject(summary: str) -> str:
    s = summary.strip()
    m = TYPES_RE.search(s)
    if m:
        return s[: m.start()].strip(" -–\u00a0")
    # иногда в SUMMARY через подгруппы I/II/III — режем по первому римскому числу
    parts = re.split(r"\s+[IVX]+\s+", s)
    return parts[0].strip(" -–\u00a0")


def normalize_name(name: str) -> str:
    n = re.sub(r"\s+", " ", name).strip()
    # унифицируем 'Ё'
    n = n.replace("Ё", "Е").replace("ё", "е")
    # пробелы в инициалах: "И. О." -> "И.О."
    n = re.sub(r"\s*\.\s*", ".", n)
    n = re.sub(r"\s*([А-Я])\.\s*([А-Я])\.", r" \1.\2.", n)
    return n


def extract_teachers(description: str, summary: str) -> List[str]:
    cand: List[str] = []

    # 1) из DESCRIPTION (главный источник)
    if description:
        for m in RE_TEACHER_FROM_DESC.finditer(description):
            val = normalize_name(m.group(1))
            if len(val) >= 3:
                cand.append(val)

        for m in RE_FIO_INITIALS.finditer(description):
            val = normalize_name(m.group(0))
            if len(val) >= 3:
                cand.append(val)

    # 2) fallback: из SUMMARY после номера аудитории
    if summary:
        for m in RE_TEACHER_AFTER_ROOM.finditer(summary):
            val = normalize_name(m.group(1))
            cand.append(val)

        for m in RE_FIO_INITIALS.finditer(summary):
            val = normalize_name(m.group(0))
            cand.append(val)

    # фильтры: убираем очевидный мусор
    cleaned: List[str] = []
    for t in cand:
        t = t.strip(",;:. ")
        # отбрасываем слова-предметы, случайно попавшие как "ФИО"
        if len(t.split()) == 1 and not re.match(r"^[А-ЯЁ][а-яё\-]+$", t):
            continue
        cleaned.append(t)

    # уникализируем, сохраняя порядок
    seen: Set[str] = set()
    uniq: List[str] = []
    for t in cleaned:
        if t not in seen:
            seen.add(t)
            uniq.append(t)

    return uniq


def parse_ics(ics_text: str) -> Tuple[str, List[Lesson]]:
    """
    Возвращает (group_code, lessons[])
    group_code берём из X-WR-CALNAME: 'Расписание <КодГруппы>'
    """
    lines = unfold_ics(ics_text)

    group_code = "UNKNOWN"
    for ln in lines:
        if ln.startswith("X-WR-CALNAME:"):
            title = ln.split(":", 1)[1].strip()








    
            # обычно "Расписание ИУ3-45БВ" — берём хвост после "Расписание "
            m = re.search(r"Расписание\s+(.+)", title)
            group_code = m.group(1).strip() if m else title
            break

    lessons: List[Lesson] = []
    ev: Dict[str, str] = {}
    in_event = False

    def flush_event():
        if not ev:
            return
        summary = ev.get("SUMMARY", "").strip()
        description = ev.get("DESCRIPTION", "").strip()
        location = ev.get("LOCATION", "").strip() or None
        dtstart = parse_dt(ev.get("DTSTART", ""))
        dtend = parse_dt(ev.get("DTEND", ""))

        if not summary or not dtstart or not dtend:
            ev.clear()
            return

        subject = extract_subject(summary)
        teachers = extract_teachers(description, summary)
        if not teachers:
            # иногда преподавателя нет — пропускаем такие записи
            ev.clear()
            return

        dow = RU_DOW[dtstart.weekday()]
        time_str = f"{dtstart.strftime('%H:%M')}–{dtend.strftime('%H:%M')}"
        for t in teachers:
            lessons.append(
                Lesson(
                    group=group_code,
                    subject=subject,
                    day=dow,
                    time=time_str,
                    room=location,
                    teacher=t,
                )
            )
        ev.clear()

    for ln in lines:
        if ln.startswith("BEGIN:VEVENT"):
            in_event = True
            ev = {}
            continue
        if ln.startswith("END:VEVENT"):
            in_event = False
            flush_event()
            continue
        if not in_event:
            continue

        if ":" not in ln:
            continue
        key_part, val = ln.split(":", 1)
        key = key_part.split(";", 1)[0].upper()
        if key in ("SUMMARY", "DESCRIPTION", "LOCATION", "DTSTART", "DTEND", "RRULE"):
            ev[key] = val.strip()

    return group_code, lessons


def build_index(
    seed_url: Optional[str],
    urls_file: Optional[str],
    extra_urls: List[str],
    out_index: str,
    out_groups_raw: str,
    max_groups: Optional[int],
    concurrency: int = 10,
) -> None:
    # 1) собираем список UUID/URL
    urls: List[str] = []

    if seed_url:
        ids = find_group_ids_from_seed(seed_url)
        if ids:
            urls.extend(to_group_url(x) for x in ids)

    if urls_file and os.path.exists(urls_file):
        with open(urls_file, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                urls.append(s)

    if extra_urls:
        urls.extend(extra_urls)

    # чистим/уникализируем
    useen: Set[str] = set()
    clean_urls: List[str] = []
    for u in urls:
        u = u.split("?")[0].strip()
        if not u:
            continue
        if not u.startswith("http"):
            # считаем, что нам дали UUID
            u = to_group_url(u)
        if u not in useen:
            useen.add(u)
            clean_urls.append(u)

    if max_groups:
        clean_urls = clean_urls[: max_groups]

    if not clean_urls:
        print("Не нашли ни одной ссылки на расписание. Дайте --urls-file или --urls.")
        sys.exit(2)

    # 2) качаем .ics параллельно
    groups_raw: Dict[str, Dict] = {}
    teachers_index: Dict[str, List[Dict]] = defaultdict(list)

    def worker(group_url: str) -> Tuple[str, Optional[str]]:
        ics_url = to_ics_url(group_url)
        return ics_url, http_get(ics_url)

    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(worker, u) for u in clean_urls]
        for fut in cf.as_completed(futures):
            ics_url, ics_text = fut.result()
            if not ics_text:
                # пропускаем недоступные
                continue
            group_code, lessons = parse_ics(ics_text)
            if not lessons:
                continue

            # сырой блок
            groups_raw.setdefault(group_code, {"ics_url": ics_url, "lessons": []})
            for L in lessons:
                groups_raw[group_code]["lessons"].append(dataclasses.asdict(L))

                entry = {
                    "group": L.group,
                    "subject": L.subject,
                    "day": L.day,
                    "time": L.time,
                    "room": L.room,
                }
                key = normalize_name(L.teacher)
                # избегаем дублей по (group+subject+day+time)
                if entry not in teachers_index[key]:
                    teachers_index[key].append(entry)

    # 3) сохраняем
    with open(out_groups_raw, "w", encoding="utf-8") as f:
        json.dump(groups_raw, f, ensure_ascii=False, indent=2)

    with open(out_index, "w", encoding="utf-8") as f:
        json.dump(teachers_index, f, ensure_ascii=False, indent=2)

    print(f"Готово. Сохранено:\n  {out_index}\n  {out_groups_raw}")


def search_index(index_path: str, query: str, topn: int = 20) -> None:
    if not os.path.exists(index_path):
        print(f"Файл индекса не найден: {index_path}")
        sys.exit(2)
    with open(index_path, "r", encoding="utf-8") as f:
        idx: Dict[str, List[Dict]] = json.load(f)

    q = normalize_name(query)
    # 1) точные/подстрочные совпадения ключа
    keys = [k for k in idx.keys() if q in normalize_name(k)]

    # 2) если пусто — простая "похожесть" по подстроке в занятиях (ФИО упоминается в ключах)
    if not keys:
        parts = q.split()
        if parts:
            p = parts[0]
            keys = [k for k in idx.keys() if p in normalize_name(k)]

    if not keys:
        print("Ничего не нашлось :(")
        return

    # сортировка по длине ключа и алфавиту
    keys = sorted(keys, key=lambda k: (len(k), k))[:topn]

    for i, tname in enumerate(keys, 1):
        print(f"\n{i}. {tname}")
        for row in sorted(idx[tname], key=lambda r: (r["day"], r["time"], r["group"], r["subject"])):
            room = f" • ауд. {row['room']}" if row.get("room") else ""
            print(f"   {row['day']} {row['time']} • {row['group']} • {row['subject']}{room}")


def main():
    ap = argparse.ArgumentParser(description="BMSTU Teachers Tool: парсер расписаний и поиск по преподавателям")
    sub = ap.add_subparsers(dest="cmd")

    a_build = sub.add_parser("build", help="Собрать индекс преподавателей из расписаний")
    a_build.add_argument("--seed", type=str, default="https://lks.bmstu.ru/schedule/list",
                         help="Стартовая страница со списком расписаний (или выключите и дайте --urls-file)")
    a_build.add_argument("--urls-file", type=str, default=None, help="Файл со ссылками на /schedule/<UUID> (по одной на строку)")
    a_build.add_argument("--urls", type=str, nargs="*", default=[], help="Доп. ссылки на /schedule/<UUID> или сами UUID")
    a_build.add_argument("--out", type=str, default="teachers_index.json", help="Куда сохранить индекс преподавателей")
    a_build.add_argument("--groups-out", type=str, default="groups_raw.json", help="Куда сохранить сырые данные по группам")
    a_build.add_argument("--max", type=int, default=None, help="Ограничить число групп (для отладки)")
    a_build.add_argument("--concurrency", type=int, default=10, help="Параллельные загрузки .ics")
    a_build.add_argument("--cookie", type=str, default=None,
                         help="Строка Cookie для доступа к .ics (скопируйте из браузера)")
    a_build.add_argument("--cookie-file", type=str, default=None,
                         help="Файл с одной строкой Cookie (альтернатива --cookie)")

    a_search = sub.add_parser("search", help="Поиск по ФИО преподавателя в готовом индексе")
    a_search.add_argument("query", type=str, help='Напр.: "Иванов" или "Иванов И.О."')
    a_search.add_argument("--index", type=str, default="teachers_index.json", help="Путь к файлу индекса")

    args = ap.parse_args()

    if args.cmd == "build":
        # Настраиваем Cookie из аргументов, если заданы
        cookie_val: Optional[str] = None
        if getattr(args, "cookie", None):
            cookie_val = args.cookie.strip()
        elif getattr(args, "cookie_file", None):
            try:
                with open(args.cookie_file, "r", encoding="utf-8") as _cf:
                    cookie_val = _cf.read().strip()
            except OSError:
                cookie_val = None
        if cookie_val:
            EXTRA_HEADERS["Cookie"] = cookie_val

        build_index(
            seed_url=args.seed,
            urls_file=args.urls_file,
            extra_urls=args.urls,
            out_index=args.out,
            out_groups_raw=args.groups_out,
            max_groups=args.max,
            concurrency=args.concurrency,
        )
    elif args.cmd == "search":
        search_index(args.index, args.query)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
