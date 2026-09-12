(function () {
  const data = window.SAF;
  if (!data) return;

  const byId = (id) => data.terminals.find((t) => t.id === id);

  function parseRoll(flight) {
    const origin = byId(flight.origin);
    const tz = origin ? origin.tz : "America/Los_Angeles";
    const [yy, mo, dd] = flight.date.split("-").map(Number);
    const [hh, mm] = flight.roll.split(":").map(Number);
    const wanted = Date.UTC(yy, mo - 1, dd, hh, mm, 0);
    let guess = new Date(wanted);
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
    });
    for (let i = 0; i < 4; i++) {
      const p = Object.fromEntries(fmt.formatToParts(guess).map((x) => [x.type, x.value]));
      const shown = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
      guess = new Date(guess.getTime() + (wanted - shown));
    }
    return guess;
  }

  function statusFor(flight) {
    const when = parseRoll(flight);
    if (when.getTime() < Date.now()) return "passed";
    return flight.kind;
  }

  function formatRoll(flight) {
    const origin = byId(flight.origin);
    return new Intl.DateTimeFormat("en-US", {
      timeZone: origin ? origin.tz : "America/Los_Angeles",
      weekday: "short", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit"
    }).format(parseRoll(flight));
  }

  function formatHHMM(flight) {
    const [h, m] = flight.roll.split(":");
    return h + m;
  }

  function haystack(flight) {
    const origin = byId(flight.origin) || {};
    return [flight.dest, flight.destKey, origin.name, origin.field, origin.city, origin.icao, origin.id]
      .join(" ").toLowerCase();
  }

  function terminalHay(t) {
    return [t.name, t.field, t.city, t.icao, t.id, t.region].join(" ").toLowerCase();
  }

  function badgeClass(status, kind) {
    if (status === "passed") return "gone";
    return kind;
  }

  function badgeLabel(status, flight) {
    if (status === "passed") return "Roll call passed";
    if (flight.kind === "firm") return flight.seats + " firm";
    if (flight.kind === "tent") return flight.seats + " tent.";
    return flight.seats;
  }

  function renderFlights(list, mount, opts) {
    if (!mount) return;
    const upcoming = list.filter((f) => statusFor(f) !== "passed");
    const show = opts && opts.upcomingOnly ? upcoming : list;
    if (!show.length) {
      mount.innerHTML = `<div class="empty">
        <h3>${opts && opts.emptyTitle ? opts.emptyTitle : "No matching flights"}</h3>
        <p>${opts && opts.emptyBody ? opts.emptyBody : "Nothing on the current outlook matches that search. Try another city or open a terminal’s official 72-hour PDF."}</p>
      </div>`;
      return;
    }
    mount.innerHTML = show.map((f) => {
      const origin = byId(f.origin) || { name: f.origin, field: "" };
      const st = statusFor(f);
      const href = origin.local || origin.page || "jblm.html";
      return `<a class="board-row${st === "passed" ? " past" : ""}" href="${href}">
        <div class="time tabular">${formatHHMM(f)}</div>
        <div class="dest">
          <strong>${f.dest}</strong>
          <span>${formatRoll(f)} · ${origin.field || origin.name}</span>
        </div>
        <div class="origin-tag">${origin.icao || ""}</div>
        <div class="badge ${badgeClass(st, f.kind)}">${badgeLabel(st, f)}</div>
      </a>`;
    }).join("");
  }

  function renderTerminals(list, mount) {
    if (!mount) return;
    if (!list.length) {
      mount.innerHTML = `<div class="empty"><h3>No terminals match</h3><p>Try a city, ICAO, or theater.</p></div>`;
      return;
    }
    mount.innerHTML = list.map((t) => {
      const href = t.local || t.page;
      return `<a class="t-card" href="${href}">
        <div class="icao">${t.icao}</div>
        <h3>${t.name}</h3>
        <p>${t.city}</p>
      </a>`;
    }).join("");
  }

  const q = document.getElementById("q");
  const chips = document.getElementById("chips");
  const board = document.getElementById("board");
  const tgrid = document.getElementById("terminals");
  const asOf = document.getElementById("asof");
  const upcomingMount = document.getElementById("upcoming");

  let region = "all";

  function filteredFlights() {
    const query = (q && q.value || "").trim().toLowerCase();
    return data.flights.filter((f) => {
      const origin = byId(f.origin);
      if (region !== "all" && origin && origin.region !== region) return false;
      if (query && !haystack(f).includes(query)) return false;
      return true;
    }).sort((a, b) => {
      const da = a.date + a.roll + a.origin;
      const db = b.date + b.roll + b.origin;
      return da < db ? -1 : da > db ? 1 : 0;
    });
  }

  function filteredTerminals() {
    const query = (q && q.value || "").trim().toLowerCase();
    return data.terminals.filter((t) => {
      if (region !== "all" && t.region !== region) return false;
      if (query && !terminalHay(t).includes(query) && !data.flights.some((f) => f.origin === t.id && haystack(f).includes(query))) return false;
      return true;
    });
  }

  function refresh() {
    const flights = filteredFlights().filter((f) => {
      const only = board && board.getAttribute("data-origin");
      return only ? f.origin === only : true;
    });
    if (upcomingMount) {
      renderFlights(flights, upcomingMount, {
        upcomingOnly: true,
        emptyTitle: "No upcoming roll calls on the current board",
        emptyBody: "McChord’s latest 72-hour window has closed. Call the flight recording or open the official PDF — terminals usually drop the next board without much warning."
      });
    }
    if (board) renderFlights(flights, board, { upcomingOnly: false });
    if (tgrid) renderTerminals(filteredTerminals(), tgrid);
  }

  function applyBoard(payload) {
    if (!payload || !Array.isArray(payload.flights)) return;
    data.flights = payload.flights;
    if (payload.asOf) data.asOf = payload.asOf;
    if (payload.asOfLabel) data.asOfLabel = payload.asOfLabel;
  }

  function boot() {
    if (asOf) asOf.textContent = data.asOfLabel;
    if (chips && !chips.dataset.ready) {
      chips.dataset.ready = "1";
      chips.innerHTML = data.regions.map((r) =>
        `<button type="button" class="chip${r.id === "all" ? " active" : ""}" data-region="${r.id}">${r.label}</button>`
      ).join("");
      chips.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-region]");
        if (!btn) return;
        region = btn.getAttribute("data-region");
        chips.querySelectorAll(".chip").forEach((c) => c.classList.toggle("active", c === btn));
        refresh();
      });
    }
    if (q && !q.dataset.ready) {
      q.dataset.ready = "1";
      q.addEventListener("input", refresh);
    }
    refresh();
    window.SAFApp = { parseRoll, statusFor, renderFlights, byId };
  }

  boot();
  fetch("js/flights.json", { cache: "no-store" })
    .then((r) => (r.ok ? r.json() : null))
    .then((payload) => { applyBoard(payload); boot(); })
    .catch(() => {});
})();
