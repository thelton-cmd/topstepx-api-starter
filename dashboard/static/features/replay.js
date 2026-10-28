// replay.js - replay banner with transport controls, "REC" header tool, and a Settings section.
//
// While the dashboard runs with --replay PATH, a fixed banner shows which file is playing and the
// replayed clock, and offers Play/Pause, speed buttons, a progress slider and Step. Order entry is
// disabled by the server (order_entry_blocked), so the ticket is read-only. This module never sends
// anything except /api/replay/control. Nothing here is trading advice.

const POLL_MS = 500;
const SPEEDS = [1, 2, 5, 10, 20];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}

function mmss(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

function clockText(iso) {
  if (!iso) return "-";
  return String(iso).replace("T", " ").replace(/(\+00:00|Z)$/, " UTC");
}

export function mount(api) {
  // -- the REC tool (live + recording) --------------------------------------------------------
  const rec = el("span", "replay-rec", "");
  rec.hidden = true;
  rec.title = "This session is being recorded; replay it later with --replay PATH";
  api.addHeaderTool(rec);

  // -- the banner (replay mode) -----------------------------------------------------------------
  const banner = el("div", "replay-banner");
  banner.hidden = true;
  banner.setAttribute("role", "region");
  banner.setAttribute("aria-label", "Replay controls");
  const label = el("span", "replay-label", "");
  const playBtn = el("button", "small replay-play", "Pause");
  playBtn.type = "button";
  const speeds = el("span", "replay-speeds");
  const speedButtons = SPEEDS.map((value) => {
    const b = el("button", "small replay-speed", `${value}x`);
    b.type = "button";
    b.dataset.speed = String(value);
    speeds.appendChild(b);
    return b;
  });
  const slider = el("input", "replay-slider");
  slider.type = "range";
  slider.min = "0";
  slider.max = "1";
  slider.step = "0.1";
  slider.value = "0";
  slider.setAttribute("aria-label", "Replay position");
  const posText = el("span", "replay-pos", "00:00 / 00:00");
  const stepBtn = el("button", "small replay-step", "Step");
  stepBtn.type = "button";
  stepBtn.title = "Dispatch the next recorded message (while paused)";
  banner.append(label, playBtn, speeds, slider, posText, stepBtn);
  document.body.insertBefore(banner, document.body.firstChild);

  let status = null;
  let dragging = false;
  let timer = null;
  let busy = false;

  function padBody() {
    document.body.style.paddingTop = banner.hidden ? "" : `${banner.offsetHeight}px`;
  }

  function render(s) {
    status = s;
    const active = !!(s && s.active);
    if (banner.hidden === active) {
      banner.hidden = !active;
      padBody();
    }
    if (!active) return;
    label.textContent = `REPLAY ${s.file || ""} - ${clockText(s.clock)} - order entry disabled`;
    playBtn.textContent = s.playing ? "Pause" : (s.ended && !s.loop ? "Ended" : "Play");
    playBtn.disabled = !!(s.ended && !s.loop && !s.playing);
    stepBtn.hidden = !!s.playing;
    for (const b of speedButtons) b.classList.toggle("on", Number(b.dataset.speed) === Number(s.speed));
    slider.max = String(Math.max(s.duration_s || 0, 0.1));
    if (!dragging) slider.value = String(s.position_s || 0);
    posText.textContent = `${mmss(s.position_s)} / ${mmss(s.duration_s)}`;
    padBody();
  }

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      render(await api.get("/api/replay/status"));
    } catch (err) {
      /* the next tick tries again */
    } finally {
      busy = false;
    }
  }

  async function control(body) {
    try {
      const out = await api.post("/api/replay/control", body);
      if (out && out.active !== undefined) render(out);
    } catch (err) {
      api.notify(`Replay: ${err.message}`, "warn");
    }
  }

  function setPolling(on) {
    if (on && !timer) {
      poll();
      timer = setInterval(poll, POLL_MS);
    } else if (!on && timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  playBtn.addEventListener("click", () => control({ action: status && status.playing ? "pause" : "play" }));
  stepBtn.addEventListener("click", () => control({ action: "step" }));
  for (const b of speedButtons) {
    b.addEventListener("click", () => control({ action: "speed", value: Number(b.dataset.speed) }));
  }
  slider.addEventListener("input", () => {
    dragging = true;
    posText.textContent = `${mmss(slider.value)} / ${mmss(status && status.duration_s)}`;
  });
  slider.addEventListener("change", async () => {
    await control({ action: "seek", value: Number(slider.value) });
    dragging = false;
  });
  window.addEventListener("resize", padBody);

  // -- follow /api/state -------------------------------------------------------------------------------
  function onSnapshot(snap) {
    const info = ((snap || {}).features || {}).replay;
    const active = !!(info && info.active);
    setPolling(active);
    if (active && !status) render(info);
    if (!active && status && status.active) render({ active: false });
    const recording = info && info.recording;
    if (recording && recording.active) {
      rec.hidden = false;
      rec.textContent = `REC ${recording.path}`;
      rec.title = `${recording.lines} lines written. Replay it later with --replay.`;
    } else {
      rec.hidden = true;
    }
  }
  api.onState(onSnapshot);
  onSnapshot(api.snapshot());

  // -- settings section ---------------------------------------------------------------------------------
  api.registerSettingsSection({
    title: "Record and replay",
    render(container) {
      const lines = [
        "Record: python -m dashboard --record PATH (a .gz name compresses). Every hub message and the lookups the page needs are saved.",
        "Replay: python -m dashboard --replay PATH [--speed N] [--replay-loop]. The page runs on the file, offline, with order entry disabled.",
        "A recording holds your account data (balances, positions, fills). Keep it private and out of git.",
      ];
      for (const line of lines) container.appendChild(el("p", "muted replay-help", line));
    },
  });
}
