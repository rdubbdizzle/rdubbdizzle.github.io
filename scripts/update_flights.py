#!/usr/bin/env python3
"""Pull the official McChord 72-hour PDF and write js/flights.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "js" / "flights.json"
PDF_URL = (
    "https://www.amc.af.mil/Portals/12/AMC%20Tvl%20Pg/Passenger%20Terminals/"
    "AMC%20CONUS%20Terminals/Joint%20Base%20Lewis-McChord%20Passenger%20Terminal/TCM72hr.pdf"
)
PAGE_URL = (
    "https://www.amc.af.mil/AMC-Travel-Site/Terminals/CONUS-Terminals/"
    "Joint-Base-Lewis-McChord-Passenger-Terminal/"
)

MONTHS = {
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4, "MAY": 5, "JUN": 6, "JUNE": 6, "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8, "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11, "DEC": 12, "DECEMBER": 12,
}
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
    r"\b(\d{3,4})\s+([A-Z0-9][A-Z0-9 .,'/&-]+?)\s+(TBD|VRC|SP|\d+[FT])\b",
    re.I,
)
KEEP_UPPER = {
    "AFB", "AB", "NAS", "MCAS", "INTL", "JB", "JRB", "ANGB", "FLD", "FIELD",
    "TX", "WA", "OR", "CA", "AK", "HI", "AZ", "NM", "OK", "KS", "MO", "AR",
    "LA", "MS", "AL", "GA", "FL", "SC", "NC", "VA", "MD", "DE", "PA", "NJ",
    "NY", "CT", "RI", "MA", "NH", "VT", "ME", "OH", "IN", "IL", "MI", "WI",
    "MN", "IA", "ND", "SD", "NE", "CO", "UT", "NV", "ID", "MT", "WY", "TN",
    "KY", "WV", "DC", "GU", "JP", "KR", "DEU", "UK", "QA",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
    "Referer": PAGE_URL,
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def pretty_dest(raw: str) -> str:
    raw = re.sub(r"\s+", " ", raw).strip(" ,")
    parts = []
    for word in raw.replace(",", " , ").split():
        up = word.upper()
        if up == ",":
            parts.append(",")
        elif up in KEEP_UPPER:
            parts.append(up)
        else:
            parts.append(word.title())
    out = " ".join(parts).replace(" ,", ",")
    out = re.sub(r"\bMguire\b", "McGuire", out, flags=re.I)
    out = re.sub(r"\bMcchord\b", "McChord", out, flags=re.I)
    return out


def dest_key(dest: str) -> str:
    return dest.lower()


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


def parse_outlook(text: str, origin: str = "tcm") -> dict:
    text = text.replace("\u00a0", " ")
    asof_m = ASOF_RE.search(text)
    if not asof_m:
        raise ValueError("Could not find CURRENT AS OF on the 72-hour PDF")
    day, mon, year, hhmm_raw = asof_m.group(1), asof_m.group(2).upper(), asof_m.group(3), asof_m.group(4)
    month = MONTHS.get(mon[:3] if mon != "SEPT" else "SEP") or MONTHS[mon]
    hhmm_raw = hhmm_raw.zfill(4)
    as_of = datetime(int(year), month, int(day), int(hhmm_raw[:2]), int(hhmm_raw[2:]), tzinfo=timezone.utc)
    # Label stays in local McChord time as printed on the PDF (Pacific).
    as_of_label = f"McChord outlook · {int(day)} {mon.title()[:3]} {year}, {hhmm_raw}L"
    as_of_iso = datetime(int(year), month, int(day), int(hhmm_raw[:2]), int(hhmm_raw[2:])).isoformat() + "-07:00"

    day_matches = list(DAY_RE.finditer(text))
    flights = []
    for i, m in enumerate(day_matches):
        d, mon_name, y = int(m.group(2)), m.group(3).upper(), int(m.group(4))
        mo = MONTHS.get(mon_name if mon_name != "SEPT" else "SEP") or MONTHS[mon_name[:3]]
        date = f"{y:04d}-{mo:02d}-{d:02d}"
        start = m.end()
        end = day_matches[i + 1].start() if i + 1 < len(day_matches) else len(text)
        chunk = text[start:end]
        if re.search(r"NO SCHEDULED FLIGHTS", chunk, re.I):
            continue
        # Flatten wrapped PDF lines so "0700\nKELLY FLD, TX\n0F" becomes one record.
        flat = re.sub(r"[ \t]*\n[ \t]*", " ", chunk)
        for fm in FLIGHT_RE.finditer(flat):
            roll_raw, dest_raw, seats = fm.group(1), fm.group(2), fm.group(3).upper()
            dest = pretty_dest(dest_raw)
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
        "sourceUrl": PDF_URL,
        "fetchedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "flights": flights,
        "_asOfParsed": as_of.isoformat(),
    }


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def fetch_pdf(dest: Path) -> None:
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = Request(PDF_URL, headers=HEADERS)
            with urlopen(req, timeout=45) as resp:
                data = resp.read()
            if data.startswith(b"%PDF"):
                dest.write_bytes(data)
                return
            preview = data[:180].decode("utf-8", "replace").replace("\n", " ")
            last_err = RuntimeError(f"AMC did not return a PDF (got {preview!r})")
        except Exception as exc:
            last_err = exc
        time.sleep(2 * (attempt + 1))
    raise last_err or RuntimeError("Failed to download McChord 72-hour PDF")


def write_board(board: dict, path: Path) -> bool:
    payload = {
        "asOf": board["asOf"],
        "asOfLabel": board["asOfLabel"],
        "sourceUrl": board["sourceUrl"],
        "fetchedAt": board["fetchedAt"],
        "flights": board["flights"],
    }
    new = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    old = path.read_text() if path.exists() else ""
    # Ignore fetchedAt when deciding if the outlook itself changed.
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
    fixture = Path(__file__).with_name("fixtures") / "tcm72.txt"
    board = parse_outlook(fixture.read_text())
    dests = [f["dest"] for f in board["flights"]]
    assert board["asOfLabel"].startswith("McChord outlook · 9 Sep 2026")
    assert dests == ["Kelly FLD, TX", "Dyess AFB, TX", "McGuire AFB, NJ"], dests
    assert board["flights"][0]["roll"] == "07:00"
    assert board["flights"][0]["kind"] == "firm"
    assert board["flights"][1]["kind"] == "tbd"
    assert board["flights"][2]["seats"] == "53F"
    print("self-test ok", len(board["flights"]), "flights")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, help="Parse this text file instead of fetching the PDF")
    ap.add_argument("--pdf", type=Path, help="Parse this local PDF")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.fixture:
        text = args.fixture.read_text()
    elif args.pdf:
        text = pdf_text(args.pdf)
    else:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            fetch_pdf(tmp_path)
            text = pdf_text(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
    board = parse_outlook(text)
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
