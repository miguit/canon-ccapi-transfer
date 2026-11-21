"use strict";

const $ = (s) => document.querySelector(s);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    ...opts,
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
  return data;
};
const enc = (key) => key.split("/").map(encodeURIComponent).join("/");
const mb = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(2)} Gt` : `${(b / 1e6).toFixed(1)} Mt`;

const state = {
  status: null,
  files: [],
  visible: [],
  selected: new Set(),
  kind: "all",
  onlyNew: false,
  anchor: null,
  lbIndex: -1,
  live: false,
};

/* ---------- toast ---------- */
let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4000);
}

/* ---------- theme ---------- */
function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function paintThemeIcon() {
  $("#btn-theme .icon").textContent = currentTheme() === "dark" ? "light_mode" : "dark_mode";
}
$("#btn-theme").onclick = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("theme", next); } catch {}
  paintThemeIcon();
};
paintThemeIcon();

/* ---------- camera status ---------- */
function paintStatus() {
  const s = state.status;
  const dot = $("#camera .dot");
  dot.className = "dot " + (s?.connected ? "on" : "off");
  let text = "Ei yhteyttä";
  if (s?.connected) {
    text = `${s.model || "Kamera"} · ${s.host}`;
    const level = s.battery?.level;
    if (level) text += ` · akku ${level === "full" ? "täynnä" : level}`;
  }
  $("#camera-text").textContent = text;
  paintTarget();
}

/* ---------- target: Kone | NAS ---------- */
function paintTarget() {
  const s = state.status || {};
  const target = s.target || "local";
  for (const b of document.querySelectorAll("#target button")) {
    const t = b.dataset.target;
    b.setAttribute("aria-checked", t === target);
    if (t === "local") {
      b.title = `Siirrä tämän koneen kansioon\n${s.dest_local || ""}`;
    } else {
      b.disabled = !s.dest_nas;
      b.classList.toggle("warn", !!s.dest_nas && !s.nas_available);
      b.title = !s.dest_nas ? "NAS-kansiota ei ole asetettu (Asetukset)"
        : s.nas_available ? `Siirrä NAS:lle\n${s.dest_nas}`
        : `NAS ei ole yhdistetty – avaa yhteys tiedostonhallinnassa\n${s.dest_nas}`;
    }
  }
}

for (const b of document.querySelectorAll("#target button")) {
  b.onclick = async () => {
    if (b.getAttribute("aria-checked") === "true") return;
    try {
      state.status = await api("/api/settings", { method: "PUT", body: { target: b.dataset.target } });
      paintStatus();
      if (state.status.target === "nas" && !state.status.nas_available)
        toast("NAS ei ole yhdistetty – avaa yhteys tiedostonhallinnassa");
      if (state.status.connected) await loadFiles(false);  // "siirretty" depends on target
    } catch (e) { toast(e.message); }
  };
}

async function loadStatus() {
  try { state.status = await api("/api/status"); } catch { state.status = null; }
  paintStatus();
  return state.status;
}

async function connect(host) {
  $("#camera-text").textContent = "Etsitään kameraa…";
  renderMessage("wifi", "Etsitään kameraa…", "Tämä voi kestää hetken, jos verkko skannataan.");
  try {
    state.status = await api("/api/connect", { method: "POST", body: { host: host || null } });
    paintStatus();
    await loadFiles(true);
  } catch (e) {
    await loadStatus();
    renderDisconnected(e.message);
  }
}

/* ---------- files ---------- */
async function loadFiles(refresh = false) {
  if (refresh) renderMessage("hourglass_empty", "Luetaan kameran muistikorttia…");
  try {
    state.files = await api(`/api/files${refresh ? "?refresh=1" : ""}`);
  } catch (e) {
    await loadStatus();
    renderDisconnected(e.message);
    return;
  }
  const keys = new Set(state.files.map((f) => f.key));
  for (const k of [...state.selected]) if (!keys.has(k)) state.selected.delete(k);
  render();
}

function applyFilters() {
  state.visible = state.files.filter((f) =>
    (state.kind === "all" || f.kind === state.kind) && (!state.onlyNew || !f.transferred));
}

function paintCounts() {
  const counts = { all: 0, jpeg: 0, raw: 0, video: 0 };
  let fresh = 0;
  for (const f of state.files) {
    counts.all++; counts[f.kind]++;
    if (!f.transferred) fresh++;
  }
  for (const chip of document.querySelectorAll("#filters .chip")) {
    chip.querySelector(".n").textContent = counts[chip.dataset.kind] || "";
    chip.setAttribute("aria-pressed", chip.dataset.kind === state.kind);
  }
  $("#only-new .n").textContent = fresh || "";
  $("#only-new").setAttribute("aria-pressed", state.onlyNew);
  const n = state.selected.size;
  $("#sel-count").textContent = n ? `${n} valittu` : "";
  const btn = $("#btn-download");
  btn.disabled = !n || jobRunning;
  btn.lastChild.textContent = n ? `Siirrä ${n}` : "Siirrä";
  $("#btn-delete").disabled = !n || jobRunning;
}

/* ---------- rendering ---------- */
const main = $("#main");

function renderMessage(icon, title, sub = "") {
  main.innerHTML = "";
  const d = document.createElement("div");
  d.className = "empty";
  d.innerHTML = `<span class="icon">${icon}</span><h2></h2><div class="sub"></div>`;
  d.querySelector("h2").textContent = title;
  d.querySelector(".sub").textContent = sub;
  main.append(d);
  return d;
}

function renderDisconnected(err) {
  const d = renderMessage("wifi_off", "Kameraan ei ole yhteyttä",
    err || "Laita kameran Wi-Fi päälle ja yhdistä kone kameran verkkoon.");
  const help = document.createElement("div");
  help.className = "sub";
  help.style.maxWidth = "520px";
  help.textContent = "Käynnistä kamerassa CCAPI-yhteys tukiasematilassa ja liitä kone kameran " +
    "Wi-Fi-verkkoon (verkon nimi ja salasana näkyvät kameran näytöllä). Sitten Yhdistä.";
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = `<input id="host-input" placeholder="IP (vapaaehtoinen)" value="">
    <button class="btn primary" id="btn-connect"><span class="icon">wifi</span>Yhdistä</button>`;
  d.append(help, row);
  const input = row.querySelector("input");
  input.value = state.status?.host || "";
  const go = () => connect(input.value.trim());
  row.querySelector("button").onclick = go;
  input.onkeydown = (e) => e.key === "Enter" && go();
}

function render() {
  applyFilters();
  paintCounts();
  thumbs.reset();
  main.innerHTML = "";
  if (!state.files.length) {
    renderMessage("image", "Muistikortilla ei ole kuvia");
    return;
  }
  if (!state.visible.length) {
    renderMessage("check_circle", "Ei näytettäviä tiedostoja",
      state.onlyNew ? "Kaikki on jo siirretty koneelle." : "");
    return;
  }
  let folder = null, grid = null;
  const frag = document.createDocumentFragment();
  state.visible.forEach((f, i) => {
    if (f.folder !== folder) {
      folder = f.folder;
      const sec = document.createElement("section");
      sec.className = "folder";
      sec.innerHTML = `<h2><span class="icon">folder</span></h2><div class="grid"></div>`;
      sec.querySelector("h2").append(folder);
      grid = sec.querySelector(".grid");
      frag.append(sec);
    }
    grid.append(tile(f, i));
  });
  main.append(frag);
}

function tile(f, i) {
  const el = document.createElement("div");
  el.className = "tile";
  el.tabIndex = 0;
  el.dataset.i = i;
  el.dataset.key = f.key;
  el.classList.toggle("selected", state.selected.has(f.key));
  el.classList.toggle("done", f.transferred);
  const ph = f.kind === "video" ? "movie" : "image";
  el.innerHTML = `
    <div class="ph"><span class="icon">${ph}</span></div>
    <span class="check"><span class="icon">check</span></span>
    ${f.transferred ? '<span class="done-mark" title="Siirretty ${state.status?.target === "nas" ? "NAS:lle" : "koneelle"}"><span class="icon">check</span></span>' : ""}
    <button class="zoom" title="Esikatselu (välilyönti)"><span class="icon">zoom_in</span></button>
    <div class="label"><span class="name"></span><span class="badge ${f.kind}">${f.ext}</span></div>`;
  el.querySelector(".name").textContent = f.name;
  thumbs.observe(el);
  return el;
}

function paintSelection() {
  for (const el of main.querySelectorAll(".tile")) {
    el.classList.toggle("selected", state.selected.has(el.dataset.key));
  }
  paintCounts();
}

/* ---------- thumbnails: lazy, max 2 at a time (camera is single-threaded) ---------- */
const thumbs = (() => {
  const queue = [];
  let active = 0;
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const el = e.target;
      if (e.isIntersecting) {
        if (!el._queued && !el._loaded) { el._queued = true; queue.push(el); }
      } else if (el._queued && !el._loading) {
        el._queued = false;
        const i = queue.indexOf(el);
        if (i >= 0) queue.splice(i, 1);
      }
    }
    pump();
  }, { rootMargin: "300px" });

  function pump() {
    while (active < 2 && queue.length) {
      const el = queue.shift();
      if (!el.isConnected) continue;
      el._loading = true;
      active++;
      const img = new Image();
      img.alt = "";
      img.decoding = "async";
      const done = () => { active--; el._loading = false; el._queued = false; pump(); };
      img.onload = () => {
        el._loaded = true;
        el.querySelector(".ph")?.remove();
        el.prepend(img);
        io.unobserve(el);
        done();
      };
      img.onerror = () => {
        el._loaded = true;
        io.unobserve(el);
        done();
      };
      img.src = `/api/thumb/${enc(el.dataset.key)}`;
    }
  }
  return {
    observe: (el) => io.observe(el),
    reset: () => { queue.length = 0; io.disconnect(); },
  };
})();

/* ---------- selection ---------- */
main.addEventListener("click", (e) => {
  const el = e.target.closest(".tile");
  if (!el) return;
  const i = +el.dataset.i;
  if (e.target.closest(".zoom")) { openLightbox(i); return; }
  const key = el.dataset.key;
  if (e.shiftKey && state.anchor !== null) {
    const [a, b] = [Math.min(state.anchor, i), Math.max(state.anchor, i)];
    for (let j = a; j <= b; j++) state.selected.add(state.visible[j].key);
  } else {
    state.selected.has(key) ? state.selected.delete(key) : state.selected.add(key);
    state.anchor = i;
  }
  paintSelection();
});
main.addEventListener("dblclick", (e) => {
  const el = e.target.closest(".tile");
  if (el) openLightbox(+el.dataset.i);
});
main.addEventListener("keydown", (e) => {
  const el = e.target.closest(".tile");
  if (!el) return;
  if (e.key === "Enter") { el.click(); e.preventDefault(); }
  if (e.key === " ") { openLightbox(+el.dataset.i); e.preventDefault(); }
});

$("#btn-select-all").onclick = () => { state.visible.forEach((f) => state.selected.add(f.key)); paintSelection(); };
$("#btn-select-none").onclick = () => { state.selected.clear(); paintSelection(); };
$("#btn-select-new").onclick = () => {
  state.selected = new Set(state.visible.filter((f) => !f.transferred).map((f) => f.key));
  paintSelection();
  if (!state.selected.size) toast("Kaikki näkyvät tiedostot on jo siirretty");
};

for (const chip of document.querySelectorAll("#filters .chip")) {
  chip.onclick = () => { state.kind = chip.dataset.kind; state.anchor = null; render(); };
}
$("#only-new").onclick = () => { state.onlyNew = !state.onlyNew; state.anchor = null; render(); };
$("#btn-refresh").onclick = () => state.status?.connected ? loadFiles(true) : connect();

/* ---------- lightbox ---------- */
function openLightbox(i) {
  state.lbIndex = i;
  $("#lightbox").hidden = false;
  showLightbox();
}
function showLightbox() {
  const f = state.visible[state.lbIndex];
  if (!f) return closeLightbox();
  $("#lb-title").textContent = `${f.folder}/${f.name}  ·  ${state.lbIndex + 1}/${state.visible.length}`;
  const img = $("#lb-img");
  img.removeAttribute("src");
  $("#lb-spin").hidden = false;
  const src = f.kind === "video" ? `/api/thumb/${enc(f.key)}` : `/api/preview/${enc(f.key)}`;
  let tries = 0;
  img.onload = () => { $("#lb-spin").hidden = true; loadExif(f); };
  $("#lb-exif").textContent = "";
  img.onerror = () => {
    // a brand-new shot may still be being written to the card: retry a few times
    if (state.live && tries++ < 5 && state.visible[state.lbIndex] === f)
      setTimeout(() => (img.src = `${src}?r=${tries}`), 800);
    else $("#lb-spin").hidden = true;
  };
  img.src = src;
  $("#lb-live").hidden = !state.live;
  paintLightboxSelect();
}
async function loadExif(f) {
  if (f.kind === "video") return;
  let x;
  try { x = await api(`/api/exif/${enc(f.key)}`); } catch { return; }
  if (state.visible[state.lbIndex] !== f || $("#lightbox").hidden) return;
  const box = $("#lb-exif");
  box.textContent = "";
  const add = (text, cls = "v") => {
    if (!text) return;
    const s = document.createElement("span");
    s.className = cls; s.textContent = text; box.append(s);
  };
  add(x.mode && `${x.mode}-tila`, "dim");
  add(x.shutter); add(x.aperture); add(x.iso); add(x.ev);
  add(x.focal, "dim");
  if (x.taken) {
    const [d, t] = x.taken.split(" ");
    const [y, m, dd] = d.split(":");
    add(`${+dd}.${+m}.${y} ${t}`, "dim");
  }
}

function paintLightboxSelect() {
  const f = state.visible[state.lbIndex];
  const on = f && state.selected.has(f.key);
  const b = $("#lb-select");
  b.querySelector(".icon").classList.toggle("fill", on);
  b.lastChild.textContent = on ? "Valittu" : "Valitse";
}
function closeLightbox() {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  $("#lightbox").hidden = true;
  main.querySelector(`.tile[data-i="${state.lbIndex}"]`)?.focus();
}
const step = (d) => {
  const n = state.lbIndex + d;
  if (n >= 0 && n < state.visible.length) { state.lbIndex = n; showLightbox(); }
};
$("#lb-prev").onclick = () => step(-1);
$("#lb-next").onclick = () => step(1);
$("#lb-close").onclick = closeLightbox;
$("#lb-select").onclick = () => {
  const k = state.visible[state.lbIndex].key;
  state.selected.has(k) ? state.selected.delete(k) : state.selected.add(k);
  paintSelection();
  paintLightboxSelect();
};

document.addEventListener("keydown", (e) => {
  if (e.target.matches?.("input, select") || $("#settings").open || $("#confirm-delete").open) return;
  if (vf.on && $("#lightbox").hidden) {
    if (e.key === "Escape") closeViewfinder();
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); $("#vf-shutter").click(); }
    else if (e.key === "f") toggleFullscreen($("#viewfinder"));
    else if (e.key === "l") toggleLive();
    return;
  }
  if (!$("#lightbox").hidden) {
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "f") toggleFullscreen();
    else if (e.key === "l") toggleLive();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === " " || e.key === "Enter") { $("#lb-select").click(); e.preventDefault(); }
    return;
  }
  if (e.key === "Escape") $("#btn-select-none").click();
  else if ((e.ctrlKey || e.metaKey) && e.key === "a") { $("#btn-select-all").click(); e.preventDefault(); }
  else if (e.key === "n" && !e.ctrlKey) $("#btn-select-new").click();
  else if (e.key === "r" && !e.ctrlKey) $("#btn-refresh").click();
  else if (e.key === "Delete" && !$("#btn-delete").disabled) $("#btn-delete").click();
  else if (e.key === "l" && !e.ctrlKey) toggleLive();
  else if (e.key === "e" && !e.ctrlKey) openViewfinder();
});

/* ---------- live mode: show each new shot big ---------- */
let liveSeq = null, liveTimer = null;

function toggleFullscreen(el = $("#lightbox")) {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else el.requestFullscreen?.().catch(() => {});
}
$("#lb-img").ondblclick = toggleFullscreen;

function paintLive(paused = false) {
  const b = $("#btn-live");
  b.setAttribute("aria-pressed", state.live);
  b.classList.toggle("paused", state.live && paused);
  b.title = !state.live ? "Näytä jokainen uusi kuva heti isona (L)"
    : paused ? "Live odottaa: siirto tai poisto on käynnissä"
    : "Live päällä: uusi kuva avautuu heti isona (L sulkee)";
  $("#lb-live").hidden = !state.live;
}

function toggleLive(on = !state.live) {
  state.live = on;
  try { localStorage.setItem("live", on ? "1" : ""); } catch {}
  clearTimeout(liveTimer);
  liveSeq = null;
  paintLive();
  if (on) { toast("Live päällä – ota kuva, niin se avautuu tähän"); pollLive(); }
}
$("#btn-live").onclick = () => toggleLive();

async function pollLive() {
  clearTimeout(liveTimer);
  if (!state.live) return;
  let r = null;
  try { r = await api(`/api/live?since=${liveSeq ?? 0}`); } catch { /* camera away: keep trying */ }
  if (!state.live) return;
  if (r) {
    paintLive(r.busy);
    const first = liveSeq === null;
    liveSeq = r.seq;
    if (!first && r.shots.length) await showNewShot(r.shots.at(-1));
  }
  liveTimer = setTimeout(pollLive, r ? 1000 : 3000);
}

async function showNewShot(keys) {
  const key = keys.find((k) => /\.jpe?g$/i.test(k)) || keys[0];
  await loadFiles(false);
  let i = state.visible.findIndex((f) => f.key === key);
  if (i < 0) {  // hidden by a filter: show everything
    state.kind = "all"; state.onlyNew = false; render();
    i = state.visible.findIndex((f) => f.key === key);
  }
  if (i >= 0) openLightbox(i);
}

/* ---------- viewfinder: camera live view ---------- */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const vf = { on: false, seq: 0, url: null, last: null, size: "medium", run: 0 };
try { vf.size = localStorage.getItem("vfSize") || "medium"; } catch {}

const MODES = { p: "P", av: "Av", tv: "Tv", m: "M", fv: "Fv", a: "A+", "a+": "A+", scn: "SCN",
  creativefilter: "Luova suodin", movie: "Video", bulb: "B", c1: "C1", c2: "C2", c3: "C3" };
const WB = { auto: "Auto", awbwhite: "Auto (valk.)", daylight: "Päivänvalo", shade: "Varjo",
  cloudy: "Pilvinen", tungsten: "Hehkulamppu", whitefluorescent: "Loisteputki", flash: "Salama",
  custom: "Oma", colortemp: "K" };
const DRIVE = { single: "Yksittäis", highspeed: "Sarja H", lowspeed: "Sarja L",
  self_10sec: "10 s", self_2sec: "2 s", self_continuous: "Itselauk. sarja" };
const AF = { oneshot: "One Shot", servo: "Servo", ai_focus: "AI Focus", manual: "MF" };

function fmtEv(v) {
  if (!v) return "";
  if (/^[+-]0(\.0)?$/.test(v)) return "±0";
  const m = /^([+-])(\d+)(?:[._](\d)\/3|\.0)?$/.exec(v);
  if (!m) return v;
  const [, sign, whole, third] = m;
  return `${sign === "-" ? "−" : "+"}${whole !== "0" ? whole : ""}${third ? `${whole !== "0" ? " " : ""}${third}/3` : ""}`;
}

function paintVfSettings(s) {
  const box = $("#vf-settings");
  box.textContent = "";
  const add = (k, v) => {
    if (v === undefined || v === null || v === "") return;
    const span = document.createElement("span");
    if (k) { const kk = document.createElement("span"); kk.className = "k"; kk.textContent = k; span.append(kk); }
    const vv = document.createElement("span"); vv.className = "v"; vv.textContent = v; span.append(vv);
    box.append(span);
  };
  add("", MODES[s.shootingmodedial] || s.shootingmodedial?.toUpperCase());
  add("", s.tv);
  add("", s.av ? s.av.replace(/^f/, "f/").replace(".", ",") : "");
  add("ISO", s.iso === "auto" ? "Auto" : s.iso);
  add("EV", fmtEv(s.exposure));
  add("WB", s.wb === "colortemp" && s.colortemperature ? `${s.colortemperature} K` : WB[s.wb] || s.wb);
  add("AF", AF[s.afoperation] || s.afoperation);
  add("", DRIVE[s.drive] || s.drive);
  const q = s.stillimagequality;
  if (q && typeof q === "object") {
    const parts = [q.raw && q.raw !== "off" ? q.raw.toUpperCase().replace("CRAW", "C-RAW") : "",
      q.jpeg && q.jpeg !== "off" ? "JPEG" : ""].filter(Boolean);
    add("", parts.join("+"));
  }
  if (s.recordableshots != null) add("", `${s.recordableshots} kuvaa jäljellä`);
}

function drawHistogram(hist) {
  const c = $("#vf-hist"), g = c.getContext("2d");
  g.clearRect(0, 0, c.width, c.height);
  if (!hist?.length) return;
  const n = hist[0].length, w = c.width / n, h = c.height;
  const sorted = [...hist[0]].sort((a, b) => a - b);
  const max = Math.max(1, sorted[Math.floor(sorted.length * 0.98)] * 1.1);
  const y = (v) => h - Math.min(1, v / max) * (h - 2);
  // luminance as an area, R/G/B as thin lines
  g.fillStyle = "rgba(255,255,255,.45)";
  g.beginPath(); g.moveTo(0, h);
  hist[0].forEach((v, i) => g.lineTo(i * w + w / 2, y(v)));
  g.lineTo(c.width, h); g.closePath(); g.fill();
  ["rgba(255,80,80,.9)", "rgba(80,220,120,.9)", "rgba(90,150,255,.9)"].forEach((col, k) => {
    const ch = hist[k + 1];
    if (!ch) return;
    g.strokeStyle = col; g.lineWidth = 1; g.beginPath();
    ch.forEach((v, i) => (i ? g.lineTo : g.moveTo).call(g, i * w + w / 2, y(v)));
    g.stroke();
  });
}

function paintVfSize() {
  for (const b of document.querySelectorAll("#vf-size button"))
    b.setAttribute("aria-checked", b.dataset.size === vf.size);
}
for (const b of document.querySelectorAll("#vf-size button")) {
  b.onclick = () => {
    vf.size = b.dataset.size;
    try { localStorage.setItem("vfSize", vf.size); } catch {}
    paintVfSize();
  };
}

async function vfFrameLoop(run) {
  const img = $("#vf-img");
  while (vf.on && vf.run === run) {
    try {
      const r = await fetch(`/api/lv/frame?after=${vf.seq}&size=${vf.size}`, { cache: "no-store" });
      if (!vf.on || vf.run !== run) break;
      if (r.status === 200) {
        vf.seq = +r.headers.get("X-Seq") || vf.seq + 1;
        const url = URL.createObjectURL(await r.blob());
        img.src = url;
        await img.decode().catch(() => {});
        if (vf.url) URL.revokeObjectURL(vf.url);
        vf.url = url;
        if ($("#vf-msg").dataset.kind !== "error") $("#vf-msg").textContent = "";
      } else if (r.status === 204) {
        await sleep(300);
      } else {
        const d = await r.json().catch(() => null);
        $("#vf-msg").textContent = d?.detail || `Virhe ${r.status}`;
        await sleep(1500);
      }
    } catch { await sleep(1500); }
  }
}

async function vfInfoLoop(run) {
  while (vf.on && vf.run === run) {
    try {
      const d = await api("/api/lv/info");
      if (!vf.on || vf.run !== run) break;
      paintVfSettings(d.settings || {});
      drawHistogram(d.histogram);
      $("#vf-fps").textContent = d.fps ? `${String(d.fps).replace(".", ",")} kuvaa/s` : "";
      const msg = $("#vf-msg");
      if (d.busy) { msg.dataset.kind = "error"; msg.textContent = "Siirto käynnissä – live-kuva jatkuu, kun se on valmis"; }
      else if (d.error) { msg.dataset.kind = "error"; msg.textContent = `Live-kuva ei käynnisty: ${d.error}`; }
      else if (msg.dataset.kind === "error") { msg.dataset.kind = ""; msg.textContent = ""; }
      $("#vf-shutter").disabled = d.busy || vf.shooting;
      if (d.last_shot && d.last_shot !== vf.last) {
        vf.last = d.last_shot;
        const li = $("#vf-last-img");
        let tries = 0;
        li.onerror = () => { if (tries++ < 5) setTimeout(() => (li.src = `/api/thumb/${enc(vf.last)}?r=${tries}`), 800); };
        li.src = `/api/thumb/${enc(vf.last)}`;
        $("#vf-last").hidden = false;
      }
    } catch { /* camera away: keep trying */ }
    await sleep(400);
  }
}

function openViewfinder() {
  if (vf.on) return;
  if (!state.status?.connected) { toast("Ei yhteyttä kameraan"); return; }
  vf.on = true; vf.seq = 0; vf.run++;
  $("#viewfinder").hidden = false;
  $("#vf-msg").dataset.kind = "";
  $("#vf-msg").textContent = "Käynnistetään live-kuvaa…";
  paintVfSize();
  vfFrameLoop(vf.run);
  vfInfoLoop(vf.run);
}

function closeViewfinder() {
  if (!vf.on) return;
  vf.on = false;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  $("#viewfinder").hidden = true;
  fetch("/api/lv/stop", { method: "POST" }).catch(() => {});
}

$("#btn-viewfinder").onclick = openViewfinder;
$("#vf-close").onclick = closeViewfinder;
$("#vf-img").ondblclick = () => toggleFullscreen($("#viewfinder"));
$("#vf-shutter").onclick = async () => {
  if (vf.shooting) return;
  vf.shooting = true;
  $("#vf-shutter").disabled = true;
  const stage = $("#vf-stage");
  stage.classList.remove("flash"); void stage.offsetWidth; stage.classList.add("flash");
  try { await api("/api/lv/shutter", { method: "POST" }); }
  catch (e) { toast(`Laukaisu ei onnistunut: ${e.message}`); }
  finally { vf.shooting = false; $("#vf-shutter").disabled = false; }
};
$("#vf-last").onclick = async () => {
  if (!vf.last) return;
  await showNewShot([vf.last]);
};
// stop live view on the camera when the page goes away
window.addEventListener("pagehide", () => { if (vf.on) navigator.sendBeacon?.("/api/lv/stop"); });

/* ---------- transfer ---------- */
let jobRunning = false, jobTimer = null;

$("#btn-download").onclick = async () => {
  const keys = state.visible.filter((f) => state.selected.has(f.key)).map((f) => f.key);
  // include selected items hidden by the current filter too
  for (const k of state.selected) if (!keys.includes(k)) keys.push(k);
  try {
    await api("/api/download", { method: "POST", body: { keys } });
    state.selected.clear();
    paintSelection();
    pollJob();
  } catch (e) { toast(e.message); }
};
/* ---------- delete from camera ---------- */
let deleteKeys = [];
$("#btn-delete").onclick = () => {
  deleteKeys = [...state.selected];
  if (!deleteKeys.length) return;
  const byKey = new Map(state.files.map((f) => [f.key, f]));
  const noCopy = deleteKeys.filter((k) => !byKey.get(k)?.copies?.length).length;
  const n = deleteKeys.length;
  $("#del-title").textContent = `Poistetaanko ${n} ${n === 1 ? "tiedosto" : "tiedostoa"} kamerasta?`;
  $("#del-warn").hidden = !noCopy;
  $("#del-warn-text").textContent = noCopy === n
    ? (n === 1 ? "Tätä tiedostoa ei ole siirretty koneelle eikä NAS:lle."
               : "Yhtäkään näistä ei ole siirretty koneelle eikä NAS:lle.")
    : `${noCopy} niistä ei ole siirretty koneelle eikä NAS:lle.`;
  $("#confirm-delete").returnValue = "";
  $("#confirm-delete").showModal();
};
$("#confirm-delete").addEventListener("close", async () => {
  if ($("#confirm-delete").returnValue !== "delete") return;
  try {
    await api("/api/delete", { method: "POST", body: { keys: deleteKeys } });
    state.selected.clear();
    paintSelection();
    pollJob();
  } catch (e) { toast(e.message); }
});

$("#tr-cancel").onclick = () => api("/api/job/cancel", { method: "POST" });
$("#tr-close").onclick = () => ($("#transfer").hidden = true);

async function pollJob() {
  clearTimeout(jobTimer);
  let j;
  try { j = await api("/api/job"); } catch { j = null; }
  if (!j) return;
  const wasRunning = jobRunning;
  jobRunning = j.running;
  const panel = $("#transfer");
  panel.hidden = false;
  const handled = j.done + j.skipped;
  const pct = j.total ? (handled / j.total) * 100 : 100;
  $("#tr-bar").style.width = `${pct}%`;
  panel.classList.toggle("ok", !j.running && !j.errors.length);
  const del = j.action === "delete";
  $("#tr-icon").textContent = j.running ? (del ? "delete" : "download") : j.errors.length ? "error" : "check_circle";
  $("#tr-title").textContent = del
    ? (j.running ? `Poistetaan ${handled + 1}/${j.total}`
      : `${j.cancelled ? "Keskeytetty" : "Valmis"}: ${j.done} poistettu kamerasta${j.skipped ? `, ${j.skipped} ohitettu` : ""}`)
    : j.running
    ? `Siirretään ${handled + 1}/${j.total}`
    : j.cancelled ? `Keskeytetty: ${j.done} siirretty`
    : `Valmis: ${j.done} siirretty ${j.target === "nas" ? "NAS:lle" : "koneelle"}${j.skipped ? `, ${j.skipped} oli jo siellä` : ""}`;
  $("#tr-bytes").textContent = del ? "" : mb(j.bytes_done);
  $("#tr-speed").textContent = !del && j.running && j.speed ? `${mb(j.speed)}/s` : "";
  $("#tr-cur").textContent = j.current || (j.running || del ? "" : j.dest || "");
  $("#tr-cancel").hidden = !j.running;
  $("#tr-close").hidden = j.running;
  const errs = $("#tr-errors");
  errs.hidden = !j.errors.length;
  errs.innerHTML = "";
  for (const m of j.errors) { const li = document.createElement("li"); li.textContent = m; errs.append(li); }
  paintCounts();
  if (j.running) jobTimer = setTimeout(pollJob, 600);
  else if (wasRunning) loadFiles(false);
}

/* ---------- settings ---------- */
$("#btn-settings").onclick = () => {
  const s = state.status || {};
  $("#set-host").value = s.host || "";
  $("#set-dest-local").value = s.dest_local || "";
  $("#set-dest-nas").value = s.dest_nas || "";
  $("#set-layout").value = s.layout || "%Y-%m-%d";
  $("#settings").showModal();
};
$("#settings").addEventListener("close", async () => {
  if ($("#settings").returnValue !== "save") return;
  try {
    state.status = await api("/api/settings", {
      method: "PUT",
      body: {
        dest_local: $("#set-dest-local").value.trim() || null,
        dest_nas: $("#set-dest-nas").value.trim(),
        layout: $("#set-layout").value,
      },
    });
    paintStatus();
    if (state.status.connected) loadFiles(false);
    const host = $("#set-host").value.trim();
    if (host && host !== state.status.host) connect(host);
    else toast("Asetukset tallennettu");
  } catch (e) { toast(e.message); }
});

/* ---------- boot ---------- */
(async () => {
  const s = await loadStatus();
  if (s?.connected) await loadFiles(false);
  else connect();
  try { if (localStorage.getItem("live")) toggleLive(true); } catch {}
  pollJob();
  setInterval(() => { if (!jobRunning) loadStatus(); }, 30000);
})();
