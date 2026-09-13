#!/usr/bin/env python3
"""Pull official 72-hour and Travis 30-day PDFs and write js/flights.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "js" / "flights.json"
PACIFIC = ZoneInfo("America/Los_Angeles")

TCM_PDF = (
    "https://www.amc.af.mil/Portals/12/AMC%20Tvl%20Pg/Passenger%20Terminals/"
    "AMC%20CONUS%20Terminals/Joint%20Base%20Lewis-McChord%20Passenger%20Terminal/TCM72hr.pdf"
)
TRAVIS_DIR = (
    "https://www.amc.af.mil/Portals/12/AMC%20Tvl%20Pg/Passenger%20Terminals/"
    "AMC%20CONUS%20Terminals/Travis%20Passenger%20Terminal/"
)
TRAVIS_PAGE = (
    "https://www.amc.af.mil/AMC-Travel-Site/Terminals/CONUS-Terminals/"
    "Travis-AFB-Passenger-Terminal/"
)

MONTHS = {
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4, "MAY": 5, "JUN": 6, "JUNE": 6, "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8, "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11, "DEC": 12, "DECEMBER": 12,
}
MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
DAY_RE = re.compile(
    r"\b(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\s*,?\s*"
    r"(\d{1,2})\s+([A-Z]+)\s+(20\d{2})\b",
    re.I,
)
ASOF_RE = re.compile(
    r"CURRENT AS OF[:\s]+(\d{1,2})\s+([A-Z]+)\s+(20\d{2})\s*,\s*(\d{3,4})\s*L",
    re.I,
)
FLIGHT_RE = re.compile(
    r"\b(\d{3,4})\s+([A-Z0-9(*][A-Z0-9 .,'/&()*-]{1,160}?)\s+(TBD|VRC|SP|\d+[FT])\b",
    re.I,
)
JUNK_DEST = re.compile(
    r"roll call|destination|seats|daily|subject|notice|information|official|terminal|"
    r"departures|no scheduled",
    re.I,
)
CLEAN_LINE_RE = re.compile(
    r"(ROLL\s*CALL(?:\s+DESTINATIONS?)?(?:\s+SEATS)?|"
    r"\*{0,2}NO SCHEDULED FLIGHTS\*{0,2}|"
    r"0?600\s*[-–]\s*1800 daily\.?|"
    r"ALL FLIGHTS SCHEDULES ARE SUBJECT TO CHANGE WITHOUT NOTICE|"
    r"The McChord(?: Field)? Passenger Terminal is open to Official Business only\.|"
    r"DEPARTURES:\s+TRAVIS AFB, CA \(SUU\)|"
    r"DEPARTURES\s+JOINT BASE LEWIS-MCCHORD \(TCM\))",
    re.I,
)
STOP_SPLIT = re.compile(
    r"(,\s*(?:[A-Z]{2}|Guam|Japan|Korea|Germany|Italy|UK|England))\s+(?=[A-Z])",
    re.I,
)
HEADER_YEAR_RE = re.compile(
    r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|"
    r"OCTOBER|NOVEMBER|DECEMBER)\s+(20\d{2})\b",
    re.I,
)
PE_ROW_RE = re.compile(
    r"([A-Z][A-Z0-9 .,'/&()*-]{2,80}?),\s*"
    r"([A-Z]{2}|GUAM|JAPAN|KOREA|GERMANY|ITALY|ENGLAND|UK)"
    r"\s*(\d{1,2})\s+"
    r"(JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUN(?:E)?|"
    r"JUL(?:Y)?|AUG(?:UST)?|SEP(?:T|TEMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"
    r"\s*/\s*(\d{3,4})\s*L",
    re.I,
)
KEEP_UPPER = {
    "AFB", "AB", "NAS", "MCAS", "INTL", "JB", "JRB", "ANGB", "ARB", "FLD", "FIELD",
    "TX", "WA", "OR", "CA", "AK", "HI", "AZ", "NM", "OK", "KS", "MO", "AR",
    "LA", "MS", "AL", "GA", "FL", "SC", "NC", "VA", "MD", "DE", "PA", "NJ",
    "NY", "CT", "RI", "MA", "NH", "VT", "ME", "OH", "IN", "IL", "MI", "WI",
    "MN", "IA", "ND", "SD", "NE", "CO", "UT", "NV", "ID", "MT", "WY", "TN",
    "KY", "WV", "DC", "GU", "JP", "KR", "UK", "QA",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/json,application/octet-stream,*/*;q=0.8",
    "Referer": TRAVIS_PAGE,
}

_SAVES = 0
MAX_SAVES = 3


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def pretty_dest(raw: str) -> str:
    raw = re.sub(r"[*]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" ,/")
    parts = []
    for word in raw.replace(",", " , ").split():
        up = word.upper().strip("()")
        if word == ",":
            parts.append(",")
        elif up in KEEP_UPPER:
            if word.startswith("(") and word.endswith(")"):
                parts.append(f"({up})")
            else:
                parts.append(up)
        else:
            parts.append(word.title())
    out = " ".join(parts).replace(" ,", ",")
    out = re.sub(r"\bMguire\b", "McGuire", out, flags=re.I)
    out = re.sub(r"\bMcchord\b", "McChord", out, flags=re.I)
    out = re.sub(r"\bTra Vis\b", "Travis", out, flags=re.I)
    out = STOP_SPLIT.sub(r"\1 / ", out)
    out = re.sub(r"\s*/\s*", " / ", out)
    return out.strip(" /")


def dest_key(dest: str) -> str:
    extra = ""
    if "patriot" in dest.lower():
        extra += " patriot express rotator hickam"
    if "hickam" in dest.lower() or "pearl" in dest.lower():
        extra += " hawaii honolulu"
    return (dest + extra).lower()


def kind_for(seats: str) -> str:
    s = seats.upper()
    if s.endswith("F") and s[0].isdigit():
        return "firm"
    if s.endswith("T") and s[0].isdigit():
        return "tent"
    return "tbd"


def note_for(seats: str, kind: str) -> str:
    if kind == "firm":
        n = seats[:-1]
        return f"{n} firm seat" + ("" if n == "1" else "s") + " posted"
    if kind == "tent":
        return f"{seats[:-1]} tentative seats posted"
    if seats.upper() == "TBD":
        return "Seat count unpublished"
    return seats


def hhmm(raw: str) -> str:
    raw = raw.zfill(4)
    return f"{raw[:2]}:{raw[2:]}"


def month_num(name: str) -> int:
    name = name.upper()
    if name == "SEPT":
        name = "SEP"
    return MONTHS.get(name) or MONTHS[name[:3]]


def flight_row(origin: str, date: str, roll_raw: str, dest: str, dest_raw: str, seats: str, horizon: str) -> dict:
    seats = seats.upper()
    return {
        "id": f"{origin}-{date[5:7]}{date[8:10]}-{slug(dest)}-{roll_raw.zfill(4)}",
        "origin": origin,
        "date": date,
        "roll": hhmm(roll_raw),
        "dest": dest,
        "destKey": dest_key(dest + " " + dest_raw),
        "seats": seats,
        "kind": kind_for(seats),
        "note": note_for(seats, kind_for(seats)),
        "horizon": horizon,
    }


def parse_outlook(text: str, origin: str, label_name: str, source_url: str, horizon: str = "72hr") -> dict:
    text = text.replace("\u00a0", " ")
    asof_m = ASOF_RE.search(text)
    day_matches = list(DAY_RE.finditer(text))
    if asof_m:
        day, mon, year, hhmm_raw = asof_m.group(1), asof_m.group(2).upper(), asof_m.group(3), asof_m.group(4)
        mo = month_num(mon)
        hhmm_raw = hhmm_raw.zfill(4)
        as_of_label = f"{label_name} · {int(day)} {mon.title()[:3]} {year}, {hhmm_raw}L"
        as_of_iso = datetime(int(year), mo, int(day), int(hhmm_raw[:2]), int(hhmm_raw[2:])).isoformat() + "-07:00"
    elif day_matches:
        d, mon_name, y = int(day_matches[0].group(2)), day_matches[0].group(3), int(day_matches[0].group(4))
        mo = month_num(mon_name)
        as_of_label = f"{label_name} · {d} {mon_name.title()[:3]} {y}"
        as_of_iso = datetime(y, mo, d, 6, 0).isoformat() + "-07:00"
    else:
        raise ValueError(f"Could not find dates on the {label_name} 72-hour PDF")

    flights = []
    for i, m in enumerate(day_matches):
        d, mon_name, y = int(m.group(2)), m.group(3).upper(), int(m.group(4))
        mo = month_num(mon_name)
        date = f"{y:04d}-{mo:02d}-{d:02d}"
        start = m.end()
        end = day_matches[i + 1].start() if i + 1 < len(day_matches) else len(text)
        chunk = CLEAN_LINE_RE.sub(" ", text[start:end])
        flat = re.sub(r"[ \t]*\n[ \t]*", " ", chunk)
        for fm in FLIGHT_RE.finditer(flat):
            roll_raw, dest_raw, seats = fm.group(1), fm.group(2), fm.group(3).upper()
            if JUNK_DEST.search(dest_raw):
                continue
            dest = pretty_dest(dest_raw)
            if len(dest) < 4:
                continue
            flights.append(flight_row(origin, date, roll_raw, dest, dest_raw, seats, horizon))
    return {
        "asOf": as_of_iso,
        "asOfLabel": as_of_label,
        "sourceUrl": source_url,
        "origin": origin,
        "horizon": horizon,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flights": flights,
    }


def parse_30day(text: str, origin: str, label_name: str, source_url: str) -> dict:
    """Monthly Patriot Express calendar: 'HICKAM, HI12 SEPT / 1535L'."""
    flat = re.sub(r"[ \t]*\n[ \t]*", " ", text.replace("\u00a0", " "))
    hm = HEADER_YEAR_RE.search(flat)
    if not hm:
        raise ValueError(f"Could not find month/year on the {label_name} 30-day PDF")
    header_month = month_num(hm.group(1))
    header_year = int(hm.group(2))
    as_of_label = f"{label_name} · {hm.group(1).title()[:3]} {header_year}"
    as_of_iso = datetime(header_year, header_month, 1, 8, 0).isoformat() + "-07:00"
    flights = []
    for m in PE_ROW_RE.finditer(flat):
        dest_raw = f"{m.group(1).strip()}, {m.group(2).strip()}"
        if "hickam" in dest_raw.lower() and "patriot" not in dest_raw.lower():
            dest_raw = dest_raw + " (Patriot Express)"
        d = int(m.group(3))
        mo = month_num(m.group(4))
        y = header_year + 1 if mo < header_month else header_year
        date = f"{y:04d}-{mo:02d}-{d:02d}"
        dest = pretty_dest(dest_raw)
        flights.append(flight_row(origin, date, m.group(5), dest, dest_raw, "TBD", "30day"))
    if not flights:
        raise ValueError(f"No Patriot Express rows on the {label_name} 30-day PDF")
    return {
        "asOf": as_of_iso,
        "asOfLabel": as_of_label,
        "sourceUrl": source_url,
        "origin": origin,
        "horizon": "30day",
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flights": flights,
    }


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def fetch_url(url: str, timeout: int = 60) -> tuple[bytes, str]:
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.geturl()


def wayback_existing(url: str, max_age_days: int = 21) -> bytes | None:
    api = "https://archive.org/wayback/available?url=" + quote(url, safe="")
    data, _ = fetch_url(api, timeout=25)
    info = json.loads(data.decode("utf-8", "replace"))
    snap = (info.get("archived_snapshots") or {}).get("closest") or {}
    if not snap.get("available") or not snap.get("timestamp"):
        return None
    ts = snap["timestamp"]
    try:
        when = datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if datetime.utcnow() - when > timedelta(days=max_age_days):
        return None
    raw = f"https://web.archive.org/web/{ts}id_/{url.split('?')[0]}"
    blob, _ = fetch_url(raw, timeout=60)
    return blob if blob.startswith(b"%PDF") else None


def wayback_save(url: str) -> bytes | None:
    global _SAVES
    if _SAVES >= MAX_SAVES:
        raise RuntimeError("wayback save budget exhausted")
    _SAVES += 1
    time.sleep(1.0)
    data, final = fetch_url("https://web.archive.org/save/" + url, timeout=90)
    stamp = None
    m = re.search(r"/web/(\d{14})/", final)
    if m:
        stamp = m.group(1)
    if not stamp:
        m = re.search(r"/web/(\d{14})/", data.decode("utf-8", "replace"))
        if m:
            stamp = m.group(1)
    if stamp:
        raw_url = f"https://web.archive.org/web/{stamp}id_/{url.split('?')[0]}"
        data, _ = fetch_url(raw_url, timeout=60)
    return data if data.startswith(b"%PDF") else None


def fetch_pdf_bytes(url: str, save: bool = True, max_age_days: int = 21) -> bytes:
    errors: list[str] = []
    try:
        data, _ = fetch_url(url, timeout=30)
        if data.startswith(b"%PDF"):
            return data
        errors.append("direct: not a PDF")
    except Exception as exc:
        errors.append(f"direct: {exc}")

    try:
        data = wayback_existing(url, max_age_days=max_age_days)
        if data:
            return data
        errors.append("wayback: no recent snapshot")
    except Exception as exc:
        errors.append(f"wayback: {exc}")

    if save:
        try:
            data = wayback_save(url)
            if data:
                return data
            errors.append("save: not a PDF")
        except Exception as exc:
            errors.append(f"save: {exc}")

    raise RuntimeError(f"download failed for {url} ({'; '.join(errors)})")


def dated_travis(prefix: str, days: int, year4: bool = False) -> list[str]:
    now = datetime.now(PACIFIC)
    urls = []
    for i in range(days):
        d = now - timedelta(days=i)
        yy = str(d.year) if year4 else str(d.year)[2:]
        stamp = f"{d.day:02d}{MONTH_ABBR[d.month - 1]}{yy}"
        urls.append(TRAVIS_DIR + f"{prefix}_{stamp}.pdf")
    return urls


def travis_candidate_urls(days: int = 3) -> list[str]:
    return dated_travis("TRAVIS_72HRS", days)


def travis_30day_urls() -> list[str]:
    now = datetime.now(PACIFIC)
    urls = []
    for months_ago in range(2):
        month = now.month - months_ago
        year = now.year
        if month <= 0:
            month += 12
            year -= 1
        stamp = f"01{MONTH_ABBR[month - 1]}{year}"
        urls.append(TRAVIS_DIR + f"TRAVIS_30DAY_{stamp}.pdf")
    urls.append(TRAVIS_DIR + "TRAVIS_30DAY.pdf")
    return urls


def fetch_first(urls: list[str], save_first: int = 2, max_age_days: int = 21) -> tuple[bytes, str]:
    last_err: Exception | None = None
    for i, url in enumerate(urls):
        try:
            return fetch_pdf_bytes(url, save=(i < save_first), max_age_days=max_age_days), url
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"No PDF found ({last_err})")


def fetch_travis_pdf() -> tuple[bytes, str]:
    return fetch_first(travis_candidate_urls(), save_first=2, max_age_days=10)


def fetch_travis_30day() -> tuple[bytes, str]:
    return fetch_first(travis_30day_urls(), save_first=1, max_age_days=40)


def merge_boards(boards: list[dict]) -> dict:
    seen: dict[tuple, dict] = {}
    keys: list[tuple] = []
    labels = []
    sources = []
    as_ofs = []
    for board in boards:
        labels.append(board["asOfLabel"])
        sources.append({
            "origin": board["origin"],
            "url": board["sourceUrl"],
            "asOfLabel": board["asOfLabel"],
            "horizon": board.get("horizon", "72hr"),
        })
        as_ofs.append(board["asOf"])
        for f in board["flights"]:
            key = (f["origin"], f["date"], f["roll"])
            prev = seen.get(key)
            if prev and prev.get("horizon") == "72hr" and f.get("horizon") == "30day":
                continue
            if key not in seen:
                keys.append(key)
            seen[key] = f
    flights = [seen[k] for k in keys]
    flights.sort(key=lambda f: (f["date"], f["roll"], f["origin"]))
    return {
        "asOf": max(as_ofs) if as_ofs else "",
        "asOfLabel": "  ·  ".join(labels),
        "sourceUrl": sources[0]["url"] if sources else "",
        "sources": sources,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flights": flights,
    }


def asof_fresh(as_of: str, days: int = 5) -> bool:
    if not as_of:
        return False
    try:
        dt = datetime.fromisoformat(as_of)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PACIFIC)
    return datetime.now(PACIFIC) - dt.astimezone(PACIFIC) <= timedelta(days=days)


def keep_previous(boards: list[dict], origin: str, label: str) -> None:
    if any(b.get("origin") == origin and b.get("horizon") != "30day" for b in boards):
        return
    if not OUT.exists():
        return
    try:
        old = json.loads(OUT.read_text())
    except json.JSONDecodeError:
        return
    flights = [
        f for f in old.get("flights", [])
        if f.get("origin") == origin and f.get("horizon") != "30day"
    ]
    if not flights:
        return
    src = next((s for s in old.get("sources", []) if s.get("origin") == origin and s.get("horizon") != "30day"), None)
    if src is None:
        src = next((s for s in old.get("sources", []) if s.get("origin") == origin), None)
    boards.append({
        "origin": origin,
        "horizon": "72hr",
        "asOf": old.get("asOf") or "",
        "asOfLabel": (src or {}).get("asOfLabel") or f"{label} · last good",
        "sourceUrl": (src or {}).get("url") or "",
        "fetchedAt": old.get("fetchedAt") or "",
        "flights": flights,
    })
    print(f"kept previous {label} ({len(flights)} flights)", file=sys.stderr)


def write_board(board: dict, path: Path) -> bool:
    payload = {
        "asOf": board["asOf"],
        "asOfLabel": board["asOfLabel"],
        "sourceUrl": board["sourceUrl"],
        "sources": board.get("sources", []),
        "fetchedAt": board["fetchedAt"],
        "flights": board["flights"],
    }
    new = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    old = path.read_text() if path.exists() else ""

    def canon(raw: str) -> str:
        try:
            obj = json.loads(raw)
        except Exception:
            return raw
        obj.pop("fetchedAt", None)
        return json.dumps(obj, sort_keys=True)

    changed = canon(old) != canon(new)
    if changed or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new)
    return changed


def self_test() -> None:
    tcm = parse_outlook(
        (Path(__file__).with_name("fixtures") / "tcm72.txt").read_text(),
        origin="tcm", label_name="McChord", source_url=TCM_PDF,
    )
    dests = [f["dest"] for f in tcm["flights"]]
    assert tcm["asOfLabel"].startswith("McChord · 12 Sep 2026"), tcm["asOfLabel"]
    assert dests == [
        "McGuire AFB, NJ",
        "JB Elmendorf, AK",
        "JB Elmendorf, AK",
        "JB Elmendorf, AK",
    ], dests
    travis = parse_outlook(
        (Path(__file__).with_name("fixtures") / "travis72.txt").read_text(),
        origin="suu", label_name="Travis", source_url="travis",
    )
    tdests = [(f["roll"], f["dest"], f["seats"]) for f in travis["flights"]]
    assert tdests[0][0] == "06:40"
    assert "Hickam" in tdests[0][1] and "Osan" in tdests[0][1]
    assert tdests[0][2] == "73T"
    assert tdests[1] == ("10:10", "Bangor INTL, ME", "19T")
    assert "Patriot" in tdests[2][1]
    assert tdests[2][2] == "41F"
    assert travis["flights"][3]["dest"].startswith("JB Elmendorf")
    pe = parse_30day(
        (Path(__file__).with_name("fixtures") / "travis30.txt").read_text(),
        origin="suu", label_name="Travis PE", source_url="30day",
    )
    rows = [(f["date"], f["roll"], f["dest"], f["seats"], f["horizon"]) for f in pe["flights"]]
    assert rows[0][0] == "2026-09-12" and rows[0][1] == "15:35", rows
    assert "Hickam" in rows[0][2] and "Patriot" in rows[0][2]
    assert rows[0][3] == "TBD" and rows[0][4] == "30day"
    assert rows[1][0] == "2026-09-26" and rows[1][1] == "15:35"
    merged = merge_boards([travis, pe])
    pe_only = [f for f in merged["flights"] if f["horizon"] == "30day"]
    assert any(f["date"] == "2026-09-26" for f in pe_only), pe_only
    # 12 Sep 1535 PE from 72h (41F) wins over 30-day TBD
    hit = next(f for f in merged["flights"] if f["date"] == "2026-09-12" and f["roll"] == "15:35")
    assert hit["seats"] == "41F" and hit["horizon"] == "72hr", hit
    print("self-test ok", "tcm", len(tcm["flights"]), "travis", len(travis["flights"]), "pe", len(pe["flights"]))


def load_terminal(origin: str, label: str, url: str, text: str, horizon: str = "72hr") -> dict:
    if horizon == "30day":
        board = parse_30day(text, origin=origin, label_name=label, source_url=url)
    else:
        board = parse_outlook(text, origin=origin, label_name=label, source_url=url, horizon=horizon)
    print(f"{label}: {len(board['flights'])} flights · {board['asOfLabel']}")
    return board


def load_from_bytes(origin: str, label: str, url: str, data: bytes, horizon: str = "72hr") -> dict:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = Path(tmp.name)
        path.write_bytes(data)
    try:
        return load_terminal(origin, label, url, pdf_text(path), horizon=horizon)
    finally:
        path.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--pdf", type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.fixture or args.pdf:
        text = args.fixture.read_text() if args.fixture else pdf_text(args.pdf)
        board = merge_boards([load_terminal("tcm", "McChord", TCM_PDF, text)])
    else:
        boards: list[dict] = []
        errors: list[str] = []
        try:
            data = fetch_pdf_bytes(TCM_PDF, save=True, max_age_days=7)
            board = load_from_bytes("tcm", "McChord", TCM_PDF, data)
            if not asof_fresh(board.get("asOf", "")):
                print(f"McChord snapshot stale ({board.get('asOfLabel')})", file=sys.stderr)
                keep_previous(boards, "tcm", "McChord")
            else:
                boards.append(board)
        except Exception as exc:
            errors.append(f"McChord: {exc}")
            print(f"McChord failed: {exc}", file=sys.stderr)
            keep_previous(boards, "tcm", "McChord")

        try:
            data, url = fetch_travis_pdf()
            board = load_from_bytes("suu", "Travis", url, data)
            if not asof_fresh(board.get("asOf", ""), days=3):
                print(f"Travis snapshot stale ({board.get('asOfLabel')})", file=sys.stderr)
                keep_previous(boards, "suu", "Travis")
            else:
                boards.append(board)
        except Exception as exc:
            errors.append(f"Travis: {exc}")
            print(f"Travis failed: {exc}", file=sys.stderr)
            keep_previous(boards, "suu", "Travis")

        try:
            data, url = fetch_travis_30day()
            boards.append(load_from_bytes("suu", "Travis PE", url, data, horizon="30day"))
        except Exception as exc:
            print(f"Travis 30-day skipped: {exc}", file=sys.stderr)

        if not boards:
            raise RuntimeError("No terminals updated (" + "; ".join(errors) + ")")
        board = merge_boards(boards)

    if args.dry_run:
        print(json.dumps({k: v for k, v in board.items() if not k.startswith("_")}, indent=2))
        return 0
    changed = write_board(board, args.out)
    print(f"wrote {args.out} flights={len(board['flights'])} asOf={board['asOfLabel']} changed={changed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"update_flights failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
