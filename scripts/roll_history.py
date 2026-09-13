#!/usr/bin/env python3
"""Parse 24-hour roll-call PDFs and keep a growing history of seats released."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_flights import (  # noqa: E402
    MONTH_ABBR,
    PACIFIC,
    TRAVIS_DIR,
    fetch_pdf_bytes,
    month_num,
    pdf_text,
    pretty_dest,
    slug,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "js" / "history.json"
TCM_ROLL = (
    "https://www.amc.af.mil/Portals/12/AMC%20Tvl%20Pg/Passenger%20Terminals/"
    "AMC%20CONUS%20Terminals/Joint%20Base%20Lewis-McChord%20Passenger%20Terminal/TCM_RollCall.pdf"
)
KEEP_DAYS = 400

DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|"
    r"JUL(?:Y)?|AUG(?:UST)?|SEP(?:T|TEMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"
    r"\s+(20\d{2})\b",
    re.I,
)
STATS_RE = re.compile(
    r"\b(\d{1,3})(?:[FT])?\s+(\d{1,3})\s+(VI|IV|V|III|II|I|-)",
    re.I,
)
NO_FLIGHTS = re.compile(r"NO FLIGHTS|NO SCHEDULED|NO ROLL CALL", re.I)
HEADER_RE = re.compile(
    r"FLIGHT/?SEAT RELEASE INFORMATION|PAX SELECTED|COMPETED FOR FLIGHT|"
    r"Date & Time|Sign-up|# PAX|Lowest\s*Category|Seats\s*Released|Seats\s*Used|"
    r"Travis Passenger Terminal|McChord Field Passenger Terminal|"
    r"24-Hour Space-A Roll-Call Report",
    re.I,
)


def parse_rollcall(text: str, origin: str) -> list[dict]:
    text = HEADER_RE.sub(" ", text.replace("\u00a0", " "))
    matches = list(DATE_RE.finditer(text))
    rows = []
    for i, m in enumerate(matches):
        d, mon, y = int(m.group(1)), m.group(2), int(m.group(3))
        date = f"{y:04d}-{month_num(mon):02d}-{d:02d}"
        chunk = text[m.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        if NO_FLIGHTS.search(chunk) and not STATS_RE.search(chunk):
            continue
        flat = re.sub(r"[ \t]*\n[ \t]*", " ", chunk)
        last = 0
        for sm in STATS_RE.finditer(flat):
            dest_raw = pretty_dest(flat[last:sm.start()])
            last = sm.end()
            if len(dest_raw) < 4 or re.match(r"^\d{1,2}\s+[A-Z]", dest_raw, re.I):
                continue
            released, used, cat = sm.group(1), sm.group(2), sm.group(3).upper()
            if cat == "-":
                cat = ""
            rows.append({
                "id": f"{origin}-{date}-{slug(dest_raw)}-{released}",
                "origin": origin,
                "date": date,
                "dest": dest_raw,
                "released": f"{released}",
                "used": used,
                "cat": cat,
                "source": "rollcall",
            })
    return rows


def archive_passed_board(flights: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(PACIFIC)
    rows = []
    for f in flights:
        y, mo, d = map(int, f["date"].split("-"))
        hh, mm = map(int, (f.get("roll") or "00:00").split(":"))
        when = datetime(y, mo, d, hh, mm, tzinfo=PACIFIC)
        if when > now:
            continue
        rows.append({
            "id": f"{f['origin']}-{f['date']}-{slug(f['dest'])}-{f.get('seats', '')}",
            "origin": f["origin"],
            "date": f["date"],
            "roll": f.get("roll", ""),
            "dest": f["dest"],
            "released": f.get("seats", ""),
            "used": "",
            "cat": "",
            "source": f.get("horizon") or "72hr",
        })
    return rows


def merge_history(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_id = {r["id"]: r for r in existing}
    for row in incoming:
        old = by_id.get(row["id"])
        if not old:
            by_id[row["id"]] = row
            continue
        if row.get("source") == "rollcall" or (row.get("used") and not old.get("used")):
            merged = {**old, **{k: v for k, v in row.items() if v}}
            by_id[row["id"]] = merged
    cutoff = (datetime.now(PACIFIC) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    rows = [r for r in by_id.values() if r.get("date", "") >= cutoff]
    rows.sort(key=lambda r: (r.get("date", ""), r.get("origin", ""), r.get("dest", "")), reverse=True)
    return rows


def travis_rollcall_urls(days: int = 4) -> list[str]:
    now = datetime.now(PACIFIC)
    urls = []
    for i in range(days):
        d = now - timedelta(days=i)
        stamp = f"{d.day:02d}{MONTH_ABBR[d.month - 1]}{str(d.year)[2:]}"
        urls.append(TRAVIS_DIR + f"TRAVIS_ROLLCALL_{stamp}.pdf")
    return urls


def load_json(path: Path) -> dict:
    if not path.exists():
        return {"rows": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"rows": []}


def self_test() -> None:
    fixture = Path(__file__).with_name("fixtures") / "travis_rollcall.txt"
    rows = parse_rollcall(fixture.read_text(), "suu")
    dests = [r["dest"] for r in rows]
    assert any("Hickam" in d for d in dests), dests
    hickam = next(r for r in rows if "Hickam" in r["dest"] and r["date"] == "2026-09-09")
    assert hickam["released"] == "73"
    assert hickam["used"] == "15"
    assert hickam["cat"] == "VI"
    tulsa = next(r for r in rows if "Tulsa" in r["dest"])
    assert tulsa["used"] == "0"
    elm = next(r for r in rows if r["date"] == "2026-09-10")
    assert "Elmendorf" in elm["dest"]
    assert elm["used"] == "1"
    print("roll_history self-test ok", len(rows), "rows")


def fetch_one(url: str, save: bool = False) -> str | None:
    try:
        data = fetch_pdf_bytes(url, save=save, max_age_days=14)
    except Exception as exc:
        print(f"skip {url.rsplit('/', 1)[-1]}: {exc}")
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = Path(tmp.name)
        path.write_bytes(data)
    try:
        return pdf_text(path)
    finally:
        path.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0

    existing = load_json(args.out).get("rows") or []
    incoming: list[dict] = []

    board_path = ROOT / "js" / "flights.json"
    if board_path.exists():
        board = json.loads(board_path.read_text())
        incoming.extend(archive_passed_board(board.get("flights") or []))

    tcm_text = fetch_one(TCM_ROLL, save=True)
    if tcm_text:
        rows = parse_rollcall(tcm_text, "tcm")
        incoming.extend(rows)
        print("McChord roll-call rows", len(rows))

    for i, url in enumerate(travis_rollcall_urls()):
        text = fetch_one(url, save=(i == 0))
        if not text:
            continue
        rows = parse_rollcall(text, "suu")
        print(f"Travis roll-call {url.rsplit('/', 1)[-1]} rows={len(rows)}")
        incoming.extend(rows)
        break

    merged = merge_history(existing, incoming)
    payload = {
        "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": merged,
    }
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out} rows={len(merged)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"roll_history failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        raise SystemExit(1)
