#!/usr/bin/env python3
"""Pull official 72-hour PDFs and write js/flights.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    "Referer": TRAVIS_PAGE,
}


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
            wrapped = f"({word[1:-1].upper()})" if word.startswith("(") and word.endswith(")") else word.upper() if word.isupper() or word.upper() in KEEP_UPPER else word
            if word.startswith("(") and word.endswith(")"):
                parts.append(f"({up})")
            else:
                parts.append(up)
        else:
            parts.append(word.title())
    out = " ".join(parts).replace(" ,", ",")
    out = re.sub(r"\bMguire\b", "McGuire", out, flags=re.I)
    out = re.sub(r"\bMcchord\b", "McChord", out, flags=re.I)
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


def parse_outlook(text: str, origin: str, label_name: str, source_url: str) -> dict:
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
            roll = hhmm(roll_raw)
            mmdd = date[5:7] + date[8:10]
            flights.append({
                "id": f"{origin}-{mmdd}-{slug(dest)}-{roll_raw.zfill(4)}",
                "origin": origin,
                "date": date,
                "roll": roll,
                "dest": dest,
                "destKey": dest_key(dest + " " + dest_raw),
                "seats": seats,
                "kind": kind_for(seats),
                "note": note_for(seats, kind_for(seats)),
            })
    return {
        "asOf": as_of_iso,
        "asOfLabel": as_of_label,
        "sourceUrl": source_url,
        "origin": origin,
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


def fetch_pdf_bytes(url: str) -> bytes:
    errors: list[str] = []
    try:
        data, _ = fetch_url(url, timeout=45)
        if data.startswith(b"%PDF"):
            return data
        errors.append("direct: not a PDF")
    except Exception as exc:
        errors.append(f"direct: {exc}")

    try:
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
        if data.startswith(b"%PDF"):
            return data
        errors.append("wayback: snapshot was not a PDF")
    except Exception as exc:
        errors.append(f"wayback: {exc}")

    raise RuntimeError(f"download failed for {url} ({'; '.join(errors)})")


def travis_candidate_urls(days: int = 8) -> list[str]:
    now = datetime.now(PACIFIC)
    urls = []
    for i in range(days):
        d = now - timedelta(days=i)
        stamp = f"{d.day:02d}{MONTH_ABBR[d.month - 1]}{str(d.year)[2:]}"
        urls.append(TRAVIS_DIR + f"TRAVIS_72HRS_{stamp}.pdf")
    return urls


def fetch_travis_pdf() -> tuple[bytes, str]:
    last_err: Exception | None = None
    for url in travis_candidate_urls():
        try:
            data = fetch_pdf_bytes(url)
            return data, url
        except Exception as exc:
            last_err = exc
            continue
    raise RuntimeError(f"No recent Travis 72-hour PDF found ({last_err})")


def merge_boards(boards: list[dict]) -> dict:
    flights = []
    labels = []
    sources = []
    as_ofs = []
    for board in boards:
        flights.extend(board["flights"])
        labels.append(board["asOfLabel"])
        sources.append({"origin": board["origin"], "url": board["sourceUrl"], "asOfLabel": board["asOfLabel"]})
        as_ofs.append(board["asOf"])
    flights.sort(key=lambda f: (f["date"], f["roll"], f["origin"]))
    return {
        "asOf": max(as_ofs) if as_ofs else "",
        "asOfLabel": "  ·  ".join(labels),
        "sourceUrl": sources[0]["url"] if sources else "",
        "sources": sources,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flights": flights,
    }


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
    print("self-test ok", "tcm", len(tcm["flights"]), "travis", len(travis["flights"]))


def load_terminal(origin: str, label: str, url: str, text: str) -> dict:
    board = parse_outlook(text, origin=origin, label_name=label, source_url=url)
    print(f"{label}: {len(board['flights'])} flights · {board['asOfLabel']}")
    return board


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
            data = fetch_pdf_bytes(TCM_PDF)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                tmp_path.write_bytes(data)
            try:
                boards.append(load_terminal("tcm", "McChord", TCM_PDF, pdf_text(tmp_path)))
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"McChord: {exc}")
            print(f"McChord failed: {exc}", file=sys.stderr)

        try:
            data, url = fetch_travis_pdf()
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                tmp_path.write_bytes(data)
            try:
                boards.append(load_terminal("suu", "Travis", url, pdf_text(tmp_path)))
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"Travis: {exc}")
            print(f"Travis failed: {exc}", file=sys.stderr)

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
