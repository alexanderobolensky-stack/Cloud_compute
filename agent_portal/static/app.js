import { compact, lineChart, sparkline } from "./charts.js";

// ---------------------------------------------------------------------------
// helpers

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $("#view");

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

function fmtDuration(seconds) {
  if (seconds == null) return "–";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} д ${h} ч`;
  if (h) return `${h} ч ${m} мин`;
  if (m) return `${m} мин`;
  return `${Math.floor(seconds)} с`;
}
const fmtMb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} ГБ` : `${Math.round(mb)} МБ`);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hourCycle: "h23" });
const fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString([], { hourCycle: "h23" });
const fmtRate = (bps) => (bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} МБ/с` : `${Math.round(bps / 1e3)} КБ/с`);

const STATES = {
  running: { label: "работает", tone: "good", icon: "●" },
  starting: { label: "запускается", tone: "info", icon: "◐" },
  stopping: { label: "останавливается", tone: "info", icon: "◑" },
  backoff: { label: "ждёт перезапуска", tone: "warn", icon: "↻" },
  crashed: { label: "упал", tone: "crit", icon: "✕" },
  exited: { label: "завершился", tone: "muted", icon: "■" },
  stopped: { label: "остановлен", tone: "muted", icon: "○" },
  missing: { label: "не найден", tone: "serious", icon: "?" },
  unknown: { label: "нет данных", tone: "muted", icon: "·" },
};
const stateBadge = (state) => {
  const s = STATES[state] ?? STATES.unknown;
  return `<span class="state" data-tone="${s.tone}"><span aria-hidden="true">${s.icon}</span>${s.label}</span>`;
};
const LEVELS = { info: "инфо", warning: "внимание", error: "ошибка" };

function toast(message, tone = "info") {
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.tone = tone;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), 4500);
}

// ---------------------------------------------------------------------------
// API

async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Portal-Request": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin" });
  if (resp.status === 401 && path !== "/api/auth") {
    await askToken();
    return api(path, { method, body });
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      detail = typeof data.detail === "string" ? data.detail
        : (data.detail ?? []).map((d) => {
          const msg = String(d.msg ?? "").replace(/^Value error, /, "");
          const field = (d.loc ?? []).slice(1).join(".");
          return field ? `${field}: ${msg}` : msg;
        }).join("; ") || detail;
    } catch { /* keep status text */ }
    throw new Error(detail);
  }
  return resp.status === 204 ? null : resp.json();
}

let tokenPromise = null;
function askToken() {
  tokenPromise ??= new Promise((resolve) => {
    const dialog = $("#token-dialog");
    const form = $("#token-form");
    const error = $("#token-error");
    form.onsubmit = async (e) => {
      e.preventDefault();
      try {
        await api("/api/auth", { method: "POST", body: { token: form.token.value } });
        dialog.close();
        tokenPromise = null;
        connectLive();
        resolve();
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    };
    dialog.showModal();
  });
  return tokenPromise;
}

// ---------------------------------------------------------------------------
// live state

const store = {
  snapshot: null,
  cpuHistory: new Map(), // agent id -> recent cpu values for lane traces
  systemHistory: [],
  unseenAlerts: 0,
  listeners: new Set(),
};
const LANE_POINTS = 90;

function pushHistory(snapshot) {
  for (const a of snapshot.agents) {
    const list = store.cpuHistory.get(a.id) ?? [];
    list.push(a.cpu);
    if (list.length > LANE_POINTS) list.shift();
    store.cpuHistory.set(a.id, list);
  }
  const s = snapshot.system;
  if (s && s.ts) {
    store.systemHistory.push([s.ts, s.cpu, s.mem_percent, s.gpus?.length ? Math.max(...s.gpus.map((g) => g.util)) : null]);
    if (store.systemHistory.length > 1800) store.systemHistory.shift();
  }
}

let socket = null;
let retryDelay = 1000;
function connectLive() {
  if (socket && socket.readyState <= 1) return;
  const conn = $("#conn");
  const proto = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${proto}://${location.host}/ws/live`);
  socket.onopen = () => {
    retryDelay = 1000;
    conn.dataset.state = "live";
    conn.textContent = "В эфире";
  };
  socket.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "snapshot") {
      store.snapshot = data;
      pushHistory(data);
      store.listeners.forEach((fn) => fn({ type: "snapshot" }));
    } else if (data.type === "event") {
      if (data.event.level !== "info" && !location.hash.startsWith("#/events")) {
        store.unseenAlerts += 1;
        renderAlertBadge();
      }
      store.listeners.forEach((fn) => fn({ type: "event", event: data.event }));
    }
  };
  socket.onclose = (e) => {
    conn.dataset.state = "down";
    conn.textContent = "Нет связи";
    if (e.code === 4401) { askToken(); return; }
    setTimeout(connectLive, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 15000);
  };
}

function renderAlertBadge() {
  const badge = $("#alert-count");
  badge.hidden = store.unseenAlerts === 0;
  badge.textContent = store.unseenAlerts > 99 ? "99+" : String(store.unseenAlerts);
  badge.setAttribute("aria-label", `${store.unseenAlerts} новых предупреждений`);
}

function onLive(fn) {
  store.listeners.add(fn);
  return () => store.listeners.delete(fn);
}

// ---------------------------------------------------------------------------
// router

let cleanup = [];
const routes = [
  [/^#?\/?$/, renderOverview, "overview"],
  [/^#\/agents\/new$/, (m) => renderForm(null), "overview"],
  [/^#\/agents\/([^/]+)\/edit$/, (m) => renderForm(decodeURIComponent(m[1])), "overview"],
  [/^#\/agents\/([^/]+)$/, (m) => renderAgent(decodeURIComponent(m[1])), "overview"],
  [/^#\/models$/, renderModels, "models"],
  [/^#\/discover$/, renderDiscover, "discover"],
  [/^#\/events$/, renderEvents, "events"],
];

async function route() {
  cleanup.forEach((fn) => fn());
  cleanup = [];
  const hash = location.hash || "#/";
  for (const [rx, render, nav] of routes) {
    const m = hash.match(rx);
    if (!m) continue;
    $$(".rail [data-nav]").forEach((a) => a.toggleAttribute("aria-current", a.dataset.nav === nav));
    try {
      await render(m);
    } catch (err) {
      view.innerHTML = `<div class="empty"><h1>Не удалось открыть страницу</h1><p>${esc(err.message)}</p>
        <p><a href="#/">Вернуться к обзору</a></p></div>`;
    }
    return;
  }
  location.hash = "#/";
}

// ---------------------------------------------------------------------------
// overview

async function renderOverview() {
  const snapshot = store.snapshot ?? (await api("/api/overview"));
  store.snapshot ??= snapshot;
  if (!store.systemHistory.length) {
    const hist = await api("/api/system/history");
    store.systemHistory = hist.points;
  }
  await Promise.all(snapshot.agents.filter((a) => !store.cpuHistory.has(a.id)).map(async (a) => {
    const m = await api(`/api/agents/${encodeURIComponent(a.id)}/metrics`);
    store.cpuHistory.set(a.id, m.points.slice(-LANE_POINTS).map((p) => p[1]));
  }));

  view.innerHTML = `
    <header class="page-head">
      <div>
        <h1>Обзор</h1>
        <p class="lede" id="summary"></p>
      </div>
      <a class="button primary" href="#/agents/new">Добавить агента</a>
    </header>
    <section class="machine" aria-label="Ресурсы компьютера">
      <div class="gauges" id="gauges"></div>
      <div class="machine-chart" id="system-chart"></div>
    </section>
    <div class="overview-grid">
      <section aria-labelledby="lanes-title">
        <div class="section-head">
          <h2 id="lanes-title">Агенты</h2>
          <div class="filters">
            <label class="visually-hidden" for="lane-filter">Поиск агентов</label>
            <input id="lane-filter" type="search" placeholder="Поиск по имени или тегу">
          </div>
        </div>
        <div class="lanes" id="lanes"></div>
      </section>
      <aside aria-labelledby="feed-title">
        <div class="section-head"><h2 id="feed-title">Последние события</h2><a href="#/events">Весь журнал</a></div>
        <ol class="feed" id="feed"></ol>
      </aside>
    </div>`;

  const filter = $("#lane-filter");
  filter.addEventListener("input", () => paintLanes(true));
  const events = await api("/api/events?limit=12");
  paintFeed(events);
  paintSystem();
  paintLanes(true);

  cleanup.push(onLive((msg) => {
    if (msg.type === "snapshot") { paintSystem(); paintLanes(false); }
    if (msg.type === "event") {
      events.unshift(msg.event);
      events.length = Math.min(events.length, 12);
      paintFeed(events);
    }
  }));
  const onResize = () => paintSystemChart();
  window.addEventListener("resize", onResize);
  cleanup.push(() => window.removeEventListener("resize", onResize));
}

function paintSystem() {
  const s = store.snapshot?.system;
  const agents = store.snapshot?.agents ?? [];
  const running = agents.filter((a) => a.state === "running").length;
  const broken = agents.filter((a) => ["crashed", "missing", "backoff"].includes(a.state) || a.health?.ok === false).length;
  $("#summary").textContent = agents.length
    ? `Работает ${running} из ${agents.length}${broken ? `, требуют внимания: ${broken}` : ""}.`
    : "Агентов пока нет.";
  if (!s || s.cpu == null) { $("#gauges").innerHTML = `<p class="muted">Собираю первые метрики…</p>`; return; }
  const gpu = s.gpus?.[0];
  const tiles = [
    { name: "Процессор", value: `${Math.round(s.cpu)}%`, pct: s.cpu,
      sub: `${s.cpu_count} потоков${s.load ? `, нагрузка ${s.load[0].toFixed(2)}` : ""}` },
    { name: "Память", value: `${Math.round(s.mem_percent)}%`, pct: s.mem_percent,
      sub: `${s.mem_used_gb.toFixed(1)} из ${s.mem_total_gb.toFixed(1)} ГБ` },
    gpu
      ? { name: "Видеокарта", value: `${Math.round(gpu.util)}%`, pct: gpu.util,
          sub: `${fmtMb(gpu.mem_used_mb)} из ${fmtMb(gpu.mem_total_mb)}, ${Math.round(gpu.temp_c)}°C` }
      : { name: "Видеокарта", value: "—", pct: null, sub: "nvidia-smi не найден" },
    { name: "Диск", value: s.disk_percent == null ? "—" : `${Math.round(s.disk_percent)}%`, pct: s.disk_percent,
      sub: s.disk_total_gb ? `${s.disk_used_gb.toFixed(0)} из ${s.disk_total_gb.toFixed(0)} ГБ` : "" },
    { name: "Сеть", value: fmtRate(s.net_rx + s.net_tx), pct: null,
      sub: `приём ${fmtRate(s.net_rx)}, отдача ${fmtRate(s.net_tx)}` },
  ];
  $("#gauges").innerHTML = tiles.map((t) => {
    const tone = t.pct == null ? "" : t.pct >= 90 ? "crit" : t.pct >= 75 ? "warn" : "ok";
    return `<div class="gauge">
      <div class="gauge-name">${esc(t.name)}</div>
      <div class="gauge-value">${esc(t.value)}</div>
      ${t.pct == null ? `<div class="meter meter-empty"></div>`
        : `<div class="meter" data-tone="${tone}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(t.pct)}" aria-label="${esc(t.name)}"><span style="width:${Math.min(100, t.pct)}%"></span></div>`}
      <div class="gauge-sub">${esc(t.sub)}</div>
    </div>`;
  }).join("");
  paintSystemChart();
}

function paintSystemChart() {
  const el = $("#system-chart");
  if (!el) return;
  const pts = store.systemHistory.slice(-450);
  if (pts.length < 2) { el.innerHTML = `<p class="muted chart-placeholder">График появится через несколько секунд.</p>`; return; }
  const series = [
    { name: "ЦП", color: "var(--series-1)", values: pts.map((p) => p[1]) },
    { name: "ОЗУ", color: "var(--series-2)", values: pts.map((p) => p[2]) },
  ];
  if (pts.some((p) => p[3] != null)) series.push({ name: "GPU", color: "var(--series-3)", values: pts.map((p) => p[3]) });
  lineChart(el, pts.map((p) => p[0]), series, { yMax: 100, unit: "%", height: 170,
    label: "Загрузка процессора и памяти за последние 15 минут" });
}

function laneMatches(agent, query) {
  if (!query) return true;
  const hay = `${agent.name} ${agent.id} ${agent.description} ${agent.tags.join(" ")}`.toLowerCase();
  return hay.includes(query.toLowerCase());
}

function paintLanes(full) {
  const root = $("#lanes");
  if (!root) return;
  const agents = (store.snapshot?.agents ?? []).filter((a) => laneMatches(a, $("#lane-filter").value.trim()));
  const ids = agents.map((a) => a.id).join("|");
  if (full || root.dataset.ids !== ids) {
    root.dataset.ids = ids;
    if (!agents.length) {
      root.innerHTML = store.snapshot?.agents.length
        ? `<p class="empty-inline">Под фильтр ничего не подходит.</p>`
        : `<div class="empty-inline"><p>Добавьте первого агента: укажите команду запуска, и портал будет следить за ним.</p>
             <p><a class="button primary" href="#/agents/new">Добавить агента</a> <a class="button" href="#/discover">Найти запущенные</a></p></div>`;
      return;
    }
    root.innerHTML = agents.map((a) => `
      <article class="lane" data-id="${esc(a.id)}">
        <div class="lane-id">
          <h3><a href="#/agents/${encodeURIComponent(a.id)}">${esc(a.name)}</a></h3>
          <p>${esc(a.description || (a.kind === "external" ? "Внешний процесс" : a.id))}</p>
          <div class="lane-meta" data-slot="meta"></div>
        </div>
        <div class="lane-trace" data-slot="trace" title="Загрузка процессора за последние 3 минуты"></div>
        <dl class="lane-stats">
          <div><dt>ЦП</dt><dd data-slot="cpu"></dd></div>
          <div><dt>Память</dt><dd data-slot="mem"></dd></div>
          <div><dt>Аптайм</dt><dd data-slot="uptime"></dd></div>
        </dl>
        <div class="lane-actions" data-slot="actions"></div>
      </article>`).join("");
  }
  for (const a of agents) {
    const row = root.querySelector(`.lane[data-id="${CSS.escape(a.id)}"]`);
    if (!row) continue;
    row.dataset.state = a.state;
    const slot = (name) => row.querySelector(`[data-slot="${name}"]`);
    const health = a.health ? (a.health.ok ? `<span class="chip" data-tone="good">отвечает, ${Math.round(a.health.latency_ms)} мс</span>`
      : `<span class="chip" data-tone="crit">не отвечает</span>`) : "";
    const meta = `${stateBadge(a.state)}${health}${a.restarts ? `<span class="chip">перезапусков: ${a.restarts}</span>` : ""}${a.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}`;
    if (slot("meta").dataset.html !== meta) { slot("meta").innerHTML = meta; slot("meta").dataset.html = meta; }
    const trace = slot("trace");
    const cpu = store.cpuHistory.get(a.id) ?? [];
    // Scale to one full core so a quiet agent looks quiet, not like a mountain range.
    trace.replaceChildren(sparkline(cpu, { height: 48, max: Math.max(100, ...cpu) }));
    slot("cpu").textContent = a.state === "running" ? `${a.cpu.toFixed(0)}%` : "–";
    slot("mem").textContent = a.state === "running" ? fmtMb(a.mem_mb) : "–";
    slot("uptime").textContent = fmtDuration(a.uptime);
    const actions = a.kind === "external" ? `<a class="button small" href="#/agents/${encodeURIComponent(a.id)}">Подробнее</a>`
      : ["running", "starting", "backoff"].includes(a.state)
        ? `<button class="small" data-act="restart">Перезапустить</button><button class="small" data-act="stop">Остановить</button>`
        : `<button class="small primary" data-act="start">Запустить</button>`;
    const box = slot("actions");
    if (box.dataset.html !== actions) {
      box.innerHTML = actions;
      box.dataset.html = actions;
      $$("button[data-act]", box).forEach((b) => b.addEventListener("click", () => agentAction(a.id, b.dataset.act, b)));
    }
  }
}

function paintFeed(events) {
  const root = $("#feed");
  if (!root) return;
  const names = new Map((store.snapshot?.agents ?? []).map((a) => [a.id, a.name]));
  root.innerHTML = events.length ? events.map((e) => `
    <li data-level="${esc(e.level)}">
      <time datetime="${new Date(e.ts * 1000).toISOString()}">${fmtTime(e.ts)}</time>
      <div>${e.agent_id ? `<a href="#/agents/${encodeURIComponent(e.agent_id)}">${esc(names.get(e.agent_id) ?? e.agent_id)}</a>: ` : ""}${esc(e.message)}</div>
    </li>`).join("") : `<li class="muted">Событий пока нет.</li>`;
}

const ACTION_DONE = { start: "Запущен", stop: "Остановлен", restart: "Перезапущен" };
async function agentAction(id, action, button) {
  if (button) button.disabled = true;
  try {
    const status = await api(`/api/agents/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    toast(`${ACTION_DONE[action]}: ${status.name}`, "good");
    if (store.snapshot) {
      const i = store.snapshot.agents.findIndex((a) => a.id === id);
      if (i >= 0) store.snapshot.agents[i] = status;
    }
    store.listeners.forEach((fn) => fn({ type: "snapshot" }));
  } catch (err) {
    toast(err.message, "crit");
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// agent detail

const WINDOWS = [[300, "5 мин"], [900, "15 мин"], [3600, "1 час"]];

async function renderAgent(id) {
  const enc = encodeURIComponent(id);
  let agent = await api(`/api/agents/${enc}`);
  const metrics = (await api(`/api/agents/${enc}/metrics`)).points;
  let windowSec = Number(sessionStorage.getItem("window") || 900);
  let tab = sessionStorage.getItem("tab") || "logs";

  view.innerHTML = `
    <header class="page-head agent-head">
      <div>
        <p class="crumb"><a href="#/">Обзор</a></p>
        <h1>${esc(agent.name)}</h1>
        <p class="lede">${esc(agent.description || "")}</p>
        <div class="lane-meta" id="agent-meta"></div>
      </div>
      <div class="head-actions" id="agent-actions"></div>
    </header>
    <dl class="facts" id="facts"></dl>
    <section aria-label="Метрики">
      <div class="filters" role="group" aria-label="Период">
        ${WINDOWS.map(([s, l]) => `<button class="seg" data-window="${s}" aria-pressed="${s === windowSec}">${l}</button>`).join("")}
      </div>
      <div class="charts">
        <figure><figcaption>Процессор, % одного ядра</figcaption><div id="chart-cpu"></div></figure>
        <figure><figcaption>Память (RSS всего дерева процессов)</figcaption><div id="chart-mem"></div></figure>
        <figure id="gpu-fig" hidden><figcaption>Видеопамять</figcaption><div id="chart-gpu"></div></figure>
      </div>
    </section>
    <section class="tabs-section">
      <div class="tabs" role="tablist">
        <button role="tab" data-tab="logs">Логи</button>
        <button role="tab" data-tab="task">Задача</button>
        <button role="tab" data-tab="events">События</button>
        <button role="tab" data-tab="config">Конфигурация</button>
      </div>
      <div id="tab-body" class="tab-body"></div>
    </section>`;

  const paintHead = () => {
    $("#agent-meta").innerHTML = `${stateBadge(agent.state)}<span class="chip">${agent.kind === "external" ? "внешний" : "управляемый"}</span>${agent.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}`;
    const managed = agent.kind === "process";
    const live = ["running", "starting", "backoff"].includes(agent.state);
    const html = `${managed ? (live
        ? `<button data-act="restart">Перезапустить</button><button data-act="stop">Остановить</button>`
        : `<button class="primary" data-act="start">Запустить</button>`) : ""}
      <a class="button" href="#/agents/${enc}/edit">Изменить</a>
      <button class="danger" data-act="delete">Удалить</button>`;
    const box = $("#agent-actions");
    if (box.dataset.html !== html) {
      box.innerHTML = html;
      box.dataset.html = html;
      $$("[data-act]", box).forEach((b) => b.addEventListener("click", async () => {
        if (b.dataset.act === "delete") {
          if (!confirm(`Удалить агента «${agent.name}»? Процесс будет остановлен, логи останутся на диске.`)) return;
          try {
            await api(`/api/agents/${enc}`, { method: "DELETE" });
            store.snapshot = null;
            store.cpuHistory.delete(id);
            toast(`Удалён: ${agent.name}`, "good");
            location.hash = "#/";
          } catch (err) { toast(err.message, "crit"); }
          return;
        }
        await agentAction(id, b.dataset.act, b);
      }));
    }
    const h = agent.health;
    const facts = [
      ["PID", agent.pids.length ? agent.pids.join(", ") : "–"],
      ["Аптайм", fmtDuration(agent.uptime)],
      ["Перезапуски", agent.kind === "process" ? String(agent.restarts) : "–"],
      ["Код выхода", agent.exit_code ?? "–"],
      ["Потоки", agent.threads || "–"],
      ["Здоровье", !agent.has_health ? "не настроено" : !h ? "ещё не проверялось"
        : h.ok ? `отвечает за ${Math.round(h.latency_ms)} мс` : `не отвечает (${h.error})`],
    ];
    $("#facts").innerHTML = facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("");
  };

  const paintCharts = () => {
    const cut = Date.now() / 1000 - windowSec;
    const pts = metrics.filter((p) => p[0] >= cut);
    const times = pts.map((p) => p[0]);
    const empty = (el) => { el.innerHTML = `<p class="muted chart-placeholder">Нет данных за этот период — агент не запущен.</p>`; };
    if (pts.length < 2) { empty($("#chart-cpu")); empty($("#chart-mem")); return; }
    lineChart($("#chart-cpu"), times, [{ name: "ЦП", color: "var(--series-1)", values: pts.map((p) => p[1]) }],
      { unit: "%", label: "Загрузка процессора" });
    lineChart($("#chart-mem"), times, [{ name: "Память", color: "var(--series-1)", values: pts.map((p) => p[2]) }],
      { format: (v) => fmtMb(v), label: "Потребление памяти",
        threshold: agent.max_memory_mb ? { value: agent.max_memory_mb, label: `лимит ${fmtMb(agent.max_memory_mb)}` } : null });
    const hasGpu = pts.some((p) => p[3] > 0);
    $("#gpu-fig").hidden = !hasGpu;
    if (hasGpu) {
      lineChart($("#chart-gpu"), times, [{ name: "Видеопамять", color: "var(--series-1)", values: pts.map((p) => p[3]) }],
        { format: (v) => fmtMb(v), label: "Видеопамять" });
    }
  };

  $$("[data-window]").forEach((b) => b.addEventListener("click", () => {
    windowSec = Number(b.dataset.window);
    sessionStorage.setItem("window", String(windowSec));
    $$("[data-window]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    paintCharts();
  }));

  const tabs = { logs: tabLogs, task: tabTask, events: tabEvents, config: tabConfig };
  let tabCleanup = null;
  const openTab = (name) => {
    tab = tabs[name] ? name : "logs";
    sessionStorage.setItem("tab", tab);
    $$("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    tabCleanup?.();
    tabCleanup = tabs[tab]($("#tab-body"), agent) ?? null;
  };
  $$("[data-tab]").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));
  cleanup.push(() => tabCleanup?.());

  paintHead();
  paintCharts();
  openTab(tab);

  cleanup.push(onLive((msg) => {
    if (msg.type !== "snapshot") return;
    const fresh = store.snapshot.agents.find((a) => a.id === id);
    if (!fresh) return;
    agent = { ...agent, ...fresh };
    metrics.push([store.snapshot.ts, fresh.cpu, fresh.mem_mb, fresh.gpu_mem_mb]);
    paintHead();
    paintCharts();
  }));
  const onResize = () => paintCharts();
  window.addEventListener("resize", onResize);
  cleanup.push(() => window.removeEventListener("resize", onResize));
}

const LOG_LEVEL = /(error|exception|traceback|ошибк|fatal|critical)/i;
const LOG_WARN = /(warn|внимани)/i;

function tabLogs(root, agent) {
  const enc = encodeURIComponent(agent.id);
  root.innerHTML = `
    <div class="toolbar">
      <label class="visually-hidden" for="log-filter">Фильтр логов</label>
      <input id="log-filter" type="search" placeholder="Фильтр строк">
      <label class="check"><input type="checkbox" id="log-follow" checked> Следить за концом</label>
      <span class="spacer"></span>
      <button class="small" id="log-download">Скачать</button>
      <button class="small" id="log-clear">Очистить</button>
    </div>
    <pre class="logs" id="logs" tabindex="0" aria-label="Логи агента"></pre>`;
  const pre = $("#logs");
  const filter = $("#log-filter");
  let lines = [];

  const lineEl = (l) => {
    const div = document.createElement("div");
    div.className = l.text.startsWith("[portal]") ? "log-portal" : LOG_LEVEL.test(l.text) ? "log-error"
      : LOG_WARN.test(l.text) ? "log-warn" : "";
    if (l.ts) {
      const t = document.createElement("span");
      t.className = "log-ts";
      t.textContent = fmtTime(l.ts) + " ";
      div.append(t);
    }
    div.append(document.createTextNode(l.text));
    return div;
  };
  const visible = (l) => !filter.value || l.text.toLowerCase().includes(filter.value.toLowerCase());
  const repaint = () => {
    pre.replaceChildren(...lines.filter(visible).map(lineEl));
    if (!lines.length) pre.textContent = "Логов пока нет.";
    if ($("#log-follow").checked) pre.scrollTop = pre.scrollHeight;
  };
  filter.addEventListener("input", repaint);

  api(`/api/agents/${enc}/logs?tail=1000`).then((data) => { lines = data.lines; repaint(); });

  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/agents/${enc}/logs`);
  ws.onmessage = (msg) => {
    const line = JSON.parse(msg.data);
    if (!lines.length) pre.textContent = "";
    lines.push(line);
    if (lines.length > 2000) lines.shift();
    if (visible(line)) {
      pre.append(lineEl(line));
      while (pre.childElementCount > 2000) pre.firstElementChild.remove();
      if ($("#log-follow")?.checked) pre.scrollTop = pre.scrollHeight;
    }
  };

  $("#log-clear").addEventListener("click", async () => {
    if (!confirm("Очистить логи агента? Файл лога на диске тоже будет очищен.")) return;
    await api(`/api/agents/${enc}/logs`, { method: "DELETE" });
    lines = [];
    repaint();
  });
  $("#log-download").addEventListener("click", () => {
    const blob = new Blob([lines.map((l) => l.text).join("\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${agent.id}.log`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
  return () => ws.close();
}

function tabTask(root, agent) {
  if (!agent.has_task) {
    root.innerHTML = `<div class="empty-inline"><p>Чтобы отправлять агенту задачи отсюда, укажите в настройках <strong>адрес для задач</strong> —
      HTTP-эндпоинт, который принимает POST с JSON <code>{"input": "…"}</code>.</p>
      <p><a class="button" href="#/agents/${encodeURIComponent(agent.id)}/edit">Изменить настройки</a></p></div>`;
    return;
  }
  root.innerHTML = `
    <form class="task-form" id="task-form">
      <label for="task-input">Задача для агента</label>
      <textarea id="task-input" rows="4" required placeholder="Например: найди в заметках всё про миграцию базы"></textarea>
      <div class="actions"><button class="primary" type="submit">Отправить задачу</button><span class="muted" id="task-status"></span></div>
    </form>
    <ol class="task-history" id="task-history"></ol>`;
  const history = $("#task-history");
  $("#task-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("#task-input").value.trim();
    if (!input) return;
    const button = e.submitter;
    button.disabled = true;
    $("#task-status").textContent = "Агент работает…";
    try {
      const res = await api(`/api/agents/${encodeURIComponent(agent.id)}/task`, { method: "POST", body: { input } });
      const li = document.createElement("li");
      const head = document.createElement("div");
      head.className = "task-head";
      head.textContent = `${input} — HTTP ${res.status}, ${res.elapsed_s} с`;
      const r = res.response;
      const body = document.createElement("pre");
      if (r && typeof r === "object" && typeof r.answer === "string") {
        // Agents that return {"answer": ...} get a readable reply plus a short stats line.
        body.className = "task-answer";
        body.textContent = r.answer;
        const meta = [r.model, r.tokens && `токенов: ${r.tokens}`, r.tokens_per_s && `${r.tokens_per_s} ток/с`].filter(Boolean);
        if (meta.length) head.textContent += `, ${meta.join(", ")}`;
      } else {
        body.textContent = typeof r === "string" ? r : JSON.stringify(r, null, 2);
      }
      li.append(head, body);
      history.prepend(li);
      $("#task-status").textContent = "";
    } catch (err) {
      $("#task-status").textContent = err.message;
    } finally {
      button.disabled = false;
    }
  });
}

function tabEvents(root, agent) {
  root.innerHTML = `<ol class="feed wide" id="agent-feed"><li class="muted">Загрузка…</li></ol>`;
  api(`/api/events?agent_id=${encodeURIComponent(agent.id)}&limit=100`).then((events) => {
    $("#agent-feed").innerHTML = events.length ? events.map((e) => `
      <li data-level="${esc(e.level)}"><time>${fmtDateTime(e.ts)}</time><div>${esc(e.message)}</div></li>`).join("")
      : `<li class="muted">Событий нет.</li>`;
  });
}

function tabConfig(root, agent) {
  root.innerHTML = `<p class="muted">Так агент записан в конфигурации. Изменить можно через кнопку «Изменить».</p>
    <pre class="config">${esc(JSON.stringify(agent.spec, null, 2))}</pre>`;
}

// ---------------------------------------------------------------------------
// create / edit form

async function renderForm(id) {
  const editing = id != null;
  let spec = editing ? (await api(`/api/agents/${encodeURIComponent(id)}`)).spec : {
    id: "", name: "", description: "", kind: "process", command: "", cwd: "", env: {}, autostart: false,
    restart_policy: "on-failure", max_restarts: 5, max_memory_mb: null, process_match: "", health_url: "",
    task_url: "", tags: [],
  };
  const draft = sessionStorage.getItem("agent-draft");
  if (!editing && draft) {
    spec = { ...spec, ...JSON.parse(draft) };
    sessionStorage.removeItem("agent-draft");
  }
  const cmd = Array.isArray(spec.command) ? spec.command.join(" ") : spec.command ?? "";
  const env = Object.entries(spec.env ?? {}).map(([k, v]) => `${k}=${v}`).join("\n");

  view.innerHTML = `
    <header class="page-head">
      <div>
        <p class="crumb"><a href="${editing ? `#/agents/${encodeURIComponent(id)}` : "#/"}">${editing ? esc(spec.name) : "Обзор"}</a></p>
        <h1>${editing ? "Настройки агента" : "Новый агент"}</h1>
      </div>
    </header>
    ${editing ? "" : `<section class="quickstart" id="quickstart" aria-labelledby="qs-title">
      <h2 id="qs-title">Агент на модели из Ollama</h2>
      <p class="muted" id="qs-hint">Проверяю, какие модели есть в Ollama…</p>
      <div class="qs-row" hidden>
        <label>Модель <select id="qs-model"></select></label>
        <button type="button" class="primary" id="qs-fill">Заполнить форму</button>
      </div>
    </section>`}
    <form class="agent-form" id="agent-form" novalidate>
      <fieldset>
        <legend>Как портал работает с агентом</legend>
        <label class="radio"><input type="radio" name="kind" value="process" ${spec.kind === "process" ? "checked" : ""}>
          <span><strong>Запускать и следить</strong><br>Портал сам стартует команду, собирает логи и перезапускает при сбоях.</span></label>
        <label class="radio"><input type="radio" name="kind" value="external" ${spec.kind === "external" ? "checked" : ""}>
          <span><strong>Только наблюдать</strong><br>Агент запускается без портала; портал находит процесс по командной строке.</span></label>
      </fieldset>
      <div class="grid2">
        <label>Название <input name="name" required maxlength="120" value="${esc(spec.name)}"></label>
        <label>Идентификатор <input name="id" required pattern="[a-z0-9][a-z0-9_-]{0,63}" value="${esc(spec.id)}" ${editing ? "readonly" : ""}>
          <small>Латиница, цифры, «-» и «_». Используется в адресах и именах файлов логов.</small></label>
      </div>
      <label>Описание <input name="description" value="${esc(spec.description)}"></label>
      <div data-kind="process">
        <label>Команда запуска <input name="command" value="${esc(cmd)}" placeholder="{python} my_agent.py --port 8000">
          <small>Команда, которой вы запускаете агента в терминале. {python} подставит интерпретатор портала, {examples} — папку с примерами агентов.</small></label>
        <label>Рабочий каталог <input name="cwd" value="${esc(spec.cwd ?? "")}" placeholder="по умолчанию — каталог портала"></label>
        <label>Переменные окружения <textarea name="env" rows="3" placeholder="OPENAI_BASE_URL=http://127.0.0.1:11434/v1">${esc(env)}</textarea>
          <small>По одной на строку: ИМЯ=значение.</small></label>
        <div class="grid3">
          <label>При завершении
            <select name="restart_policy">
              ${[["on-failure", "перезапускать при ошибке"], ["always", "перезапускать всегда"], ["never", "не перезапускать"]]
                .map(([v, l]) => `<option value="${v}" ${spec.restart_policy === v ? "selected" : ""}>${l}</option>`).join("")}
            </select></label>
          <label>Попыток подряд <input name="max_restarts" type="number" min="0" max="1000" value="${esc(spec.max_restarts)}"></label>
          <label>Лимит памяти, МБ <input name="max_memory_mb" type="number" min="1" value="${esc(spec.max_memory_mb ?? "")}" placeholder="без лимита">
            <small>При превышении агент перезапускается.</small></label>
        </div>
        <label class="check"><input type="checkbox" name="autostart" ${spec.autostart ? "checked" : ""}> Запускать вместе с порталом</label>
      </div>
      <div data-kind="external">
        <label>Шаблон командной строки <input name="process_match" value="${esc(spec.process_match ?? "")}" placeholder="^\\S*ollama\\s+serve">
          <small>Регулярное выражение. Все процессы, чья командная строка ему соответствует, считаются этим агентом. Начинайте с ^, чтобы не совпадали посторонние команды.</small></label>
      </div>
      <div class="grid2">
        <label>Адрес проверки здоровья <input name="health_url" type="url" value="${esc(spec.health_url ?? "")}" placeholder="http://127.0.0.1:8000/health">
          <small>GET-запрос; ответ с кодом ниже 400 считается успехом.</small></label>
        <label>Адрес для задач <input name="task_url" type="url" value="${esc(spec.task_url ?? "")}" placeholder="http://127.0.0.1:8000/task">
          <small>POST с JSON {"input": "…"}; включает вкладку «Задача».</small></label>
      </div>
      <label>Теги <input name="tags" value="${esc(spec.tags.join(", "))}" placeholder="rag, ночные задачи"></label>
      <p class="form-error" id="form-error" hidden></p>
      <div class="actions">
        <button class="primary" type="submit">${editing ? "Сохранить" : "Добавить агента"}</button>
        <a class="button ghost" href="${editing ? `#/agents/${encodeURIComponent(id)}` : "#/"}">Отмена</a>
      </div>
    </form>`;

  const form = $("#agent-form");
  if (!editing) setupQuickstart(form);
  const syncKind = () => {
    const kind = form.kind.value;
    $$("[data-kind]", form).forEach((el) => { el.hidden = el.dataset.kind !== kind; });
  };
  $$("input[name=kind]", form).forEach((r) => r.addEventListener("change", syncKind));
  syncKind();
  if (!editing) {
    form.name.addEventListener("input", () => {
      if (form.id.dataset.touched) return;
      form.id.value = translit(form.name.value);
    });
    form.id.addEventListener("input", () => { form.id.dataset.touched = "1"; });
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const opt = (k) => (f.get(k) || "").toString().trim() || null;
    const body = {
      id: opt("id") ?? "", name: opt("name") ?? "", description: opt("description") ?? "",
      kind: f.get("kind"), command: opt("command"), cwd: opt("cwd"),
      env: parseEnv((f.get("env") || "").toString()),
      autostart: f.get("autostart") === "on", restart_policy: f.get("restart_policy"),
      max_restarts: Number(f.get("max_restarts") || 0), max_memory_mb: opt("max_memory_mb") ? Number(opt("max_memory_mb")) : null,
      process_match: opt("process_match"), health_url: opt("health_url"), task_url: opt("task_url"),
      tags: (f.get("tags") || "").toString().split(",").map((t) => t.trim()).filter(Boolean),
    };
    const error = $("#form-error");
    error.hidden = true;
    const missing = body.kind === "process" && !body.command ? "Укажите команду запуска — или выберите «Только наблюдать», если агент запускается без портала."
      : body.kind === "external" && !body.process_match && !body.health_url ? "Укажите шаблон командной строки или адрес проверки здоровья."
      : !body.name ? "Укажите название агента." : null;
    if (missing) {
      error.textContent = missing;
      error.hidden = false;
      return;
    }
    try {
      const saved = editing
        ? await api(`/api/agents/${encodeURIComponent(id)}`, { method: "PUT", body })
        : await api("/api/agents", { method: "POST", body });
      store.snapshot = null;
      toast(editing ? "Настройки сохранены" : `Агент добавлен: ${saved.name}`, "good");
      location.hash = `#/agents/${encodeURIComponent(saved.id)}`;
    } catch (err) {
      error.textContent = `Не сохранено: ${err.message}`;
      error.hidden = false;
    }
  });
}

// Fills the form with a ready-made agent (examples/ollama_agent.py) for a chosen Ollama model.
async function setupQuickstart(form) {
  const hint = $("#qs-hint");
  const row = $(".qs-row");
  let models = [];
  let backendUrl = "http://127.0.0.1:11434";
  try {
    const backends = await api("/api/backends");
    const ollama = backends.find((b) => b.type === "ollama" && b.online);
    if (ollama) {
      models = ollama.models.map((m) => m.name);
      backendUrl = ollama.url;
    }
  } catch { /* hint below explains */ }
  if (!models.length) {
    hint.textContent = "Ollama не отвечает или в ней нет моделей. Запустите Ollama и скачайте модель: ollama pull qwen2.5:7b — затем обновите страницу.";
    return;
  }
  hint.textContent = "Выберите модель — портал сам подставит команду запуска, порт и адреса. Останется нажать «Добавить агента».";
  row.hidden = false;
  const select = $("#qs-model");
  select.innerHTML = models.map((m) => `<option>${esc(m)}</option>`).join("");
  select.value = models.find((m) => m.toLowerCase().includes("qwen")) ?? models[0];

  $("#qs-fill").addEventListener("click", async () => {
    const model = select.value;
    const used = new Set();
    const agents = await api("/api/agents");
    await Promise.all(agents.map(async (a) => {
      const { spec } = await api(`/api/agents/${encodeURIComponent(a.id)}`);
      const text = `${spec.command ?? ""} ${spec.health_url ?? ""} ${spec.task_url ?? ""}`;
      for (const m of text.matchAll(/(?:--port\s+|:)(\d{4,5})\b/g)) used.add(Number(m[1]));
    }));
    let port = 8720;
    while (used.has(port)) port += 1;
    const base = model.split(":")[0].replace(/[^a-z0-9]+/gi, "-").toLowerCase();
    let id = `${base}-agent`;
    for (let n = 2; agents.some((a) => a.id === id); n += 1) id = `${base}-agent-${n}`;
    form.querySelector("input[name=kind][value=process]").checked = true;
    form.querySelector("input[name=kind][value=process]").dispatchEvent(new Event("change"));
    form.name.value = `Ассистент на ${model}`;
    form.id.value = id;
    form.id.dataset.touched = "1";
    form.description.value = `Отвечает на задачи через модель ${model} в Ollama`;
    form.command.value = `{python} {examples}/ollama_agent.py --port ${port}`;
    form.env.value = `MODEL=${model}\nOLLAMA_URL=${backendUrl}`;
    form.restart_policy.value = "on-failure";
    form.autostart.checked = true;
    form.health_url.value = `http://127.0.0.1:${port}/health`;
    form.task_url.value = `http://127.0.0.1:${port}/task`;
    form.tags.value = "ollama";
    toast("Форма заполнена — проверьте и нажмите «Добавить агента»", "good");
    form.querySelector("button[type=submit]").focus();
  });
}

function parseEnv(text) {
  const env = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq > 0) env[line.slice(0, eq).trim()] = line.slice(eq + 1);
  }
  return env;
}

const TRANSLIT = { а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i", й: "y", к: "k",
  л: "l", м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t", у: "u", ф: "f", х: "h", ц: "c", ч: "ch", ш: "sh",
  щ: "sch", ъ: "", ы: "y", ь: "", э: "e", ю: "yu", я: "ya" };
function translit(text) {
  return text.toLowerCase().split("").map((c) => TRANSLIT[c] ?? c).join("")
    .replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
}

// ---------------------------------------------------------------------------
// models

async function renderModels() {
  view.innerHTML = `
    <header class="page-head">
      <div><h1>Модели</h1><p class="lede">Локальные серверы моделей: что установлено и что сейчас загружено в память.</p></div>
      <div class="head-actions"><button id="refresh">Обновить</button><button id="edit-backends">Изменить список серверов</button></div>
    </header>
    <div id="backends"><p class="muted">Опрашиваю серверы…</p></div>`;
  const paint = async () => {
    const backends = await api("/api/backends");
    const root = $("#backends");
    if (!backends.length) {
      root.innerHTML = `<div class="empty-inline"><p>Серверы моделей не настроены. Добавьте Ollama, LM Studio, llama.cpp или vLLM.</p></div>`;
      return backends;
    }
    root.innerHTML = backends.map((b, i) => `
      <section class="backend" data-online="${b.online}">
        <div class="section-head">
          <h2>${esc(b.name)}</h2>
          <span class="state" data-tone="${b.online ? "good" : "crit"}"><span aria-hidden="true">${b.online ? "●" : "✕"}</span>${b.online ? "доступен" : "недоступен"}</span>
        </div>
        <p class="muted">${esc(b.url)}${b.version ? `, версия ${esc(b.version)}` : ""}${b.latency_ms != null ? `, ответ за ${Math.round(b.latency_ms)} мс` : ""}</p>
        ${b.online ? "" : `<p class="form-error">Сервер не ответил: ${esc(b.error)}. Проверьте, что он запущен и адрес верный.</p>`}
        ${b.running.length ? `<h3>Загружены в память</h3><table><thead><tr><th>Модель</th><th class="num">Размер</th><th class="num">В видеопамяти</th><th>Выгрузится</th></tr></thead><tbody>
          ${b.running.map((m) => `<tr><td>${esc(m.name)}</td><td class="num">${m.size_gb} ГБ</td><td class="num">${m.vram_gb} ГБ</td><td>${m.expires_at ? esc(new Date(m.expires_at).toLocaleTimeString()) : "–"}</td></tr>`).join("")}
          </tbody></table>` : ""}
        ${b.models.length ? `<h3>Установлены (${b.models.length})</h3><table><thead><tr><th>Модель</th><th>Семейство</th><th>Параметры</th><th>Квантование</th><th class="num">Размер</th></tr></thead><tbody>
          ${b.models.map((m) => `<tr><td>${esc(m.name)}</td><td>${esc(m.family ?? m.owned_by ?? "–")}</td><td>${esc(m.parameters ?? "–")}</td><td>${esc(m.quantization ?? "–")}</td><td class="num">${m.size_gb != null ? `${m.size_gb} ГБ` : "–"}</td></tr>`).join("")}
          </tbody></table>
          <form class="prompt-form" data-index="${i}">
            <h3>Проверить модель</h3>
            <div class="grid-prompt">
              <label>Модель <select name="model">${b.models.map((m) => `<option>${esc(m.name)}</option>`).join("")}</select></label>
              <label>Запрос <input name="prompt" required value="Ответь одним предложением: что ты умеешь?"></label>
              <button class="primary" type="submit">Отправить запрос</button>
            </div>
            <div class="prompt-out" hidden></div>
          </form>` : b.online ? `<p class="muted">Моделей нет. Для Ollama: <code>ollama pull llama3.2</code>.</p>` : ""}
      </section>`).join("");
    $$(".prompt-form", root).forEach((form) => form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const out = $(".prompt-out", form);
      const button = e.submitter;
      button.disabled = true;
      out.hidden = false;
      out.textContent = "Модель думает…";
      try {
        const res = await api(`/api/backends/${form.dataset.index}/prompt`, { method: "POST",
          body: { model: form.model.value, prompt: form.prompt.value } });
        out.textContent = "";
        const stats = document.createElement("div");
        stats.className = "muted";
        stats.textContent = `${res.elapsed_s} с${res.tokens ? `, токенов: ${res.tokens}` : ""}${res.tokens_per_s ? `, ${res.tokens_per_s} ток/с` : ""}`;
        const text = document.createElement("p");
        text.textContent = res.text;
        out.append(text, stats);
      } catch (err) {
        out.textContent = err.message;
      } finally {
        button.disabled = false;
      }
    }));
    return backends;
  };
  let backends = await paint();
  $("#refresh").addEventListener("click", async () => { backends = await paint(); });
  $("#edit-backends").addEventListener("click", () => editBackends(backends, async () => { backends = await paint(); }));
}

function editBackends(current, done) {
  const root = $("#backends");
  const row = (b = { name: "", type: "ollama", url: "" }) => `
    <div class="backend-row">
      <label>Название <input name="name" required value="${esc(b.name)}"></label>
      <label>Тип <select name="type">
        <option value="ollama" ${b.type === "ollama" ? "selected" : ""}>Ollama</option>
        <option value="openai" ${b.type === "openai" ? "selected" : ""}>OpenAI-совместимый</option></select></label>
      <label>Адрес <input name="url" type="url" required value="${esc(b.url)}" placeholder="http://127.0.0.1:11434"></label>
      <button type="button" class="ghost small" data-remove>Убрать</button>
    </div>`;
  root.innerHTML = `
    <form class="backends-form" id="backends-form">
      <p class="muted">Для OpenAI-совместимых серверов (LM Studio, llama.cpp, vLLM, LocalAI) укажите адрес с <code>/v1</code>.</p>
      <div id="backend-rows">${current.map(row).join("")}</div>
      <p><button type="button" id="add-backend">Добавить сервер</button></p>
      <p class="form-error" id="backend-error" hidden></p>
      <div class="actions"><button class="primary" type="submit">Сохранить список</button><button type="button" class="ghost" id="cancel-backends">Отмена</button></div>
    </form>`;
  const rows = $("#backend-rows");
  const bindRemove = () => $$("[data-remove]", rows).forEach((b) => { b.onclick = () => b.closest(".backend-row").remove(); });
  bindRemove();
  $("#add-backend").addEventListener("click", () => { rows.insertAdjacentHTML("beforeend", row()); bindRemove(); });
  $("#cancel-backends").addEventListener("click", done);
  $("#backends-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = $$(".backend-row", rows).map((r) => ({
      name: $("[name=name]", r).value.trim(), type: $("[name=type]", r).value, url: $("[name=url]", r).value.trim() }));
    try {
      await api("/api/backends", { method: "PUT", body });
      toast("Список серверов сохранён", "good");
      done();
    } catch (err) {
      $("#backend-error").textContent = `Не сохранено: ${err.message}`;
      $("#backend-error").hidden = false;
    }
  });
}

// ---------------------------------------------------------------------------
// discovery

async function renderDiscover() {
  view.innerHTML = `
    <header class="page-head">
      <div><h1>Обнаружение</h1>
        <p class="lede">Процессы на этом компьютере, похожие на ИИ-рантаймы и агентов: Ollama, llama.cpp, vLLM, LM Studio, CrewAI, LangGraph и другие.</p></div>
      <div class="head-actions"><button id="rescan">Сканировать снова</button></div>
    </header>
    <div id="found"><p class="muted">Сканирую процессы…</p></div>`;
  const paint = async () => {
    const found = await api("/api/discovery");
    const root = $("#found");
    if (!found.length) {
      root.innerHTML = `<div class="empty-inline"><p>Похожих процессов не нашлось. Если ваш агент — обычный скрипт, добавьте его вручную.</p>
        <p><a class="button primary" href="#/agents/new">Добавить агента</a></p></div>`;
      return;
    }
    root.innerHTML = `<table class="found"><thead><tr><th>Что это</th><th class="num">PID</th><th>Командная строка</th><th class="num">Память</th><th>Запущен</th><th></th></tr></thead><tbody>
      ${found.map((p, i) => `<tr>
        <td><strong>${esc(p.label)}</strong><br><span class="muted">${esc(p.name)}${p.user ? `, ${esc(p.user)}` : ""}</span></td>
        <td class="num">${p.pid}</td>
        <td class="cmd" title="${esc(p.cmdline)}">${esc(p.cmdline)}</td>
        <td class="num">${p.mem_mb != null ? fmtMb(p.mem_mb) : "–"}</td>
        <td>${p.started_at ? fmtDateTime(p.started_at) : "–"}</td>
        <td>${p.managed ? `<span class="chip">уже в портале</span>` : `<button class="small" data-watch="${i}">Наблюдать</button>`}</td>
      </tr>`).join("")}</tbody></table>`;
    $$("[data-watch]", root).forEach((b) => b.addEventListener("click", () => {
      const p = found[Number(b.dataset.watch)];
      sessionStorage.setItem("agent-draft", JSON.stringify({
        kind: "external", name: p.label, id: translit(p.label), process_match: p.suggested_match,
        description: `Найден автоматически: ${p.name}`, tags: ["обнаружен"],
        health_url: p.label === "Ollama" ? "http://127.0.0.1:11434/api/version" : "",
      }));
      location.hash = "#/agents/new";
    }));
  };
  await paint();
  $("#rescan").addEventListener("click", paint);
}

// ---------------------------------------------------------------------------
// events

async function renderEvents() {
  store.unseenAlerts = 0;
  renderAlertBadge();
  const agents = store.snapshot?.agents ?? (await api("/api/agents"));
  view.innerHTML = `
    <header class="page-head"><div><h1>Журнал событий</h1>
      <p class="lede">Запуски, остановки, падения, лимиты памяти и смена статуса проверки здоровья.</p></div></header>
    <div class="filters">
      <label>Важность <select id="ev-level"><option value="">все</option><option value="warning">внимание</option><option value="error">ошибки</option><option value="info">инфо</option></select></label>
      <label>Агент <select id="ev-agent"><option value="">все</option>${agents.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("")}</select></label>
    </div>
    <table class="events"><thead><tr><th>Время</th><th>Важность</th><th>Агент</th><th>Событие</th></tr></thead><tbody id="ev-body"></tbody></table>`;
  const names = new Map(agents.map((a) => [a.id, a.name]));
  const paint = async () => {
    const params = new URLSearchParams({ limit: "500" });
    if ($("#ev-level").value) params.set("level", $("#ev-level").value);
    if ($("#ev-agent").value) params.set("agent_id", $("#ev-agent").value);
    const events = await api(`/api/events?${params}`);
    $("#ev-body").innerHTML = events.length ? events.map((e) => `<tr data-level="${esc(e.level)}">
      <td>${fmtDateTime(e.ts)}</td>
      <td><span class="level" data-level="${esc(e.level)}">${LEVELS[e.level] ?? esc(e.level)}</span></td>
      <td>${e.agent_id ? `<a href="#/agents/${encodeURIComponent(e.agent_id)}">${esc(names.get(e.agent_id) ?? e.agent_id)}</a>` : "портал"}</td>
      <td>${esc(e.message)}</td></tr>`).join("") : `<tr><td colspan="4" class="muted">Под фильтр ничего не подходит.</td></tr>`;
  };
  $("#ev-level").addEventListener("change", paint);
  $("#ev-agent").addEventListener("change", paint);
  await paint();
  cleanup.push(onLive((msg) => { if (msg.type === "event") paint(); }));
}

// ---------------------------------------------------------------------------
// boot

function initTheme() {
  const saved = (() => { try { return localStorage.getItem("theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch { /* private mode */ }
    route();
  });
}

initTheme();
window.addEventListener("hashchange", () => { route(); view.focus({ preventScroll: true }); });
api("/api/info").then(() => { connectLive(); route(); }).catch((err) => {
  view.innerHTML = `<div class="empty"><h1>Портал не отвечает</h1><p>${esc(err.message)}</p></div>`;
});
