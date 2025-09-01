"""
teacher_schedule_parser.py
============================

This module provides a set of functions to aggregate teachers' lesson schedules
from the public group schedules published on the BMSTU student portal.

The portal exposes an API under the ``/lks-back/api/v1`` prefix which the
web application calls to load both group and teacher timetables.  While the
exact API endpoints are not officially documented, the JavaScript bundles
served by the site reveal a ``BACKEND_URL`` of ``https://lks.bmstu.ru/lks-back/api/v1``
and helper functions building URLs for group and teacher schedules【459112020776857†L86-L111】.

By inspecting the network traffic (or the site's JavaScript) one can derive
the following endpoints:

``/schedule/group/<uuid>``
    Returns the full timetable for the specified group identified by its UUID.

``/schedule/teacher/<uuid>``
    Returns the timetable for the specified teacher identified by their UUID.

These endpoints are accessed via HTTP GET and respond with JSON objects
containing an array of lesson entries.  Each entry describes the date,
time, discipline, audience and includes information about participating
teachers and groups.  The parser in this module assumes this JSON structure
and falls back gracefully when unknown fields are encountered.

Example usage:

.. code-block:: python

    from teacher_schedule_parser import aggregate_teachers_from_groups

    # UUIDs of groups you want to aggregate (can be taken from the schedule URL)
    group_ids = [
        "8156af29-bc7e-11ee-b32d-df9b99f124c0",  # example group from BMSTU site
        # add more UUIDs here
    ]

    teacher_schedule = aggregate_teachers_from_groups(group_ids)
    for teacher_name, lessons in teacher_schedule.items():
        print(f"Schedule for {teacher_name}:")
        for lesson in lessons:
            print("  {date} {start}-{end}: {discipline} in {room}".format(**lesson))

The resulting ``teacher_schedule`` dictionary maps each teacher's full name to
their list of lessons.  Each lesson entry contains the date, start time,
end time, discipline, room and group identifiers.

Note: The BMSTU site occasionally requires an authenticated session to access
teacher schedules.  When only group schedules are publicly available, this
parser still works because each group lesson record includes the teachers
present.  Aggregating across all groups yields a complete timetable for
every teacher.

"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import requests


BASE_URL = "https://lks.bmstu.ru/lks-back/api/v1"


@dataclass
class Lesson:
    """Representation of a single lesson.

    Attributes
    ----------
    date : str
        ISO formatted date (YYYY-MM-DD).
    start_time : str
        Lesson start time in HH:MM format.
    end_time : str
        Lesson end time in HH:MM format.
    discipline : str
        Name of the discipline (subject).
    room : Optional[str]
        Audience or classroom; may be ``None`` if not specified.
    groups : List[str]
        List of UUIDs of groups attending this lesson.
    teachers : List[str]
        List of full teacher names participating in the lesson.
    """

    date: str
    start_time: str
    end_time: str
    discipline: str
    room: Optional[str]
    groups: List[str] = field(default_factory=list)
    teachers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, str]:
        """Convert the lesson to a plain dictionary for easy formatting."""
        return {
            "date": self.date,
            "start": self.start_time,
            "end": self.end_time,
            "discipline": self.discipline,
            "room": self.room or "",
            "groups": ", ".join(self.groups),
            "teachers": ", ".join(self.teachers),
        }


def _parse_lesson(item: Dict) -> Lesson:
    """Internal helper to convert a raw lesson dict into a :class:`Lesson`.

    Parameters
    ----------
    item : dict
        Raw JSON dict representing a lesson entry returned by the API.

    Returns
    -------
    Lesson
        The parsed lesson instance.
    """

    # Extract date and time fields.  The API returns separate hour and minute
    # numbers; we normalise them into ISO formatted strings.
    date_str = item.get("date") or item.get("day") or ""
    # Some APIs return ``dayOfWeek`` etc.  We fall back to empty string.

    start_hour = item.get("startTimeHourNum") or item.get("startHour")
    start_min = item.get("startTimeMinNum") or item.get("startMinute") or 0
    end_hour = item.get("endTimeHourNum") or item.get("endHour")
    end_min = item.get("endTimeMinNum") or item.get("endMinute") or 0
    def fmt_time(h: Optional[int], m: Optional[int]) -> str:
        if h is None:
            return ""
        return f"{int(h):02d}:{int(m or 0):02d}"

    start_time = fmt_time(start_hour, start_min)
    end_time = fmt_time(end_hour, end_min)

    discipline = item.get("discipline") or item.get("subject") or item.get("title") or ""
    room = item.get("room") or item.get("auditory") or item.get("audience")

    # Extract lists of groups and teachers.  The structure may vary across APIs.
    groups = []
    teachers = []

    # The current API (schedule.group endpoint) returns a "stream" object with
    # sub‑groups and teacher information nested inside.  We attempt to extract
    # both group UUIDs and teacher full names.
    stream = item.get("stream") or {}
    if isinstance(stream, dict):
        # Groups may be nested under ``groups`` -> list of objects with
        # ``groupUuid`` keys and optional ``sub1``/``sub2`` indicators.
        for group in stream.get("groups", []):
            uuid = group.get("groupUuid") or group.get("uuid")
            if uuid:
                groups.append(str(uuid))
    # Teachers may appear directly in the item or in ``stream``.
    for teacher in item.get("teachers", []) + stream.get("teachers", []):
        # Some objects contain firstName, middleName, lastName; others may
        # already have a ``name`` property.
        if isinstance(teacher, dict):
            name_parts = [teacher.get(key, "") for key in ("lastName", "firstName", "middleName")]
            # Filter out empty strings and join with spaces.  Example: Иванов И.И.
            name = " ".join(part for part in name_parts if part)
        else:
            name = str(teacher)
        if name:
            teachers.append(name.strip())

    return Lesson(
        date=date_str,
        start_time=start_time,
        end_time=end_time,
        discipline=discipline,
        room=room,
        groups=groups,
        teachers=teachers,
    )


def get_group_schedule(group_uuid: str) -> List[Lesson]:
    """Retrieve and parse the lesson schedule for a single group.

    Parameters
    ----------
    group_uuid : str
        The UUID of the group to fetch.

    Returns
    -------
    List[Lesson]
        A list of :class:`Lesson` objects representing the group's timetable.

    Raises
    ------
    requests.HTTPError
        If the HTTP request fails (non‑200 response).
    """

    url = f"{BASE_URL}/schedule/group/{group_uuid}"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()

    # The API returns an object with a ``data`` field containing the schedule.
    schedule_list = data.get("data") or data.get("schedule") or []

    lessons: List[Lesson] = []
    for item in schedule_list:
        try:
            lessons.append(_parse_lesson(item))
        except Exception:
            # Skip malformed entries without stopping the entire parsing process.
            continue
    return lessons


def aggregate_teachers_from_groups(group_uuids: List[str]) -> Dict[str, List[Dict[str, str]]]:
    """Aggregate teachers' schedules across multiple groups.

    This function iterates through a list of group UUIDs, downloads each group's
    timetable, extracts the lessons and then builds a mapping from teacher names
    to lists of lessons.  If a teacher appears in multiple groups, their
    lessons are combined into a single list.  Duplicate lesson entries (same
    date and time) are not removed to preserve full context.

    Parameters
    ----------
    group_uuids : list of str
        List of UUIDs identifying the groups whose schedules should be
        aggregated.

    Returns
    -------
    Dict[str, List[Dict[str, str]]]
        Dictionary mapping teacher full names to a list of lesson dictionaries
        (converted via :meth:`Lesson.to_dict`).
    """
    teacher_schedule: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for group_id in group_uuids:
        try:
            lessons = get_group_schedule(group_id)
        except Exception as exc:
            # If a specific group fails to load, continue with others.
            print(f"Failed to load schedule for group {group_id}: {exc}")
            continue
        for lesson in lessons:
            for teacher_name in lesson.teachers:
                teacher_schedule[teacher_name].append(lesson.to_dict())
    return teacher_schedule


def search_teacher_schedule(teacher_schedule: Dict[str, List[Dict[str, str]]], query: str) -> Dict[str, List[Dict[str, str]]]:
    """Search for teachers whose names contain a given query string.

    Parameters
    ----------
    teacher_schedule : dict
        The aggregated teacher schedule produced by
        :func:`aggregate_teachers_from_groups`.
    query : str
        Case‑insensitive substring to look for in teacher names.

    Returns
    -------
    dict
        Subset of ``teacher_schedule`` with only the matching teachers.
    """
    q = query.strip().lower()
    return {name: lessons for name, lessons in teacher_schedule.items() if q in name.lower()}


if __name__ == "__main__":
    # Example CLI interface: the user can pass group UUIDs as command line
    # arguments to quickly aggregate and display teacher schedules.
    import argparse
    parser = argparse.ArgumentParser(description="Aggregate teachers' schedules from BMSTU group timetables.")
    parser.add_argument("group", nargs="*", help="UUID(s) of groups to process")
    parser.add_argument("-s", "--search", metavar="QUERY", help="Optional teacher name substring to search for")
    parser.add_argument("-o", "--output", metavar="FILE", help="Optional JSON file to write the aggregated schedule to")
    args = parser.parse_args()

    if not args.group:
        parser.error("Please specify at least one group UUID")

    schedule = aggregate_teachers_from_groups(args.group)

    if args.search:
        schedule = search_teacher_schedule(schedule, args.search)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(schedule, f, ensure_ascii=False, indent=2)
        print(f"Written aggregated schedule to {args.output}")
    else:
        # Pretty print to console
        for teacher, lessons in schedule.items():
            print(teacher)
            for lesson in lessons:
                print("  {date} {start}-{end} | {discipline} | Room: {room} | Groups: {groups}".format(**lesson))
            print()