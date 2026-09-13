#!/usr/bin/env python3
"""Flag JBLM→Hawaii directs and JBLM→Travis→Hawaii connections."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "js" / "flights.json"
STATE = ROOT / "js" / "watch-state.json"
PACIFIC = ZoneInfo("America/Los_Angeles")
MIN_CONNECT = timedelta(hours=6)

HAWAII = ("hawaii", "hickam", "honolulu", "pearl harbor", "kaneohe", "jbphh", "barbers")
TRAVIS = ("travis", "fairfield", "suu", "ksuu", "david grant")


def hay(flight: dict) -> str:
    return f"{flight.get('dest', '')} {flight.get('destKey', '')}".lower()


def is_hawaii(flight: dict) -> bool:
    h = hay(flight)
    return any(tok in h for tok in HAWAII)


def is_travis(flight: dict) -> bool:
    h = hay(flight)
    return any(tok in h for tok in TRAVIS)


def roll_at(flight: dict) -> datetime:
    y, m, d = map(int, flight["date"].split("-"))
    hh, mm = map(int, flight["roll"].split(":"))
    return datetime(y, m, d, hh, mm, tzinfo=PACIFIC)


def upcoming(flight: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(PACIFIC)
    return roll_at(flight) >= now


def fmt(flight: dict) -> str:
    when = roll_at(flight).strftime("%a %-d %b %-I:%M %p").replace(" 0", " ")
    return f"{when}  {flight['dest']}  ({flight['seats']})"


def find_matches(flights: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(PACIFIC)
    live = [f for f in flights if upcoming(f, now)]
    tcm = [f for f in live if f.get("origin") == "tcm"]
    suu = [f for f in live if f.get("origin") == "suu"]
    matches = []

    for f in tcm:
        if is_hawaii(f):
            matches.append({
                "id": f"direct:{f['id']}",
                "kind": "direct",
                "summary": f"JBLM → Hawaii — {fmt(f)}",
                "flights": [f],
            })

    feeders = [f for f in tcm if is_travis(f)]
    hawaii_legs = [f for f in suu if is_hawaii(f)]
    for hop in hawaii_legs:
        hop_at = roll_at(hop)
        viable = [f for f in feeders if hop_at - roll_at(f) >= MIN_CONNECT]
        if viable:
            first = min(viable, key=roll_at)
            gap = hop_at - roll_at(first)
            hours = int(gap.total_seconds() // 3600)
            matches.append({
                "id": f"connect:{first['id']}>{hop['id']}",
                "kind": "connect",
                "summary": (
                    f"JBLM → Travis → Hawaii — {hours}h window\n"
                    f"  1. JBLM → Travis: {fmt(first)}\n"
                    f"  2. Travis → Hawaii: {fmt(hop)}"
                ),
                "flights": [first, hop],
            })
        else:
            matches.append({
                "id": f"travis-hawaii:{hop['id']}",
                "kind": "travis-only",
                "summary": (
                    f"Travis → Hawaii is on the board, but no JBLM → Travis "
                    f"feeder with ≥6h before roll call.\n"
                    f"  Travis → Hawaii: {fmt(hop)}"
                ),
                "flights": [hop],
            })
    return matches


def render_comment(matches: list[dict], as_of: str) -> str:
    lines = [
        f"## Hawaii watch — new on the board",
        "",
        f"Outlook: {as_of}",
        "",
        "Looking for **JBLM → Hawaii**, or **JBLM → Travis** then **Travis → Hawaii** with at least 6 hours between roll calls.",
        "",
    ]
    for m in matches:
        if m["kind"] == "direct":
            lines.append(f"- **Direct** {m['summary']}")
        elif m["kind"] == "connect":
            lines.append(f"- **Connection** {m['summary']}")
        else:
            lines.append(f"- **Travis only** {m['summary']}")
    lines += [
        "",
        "Unofficial. Call the recording and make roll call. Seats are never guaranteed.",
        "",
        "https://spaceafinder.com/",
    ]
    return "\n".join(lines)


def self_test() -> None:
    now = datetime(2026, 9, 12, 16, 0, tzinfo=PACIFIC)
    flights = [
        {"id": "tcm-a", "origin": "tcm", "date": "2026-09-13", "roll": "07:00",
         "dest": "Hickam AFB, HI", "destKey": "hickam hawaii", "seats": "40F"},
        {"id": "tcm-b", "origin": "tcm", "date": "2026-09-13", "roll": "06:00",
         "dest": "Travis AFB, CA", "destKey": "travis suu", "seats": "20T"},
        {"id": "suu-a", "origin": "suu", "date": "2026-09-13", "roll": "15:35",
         "dest": "JB Pearl Harbor-Hickam, HI", "destKey": "hickam hawaii", "seats": "41F"},
        {"id": "suu-b", "origin": "suu", "date": "2026-09-12", "roll": "06:40",
         "dest": "Hickam, HI", "destKey": "hawaii", "seats": "73T"},
        {"id": "tcm-c", "origin": "tcm", "date": "2026-09-13", "roll": "14:00",
         "dest": "Travis AFB, CA", "destKey": "travis", "seats": "10T"},
    ]
    kinds = {m["kind"] for m in find_matches(flights, now)}
    ids = {m["id"] for m in find_matches(flights, now)}
    assert "direct" in kinds
    assert "connect" in kinds
    assert "direct:tcm-a" in ids
    assert "connect:tcm-b>suu-a" in ids
    assert "connect:tcm-c>suu-a" not in ids  # only 1h35m
    assert not any("suu-b" in m["id"] for m in find_matches(flights, now))  # already passed
    print("hawaii_watch self-test ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--comment", type=Path, help="Write issue comment markdown here if new matches")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0

    board = json.loads(BOARD.read_text())
    matches = find_matches(board.get("flights") or [])
    state = {"notifiedIds": []}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    notified = set(state.get("notifiedIds") or [])
    live_ids = [m["id"] for m in matches]
    new = [m for m in matches if m["id"] not in notified]
    STATE.write_text(json.dumps({
        "notifiedIds": sorted(set(notified) | set(live_ids)),
        "lastMatches": live_ids,
    }, indent=2) + "\n")

    print(f"hawaii_watch matches={len(matches)} new={len(new)}")
    for m in matches:
        print(" -", m["kind"], m["id"])
    if new and args.comment:
        args.comment.write_text(render_comment(new, board.get("asOfLabel") or ""))
        print(f"wrote {args.comment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
