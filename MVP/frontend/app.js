const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  view: "overview",
  profile: "ALL",
  queueMode: "active",
  queueFilters: { search: "", source: "", date: "", importance: "", kind: "", tag: "all" },
  sourceMode: "sources",
  materialMode: "attention",
  historyMode: "important",
  selected: new Set(),
  queue: [],
  sources: [],
  materials: [],
  npa: [],
  events: [],
  context: null,
  digests: [],
  digestOrder: [],
  status: null,
  sourcePreview: null,
  poll: null,
  scheduleClock: null,
  schedulePoll: null,
  workspacePoll: null,
  workspaceFingerprint: "",
  draggedId: null,
  digestMeta: {
    title: "Информационная повестка GS Labs",
    header: "Ключевые события за период",
    footer: "Подробности и первоисточники доступны по ссылкам.",
  },
  digestMetaUpdated: { header: null, footer: null },
  roleSelections: { PR: [], GR: [] },
  currentDigest: null,
};

const views = {
  overview: ["СЕГОДНЯ", "Рабочая повестка", "Что нужно проверить и подготовить сегодня."],
  queue: ["РАБОЧАЯ ПОВЕСТКА", "Разбор и выпуск", "PR и GR выбирают сигналы и вместе готовят общий дайджест."],
  npa: ["НПА", "Нормативные объекты", "Актуальная стадия, изменения и история каждого документа."],
  sources: ["ИСТОЧНИКИ И ПОТОК", "Контроль входа", "Подключения, ошибки и реальные входящие материалы."],
  materials: ["ИСТОЧНИКИ И ПОТОК", "Контроль входа", "Подключения, ошибки и реальные входящие материалы."],
  events: ["СЮЖЕТЫ", "Связанные сюжеты", "Публикации об одном событии собраны вместе."],
  context: ["КОНТЕКСТ", "Что важно GS Labs", "Общий контекст и отдельные ориентиры для PR и GR."],
  history: ["ИСТОРИЯ", "Что происходило", "Понятная хронология действий системы и людей."],
};

const kindLabels = {
  rss: "RSS", telegram: "Telegram", html: "Сайт", sitemap: "Карта сайта",
  api: "Открытый API", search: "Поиск", tavily: "Поиск", web_search: "Поиск", manual: "Ручная ссылка",
  news: "Новость", npa: "НПА", npa_candidate: "Возможный НПА",
};
const directionLabels = { both: "PR + GR", BOTH: "PR + GR", pr: "PR", gr: "GR", ALL: "Все", PR: "PR", GR: "GR", IRRELEVANT: "Вне фокуса" };
const importanceLabels = { low: "Низкая", medium: "Средняя", high: "Высокая", critical: "Критичная" };
const stageLabels = {
  draft: "Проект", public_discussion: "Общественное обсуждение", revised_draft: "Доработанный проект",
  introduced: "Внесён", adopted: "Принят", effective: "Действует", amended: "Изменён",
  repealed: "Утратил силу", unknown: "Требует уточнения", "Официально опубликован": "Официально опубликован",
};
const digestStatus = { draft: "Черновик", approved: "Подтверждён", sent: "Отправлен", delivered: "Отправлен", reset: "Сброшен", delivery_failed: "Ошибка отправки" };

const esc = (value) => String(value ?? "").replace(
  /[&<>'"]/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char],
);
const safeUrl = (value) => {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch { return ""; }
};
const fmt = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString("ru-RU", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
};
const day = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleDateString("ru-RU", {
    day: "2-digit", month: "short", year: "numeric",
  });
};

const dateInputValue = (value) => {
  const date = value instanceof Date ? value : new Date(value);
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
};
const short = (value, length = 180) => {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
};
const plural = (count, one, few, many) => {
  const mod10 = Math.abs(count) % 10; const mod100 = Math.abs(count) % 100;
  return mod10 === 1 && mod100 !== 11 ? one : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? few : many;
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options,
  });
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.error || `Ошибка ${response.status}`);
  return data;
}

function toast(message, bad = false) {
  const element = $("#toast");
  element.textContent = message;
  element.style.background = bad ? "#8e2d28" : "";
  element.classList.add("show");
  clearTimeout(element._timer);
  element._timer = setTimeout(() => {
    element.classList.remove("show"); element.style.background = "";
  }, 2800);
}

const empty = (title, text) => `<div class="empty"><strong>${esc(title)}</strong><span>${esc(text)}</span></div>`;
const profileQuery = (extra = "") => {
  const params = new URLSearchParams(extra);
  if (state.profile !== "ALL") params.set("profile", state.profile);
  return params.toString() ? `?${params}` : "";
};

function metaPills(signal) {
  const interest = signal.interest || "BOTH";
  const result = [
    `<span class="pill ${esc(interest)}">${esc(directionLabels[interest] || interest)}</span>`,
    `<span class="pill ${esc(signal.importance || "low")}">${esc(importanceLabels[signal.importance] || "Важность не определена")}</span>`,
  ];
  if (["npa", "npa_candidate"].includes(signal.kind)) result.push('<span class="pill GR">НПА</span>');
  if (signal.urgency === "urgent") result.push('<span class="pill critical">Срочно</span>');
  return `<div class="meta">${result.join("")}</div>`;
}

function reliability(signal, reviewRequired) {
  const score = Number(signal.confidence || 0);
  if (reviewRequired || score < 0.65) return ["check", "Нужно проверить"];
  if (score >= 0.82) return ["strong", "Высокая надёжность"];
  return ["normal", "Достаточно данных"];
}

function queueRow(item, selectable = false) {
  const signal = item.signal || {};
  const decision = item.latest_decision === "include" ? ["strong", "Подтверждён"]
    : item.latest_decision === "exclude" ? ["muted", "Исключён"]
      : item.review_required ? ["check", "Нужно проверить"] : ["normal", "Готов"];
  const input = selectable
    ? `<input type="checkbox" data-pick="${esc(signal.signal_id)}" ${state.selected.has(signal.signal_id) ? "checked" : ""} aria-label="Выбрать сигнал">`
    : "";
  return `<article class="signal-row" data-signal="${esc(signal.signal_id)}">
    <div class="signal-mark ${esc(signal.importance || "low")}"></div>${input}
    <div class="signal-main"><h3>${esc(signal.summary || "Без заголовка")}</h3><p>${esc(short(signal.impact || "Влияние пока не сформулировано", 155))}</p>
      <div class="signal-meta"><span>${esc(signal.source_title || kindLabels[signal.kind] || "Источник")}</span><i></i><span>${fmt(item.decision_at || item.created_at)}</span>${metaPills(signal)}</div>
    </div><div class="signal-state"><span class="trust ${decision[0]}">${decision[1]}</span><b aria-hidden="true">›</b></div>
  </article>`;
}

const priority = (item) => {
  const signal = item.signal || {};
  const weight = { critical: 4, high: 3, medium: 2, low: 1 }[signal.importance] || 0;
  return (signal.urgency === "urgent" ? 10 : 0) + weight;
};
const sortedQueue = (items) => [...items].sort((a, b) => priority(b) - priority(a) || String(b.created_at || "").localeCompare(String(a.created_at || "")));

function digestRow(item) {
  const signal = item.signal || {};
  return `<article class="digest-candidate" data-signal="${esc(signal.signal_id)}">
    <input type="checkbox" data-pick="${esc(signal.signal_id)}" ${state.selected.has(signal.signal_id) ? "checked" : ""} aria-label="Выбрать сигнал">
    <div><h3>${esc(signal.summary || "Без заголовка")}</h3><p>${esc(short(signal.impact || "Влияние пока не сформулировано", 135))}</p><div class="signal-meta">${metaPills(signal)}</div></div>
    <b aria-hidden="true">›</b>
  </article>`;
}

function metric(icon, label, value, caption, tone = "") {
  return `<article class="metric-card"><div class="metric-top"><span class="eyebrow">${esc(label)}</span><i class="metric-icon ${tone}">${icon}</i></div><strong>${esc(value)}</strong><p>${esc(caption)}</p></article>`;
}

function jobStep(step, current) {
  const order = ["collecting", "analysing", "maintenance"];
  const index = order.indexOf(step);
  const currentIndex = order.indexOf(current);
  const labels = {
    collecting: ["Получение материалов", "Проверяем источники и сохраняем оригиналы"],
    analysing: ["AI-анализ", "Выделяем сигналы, адресатов и доказательства"],
    maintenance: ["Обновление банка", "Объединяем события и обновляем НПА"],
  };
  const status = current === "completed" ? "done" : currentIndex > index ? "done" : currentIndex === index ? "current" : "waiting";
  return `<div class="process-step ${status}"><i>${status === "done" ? "✓" : index + 1}</i><div><strong>${labels[step][0]}</strong><small>${labels[step][1]}</small></div></div>`;
}

function jobUI(status) {
  const job = status?.job || {};
  const running = job.state === "running";
  const failed = job.state === "failed";
  // Ход обработки доступен по клику в боковой панели и не отнимает место на каждом экране.
  $("#job-banner").hidden = true;
  $("#run-cycle").classList.toggle("busy", running);
  $("#run-cycle").disabled = running;
  $("#system-dot").className = running ? "busy" : failed ? "warn" : "ok";
  $("#system-label").textContent = running ? "Идёт обновление" : failed ? "Запуск требует внимания" : "Система готова";
  $("#system-subline").textContent = running
    ? job.step === "collecting" ? "Получаем материалы" : job.step === "maintenance" ? "Обновляем банк" : "Анализируем новое"
    : failed ? "Откройте подробности" : status?.watching ? "Автосбор каждые 10 минут" : "Запуск по запросу";
  if (running) {
    $("#job-title").textContent = job.step === "collecting" ? "Получаем новые материалы" : job.step === "maintenance" ? "Обновляем события и НПА" : "Анализируем материалы";
    $("#job-detail").textContent = job.total ? `${job.progress} из ${job.total} обработано` : "Подготавливаем входной поток";
    $("#job-progress").style.width = job.total ? `${Math.max(5, (job.progress / job.total) * 100)}%` : "18%";
  }
}

async function refreshStatus() {
  try {
    state.status = await api("/api/pilot/status");
    jobUI(state.status);
    clearTimeout(state.poll);
    state.poll = state.status.job?.state === "running" ? setTimeout(refreshStatus, 1600) : null;
  } catch {
    $("#system-label").textContent = "Система недоступна";
    $("#system-subline").textContent = "Нет связи с сервером";
    $("#system-dot").className = "warn";
  }
}

function openDrawer() {
  $("#drawer").classList.add("open"); $("#drawer-backdrop").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
}
function stopScheduleTimers() {
  clearInterval(state.scheduleClock); clearInterval(state.schedulePoll);
  state.scheduleClock = null; state.schedulePoll = null;
}
function closeDrawer() {
  stopScheduleTimers();
  $("#drawer").classList.remove("open"); $("#drawer-backdrop").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
}

function countdownLabel(value, fallback = "По запросу") {
  if (!value) return fallback;
  const seconds = Math.max(0, Math.ceil((new Date(value).getTime() - Date.now()) / 1000));
  if (seconds <= 2) return "Запускается";
  if (seconds < 60) return `через ${seconds} сек`;
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `через ${minutes} мин`;
  const hours = Math.floor(minutes / 60); const rest = minutes % 60;
  return `через ${hours} ч${rest ? ` ${rest} мин` : ""}`;
}

function scheduleIntervalLabel(values = []) {
  const minutes = [...new Set(values.map((value) => Math.round(value / 60)))].sort((a, b) => a - b);
  if (!minutes.length) return "интервал не задан";
  return minutes.length === 1 ? `каждые ${minutes[0]} мин` : `каждые ${minutes[0]}–${minutes.at(-1)} мин`;
}

function updateScheduleCountdowns() {
  $$('[data-next-run]').forEach((element) => {
    element.textContent = countdownLabel(element.dataset.nextRun, element.dataset.fallback);
  });
}

function renderJobPanel() {
  const status = state.status || {};
  const job = status.job || {};
  const data = status.data || {};
  const schedule = status.collection_schedule || [];
  const currentLabels = { collecting: "Собираем новые материалы", analysing: "Анализируем новое", maintenance: "Обновляем сюжеты и НПА" };
  const rows = schedule.map((item) => {
    const running = item.state === "running";
    const fallback = running ? "Идёт сбор" : item.state === "manual" ? "Только вручную" : "Ожидает запуска";
    const count = `${item.source_count} ${plural(item.source_count, "источник", "источника", "источников")}`;
    return `<div class="schedule-row ${running ? "running" : ""}"><i></i><div><strong>${esc(kindLabels[item.kind] || item.kind)}</strong><small>${count} · ${scheduleIntervalLabel(item.intervals_seconds)}</small></div><div class="schedule-time"><b data-next-run="${esc(running ? "" : item.next_run_at || "")}" data-fallback="${esc(fallback)}">${running ? "Идёт сбор" : countdownLabel(item.next_run_at, fallback)}</b><small>${item.last_success_at ? `последний успешный ${fmt(item.last_success_at)}` : "ещё не запускался"}</small></div></div>`;
  }).join("");
  $("#drawer-content").innerHTML = `<div class="eyebrow">АВТОМАТИЧЕСКИЙ СБОР</div><h2>Когда система проверит источники</h2>
    <p class="lead">Каждый способ запускается по реальному расписанию. Можно не ждать и запустить всё сейчас.</p>
    ${job.state === "running" ? `<div class="current-job"><span class="live-dot"></span><div><strong>${esc(currentLabels[job.step] || "Идёт обновление")}</strong><small>${job.total ? `${job.progress} из ${job.total} обработано` : "Текущий цикл ещё не завершён"}</small></div></div>` : ""}
    <div class="schedule-list" id="collection-schedule">${rows || `<div class="notice">Активных автоматических источников пока нет.</div>`}</div>
    <div class="schedule-summary"><span>После сбора</span><strong>${data.awaiting_ai || 0} материалов ожидают AI-анализа</strong></div>
    ${job.error ? `<div class="notice danger"><strong>Что помешало запуску</strong><p>${esc(job.error)}</p></div>` : ""}
    <div class="drawer-actions"><button class="button soft" data-go="sources">Настроить источники</button><button class="button primary" data-job="cycle" ${job.state === "running" ? "disabled" : ""}>Запустить сейчас</button></div>`;
  updateScheduleCountdowns();
}

function openJob() {
  stopScheduleTimers();
  renderJobPanel();
  openDrawer();
  state.scheduleClock = setInterval(updateScheduleCountdowns, 1000);
  state.schedulePoll = setInterval(async () => {
    if (!$("#collection-schedule")) return;
    try { state.status = await api("/api/pilot/status"); jobUI(state.status); renderJobPanel(); } catch { /* global status handles connectivity */ }
  }, 10000);
}

async function renderOverview() {
  const [overview, queue, resolved, sources, npa, digests] = await Promise.all([
    api("/api/overview"), api("/api/queue"), api("/api/queue?resolved=1"), api("/api/sources"), api("/api/npa"), api("/api/digests"),
  ]);
  state.overview = overview; state.queue = queue.items;
  $("#queue-badge").textContent = overview.pending_queue || 0;
  const sourceErrors = sources.items.filter((item) => ["attention", "coverage_gap"].includes(item.state));
  const included = resolved.items.filter((item) => item.latest_decision === "include");
  const pending = queue.items.length;
  const prCount = queue.items.filter((item) => ["PR", "BOTH"].includes(item.signal?.interest)).length;
  const grCount = queue.items.filter((item) => ["GR", "BOTH"].includes(item.signal?.interest)).length;
  const npaAttention = npa.items.filter((item) => item.stage === "unknown" || !item.official_url).length;
  const selectedPr = included.filter((item) => digestItemOwner(item) === "PR").length;
  const selectedGr = included.filter((item) => digestItemOwner(item) === "GR").length;
  const activeIds = new Set(queue.items.map((item) => item.signal?.signal_id));
  const fullFlow = sortedQueue(resolved.items);
  const flowRows = fullFlow.map((item) => todayFlowRow(item, activeIds)).join("");
  const newestDigest = digests.items[0];
  const latestDigest = ["draft", "delivered"].includes(newestDigest?.status) && newestDigest?.payload?.role_content
    ? newestDigest
    : null;
  const readiness = latestDigest?.payload?.readiness || {};
  const schedule = state.status?.collection_schedule || [];
  const nextRun = schedule.map((item) => item.next_run_at).filter(Boolean).sort()[0];
  const systemRunning = state.status?.job?.state === "running";
  const focusTitle = systemRunning ? "Система обновляет повестку" : pending ? "Материалы готовы к разбору" : "Рабочая повестка разобрана";
  const focusText = systemRunning ? "Новые материалы собираются и проходят AI-анализ." : `Автосбор работает в фоне${nextRun ? ` · следующий запуск ${countdownLabel(nextRun)}` : ""}.`;
  $("#view").innerHTML = `<article class="focus-card"><div><div class="eyebrow">ТЕКУЩИЙ ЦИКЛ</div><h2>${focusTitle}</h2><p>${focusText}</p></div><div class="hero-actions"><button class="button acid" data-go="queue">Открыть рабочую зону</button><button class="button ghost-dark" data-open-job>Статус системы</button></div></article>
  <div class="today-grid">
    <section class="today-panel team-panel"><div class="today-panel-head"><div><span>РАБОТА КОМАНДЫ</span><h2>Разбор по направлениям</h2></div><small>Полный список — в рабочей зоне</small></div>
      <button class="role-progress" data-dashboard-profile="PR"><i class="pr"></i><div><strong>PR</strong><span>${prCount ? `${prCount} ждут решения` : "очередь разобрана"}</span></div><b>${selectedPr} выбрано</b><em>→</em></button>
      <button class="role-progress" data-dashboard-profile="GR"><i class="gr"></i><div><strong>GR</strong><span>${grCount ? `${grCount} ждут решения` : "очередь разобрана"}</span></div><b>${selectedGr} выбрано</b><em>→</em></button>
    </section>
    <section class="today-panel release-panel"><div class="today-panel-head"><div><span>ОБЩИЙ ВЫПУСК</span><h2>${latestDigest ? (latestDigest.status === "delivered" ? "Отправлен" : "Готовится командой") : "Ещё не собран"}</h2></div></div>
      <div class="release-readiness"><div class="${readiness.PR ? "done" : ""}"><i>${readiness.PR ? "✓" : ""}</i><span>PR</span><b>${readiness.PR ? "готов" : "работает"}</b></div><div class="${readiness.GR ? "done" : ""}"><i>${readiness.GR ? "✓" : ""}</i><span>GR</span><b>${readiness.GR ? "готов" : "работает"}</b></div></div>
      <p>${latestDigest?.status === "delivered" ? `Последняя версия отправлена ${fmt(latestDigest.created_at)}.` : "Выпуск отправится автоматически, когда оба блока будут готовы."}</p><button class="button soft" data-go="queue">Открыть выпуск</button>
    </section>
    <section class="today-panel system-panel"><div class="today-panel-head"><div><span>СИСТЕМА</span><h2>${systemRunning ? "Идёт обновление" : "Работает штатно"}</h2></div><span class="live-sync"><i></i>${systemRunning ? "В процессе" : "Онлайн"}</span></div>
      <div class="system-lines"><div><span>Источники</span><strong>${overview.sources_total || 0} подключено</strong></div><div><span>Новых в последнем сборе</span><strong>${state.status?.latest_collection?.docs_new || 0}</strong></div><div><span>Следующая проверка</span><strong>${nextRun ? countdownLabel(nextRun) : "по команде"}</strong></div></div>
      ${sourceErrors.length ? `<button class="inline-warning" data-go="sources">Есть источники, требующие внимания →</button>` : ""}
      ${npaAttention ? `<button class="inline-warning neutral" data-go="npa">В реестре НПА есть записи для уточнения →</button>` : ""}
    </section>
  </div>
  <details class="today-flow-panel">
    <summary><div><span>ВХОДЯЩИЙ ПОТОК</span><strong>Все материалы</strong></div><small>${fullFlow.length} материалов</small><b><i></i>Показать</b></summary>
    <div class="today-flow-list">${flowRows || empty("Поток пуст", "Материалы появятся после следующего сбора.")}</div>
    <div class="today-flow-footer"><span>Здесь только обзор. Решения принимаются в рабочей зоне.</span><button class="button soft" data-go="queue">Перейти к разбору</button></div>
  </details>`;
}

function todayFlowRow(item, activeIds) {
  const signal = item.signal || {};
  let stateLabel = "Отсеяно AI";
  let stateClass = "muted";
  if (item.latest_decision === "include") { stateLabel = "В дайджесте"; stateClass = "included"; }
  else if (item.latest_decision === "exclude") { stateLabel = "Отсеяно"; stateClass = "excluded"; }
  else if (activeIds.has(signal.signal_id)) { stateLabel = item.review_required ? "Нужно проверить" : "Новый"; stateClass = item.review_required ? "check" : "new"; }
  return `<button class="today-flow-row" data-signal="${esc(signal.signal_id)}"><span class="signal-mark ${esc(signal.importance || "low")}"></span><div><strong>${esc(short(signal.summary || "Без заголовка", 125))}</strong><small>${esc(signal.source_title || kindLabels[signal.kind] || "Источник")} · ${fmt(item.created_at)}</small></div><span class="flow-state ${stateClass}">${stateLabel}</span><em>›</em></button>`;
}

function workSignalRow(item, mode = "active") {
  const signal = item.signal || {};
  const canAct = ["PR", "GR"].includes(state.profile);
  const returnButton = !canAct
    ? `<span class="role-hint">Выберите PR или GR</span>`
    : mode === "excluded"
    ? `<button class="mini-action" data-review="restore" data-id="${esc(signal.signal_id)}" data-rev="${item.revision}">Вернуть</button>`
    : `<button class="mini-action primary" data-review="include" data-id="${esc(signal.signal_id)}" data-rev="${item.revision}">В дайджест →</button><button class="mini-action quiet" data-review="exclude" data-id="${esc(signal.signal_id)}" data-rev="${item.revision}">Отсеять</button>`;
  const sourceName = item.source_name || signal.source_title || kindLabels[item.source_kind] || "Источник";
  const searchText = `${signal.summary || ""} ${signal.impact || ""} ${sourceName} ${signal.npa_identifier || ""}`.toLowerCase();
  return `<article class="work-signal" data-search-text="${esc(searchText)}"><button class="work-signal-main" data-signal="${esc(signal.signal_id)}"><div class="signal-mark ${esc(signal.importance || "low")}"></div><div><h3>${esc(signal.summary || "Без заголовка")}</h3><p>${esc(short(signal.impact || "Влияние пока не сформулировано", 105))}</p><div class="signal-meta"><span>${esc(sourceName)}</span><i></i><span>${fmt(item.published_at || item.created_at)}</span>${metaPills(signal)}</div></div></button><div class="work-signal-actions">${returnButton}</div></article>`;
}

function queueTagMatch(item, tag) {
  const signal = item.signal || {};
  if (tag === "all") return true;
  if (tag === "PR" || tag === "GR") return [tag, "BOTH"].includes(signal.interest);
  if (tag === "npa") return ["npa", "npa_candidate"].includes(signal.kind);
  if (tag === "urgent") return signal.urgency === "urgent" || signal.importance === "critical";
  return true;
}

function queueDateMatch(item, range) {
  if (!range) return true;
  const date = new Date(item.published_at || item.created_at || item.decision_at || 0);
  if (Number.isNaN(date.getTime())) return false;
  const now = new Date();
  if (range === "today") return date.toDateString() === now.toDateString();
  const days = range === "week" ? 7 : 30;
  return now.getTime() - date.getTime() <= days * 86400000;
}

function selectedDigestItem(item) {
  const signal = item.signal || {};
  const owner = digestItemOwner(item);
  const editable = owner === state.profile;
  return `<article class="selected-item ${editable ? "editable" : "readonly"}" draggable="${editable}" data-selected="${esc(signal.signal_id)}"><span class="drag-handle" title="${editable ? "Изменить порядок" : `Блок ведёт ${owner}`}">${editable ? "⋮⋮" : "•"}</span><div><strong>${esc(short(signal.summary || "Без заголовка", 95))}</strong><small>${owner} · ${esc(importanceLabels[signal.importance] || "")}${editable ? "" : " · только просмотр"}</small></div>${editable ? `<button class="remove-selected" data-review="restore" data-id="${esc(signal.signal_id)}" data-rev="${item.revision}" title="Вернуть в список">×</button>` : ""}</article>`;
}

function digestItemOwner(item) {
  const actor = String(item.decision_actor || "").toLowerCase();
  if (actor.startsWith("demo-gr") || actor.startsWith("gr-")) return "GR";
  if (actor.startsWith("demo-pr") || actor.startsWith("pr-")) return "PR";
  return item.signal?.interest === "GR" ? "GR" : "PR";
}

function metaChangedAt(key) {
  const changed = state.digestMetaUpdated[key];
  return changed ? `Изменено ${new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(changed)}` : "Задано шаблоном";
}

function digestMetaRow(key, label) {
  return `<div class="digest-meta-row"><div><small>${label}</small><strong>${esc(state.digestMeta[key])}</strong><span>${metaChangedAt(key)}</span></div><button class="meta-edit" data-edit-digest-meta="${key}" aria-label="Изменить ${label.toLowerCase()}" title="Изменить">✎</button></div>`;
}

function editDigestMeta(key) {
  const labels = { header: "Вступление", footer: "Завершение" };
  if (!labels[key]) return;
  $("#modal-content").innerHTML = `<div class="eyebrow">ОФОРМЛЕНИЕ ВЫПУСКА</div><h2 id="modal-title">${labels[key]}</h2><label>Текст<input id="digest-meta-value" value="${esc(state.digestMeta[key])}"></label><button class="button primary" id="save-digest-meta" data-meta-key="${key}">Сохранить</button>`;
  $("#modal-backdrop").hidden = false;
}

function renderDigestText(text) {
  return String(text || "").split(/\n+/).filter(Boolean).map((line) => {
    const link = line.match(/^\[([^\]]+)]\((https?:\/\/[^)]+)\)$/);
    return link ? `<a href="${esc(link[2])}" target="_blank" rel="noopener">${esc(link[1])} ↗</a>` : `<p>${esc(line)}</p>`;
  }).join("");
}

function editRoleContent(role) {
  const content = state.currentDigest?.payload?.role_content?.[role];
  if (!content?.text || state.profile !== role) return;
  $("#modal-content").innerHTML = `<div class="eyebrow">${role}-БЛОК</div><h2 id="modal-title">Исправить текст дайджеста</h2><label>Готовый текст<textarea id="role-content-value">${esc(content.text)}</textarea></label><button class="button primary" id="save-role-content" data-role="${role}">Сохранить исправления</button>`;
  $("#modal-backdrop").hidden = false;
}

function digestDraftPanel(digest) {
  const payload = digest.payload || {};
  const readiness = payload.readiness || {};
  const sections = [
    ["pr", "PR", (payload.items || []).filter((item) => item.section === "pr")],
    ["gr", "GR и НПА", (payload.items || []).filter((item) => item.section === "gr")],
  ];
  const readyRole = ["PR", "GR"].includes(state.profile) ? state.profile : null;
  if (digest.status === "delivered") return `<div class="sent-state"><i>✓</i><h3>Дайджест отправлен</h3><p>PR и GR подтвердили общую версию.</p><button class="button soft" data-open-digest="${esc(digest.id)}" data-version="${digest.version}">Открыть выпуск</button></div>`;
  if (digest.status === "delivery_failed") return `<div class="sent-state delivery-error"><i>!</i><h3>Не удалось отправить</h3><p>Выпуск сохранён. Проверьте Telegram-канал и повторите попытку — дубль не появится.</p><button class="button primary" data-retry-delivery data-digest-id="${esc(digest.id)}" data-version="${digest.version}">Повторить отправку</button></div>`;
  const sectionHtml = sections.map(([key, label, items]) => {
    const owned = key.toUpperCase() === state.profile;
    const access = state.profile === "ALL" ? "Выберите роль" : owned ? "Ваш блок" : "Только просмотр";
    return `<section class="digest-block ${owned ? "role-owned" : "role-locked"}"><h4><span>${label}</span><small>${owned ? "✎" : "🔒"} ${access}</small></h4>${items.map((item) => `<div><strong>${esc(item.signal?.summary || "")}</strong><p>${esc(item.signal?.impact || "")}</p><small>${(item.signal?.claims || []).length} подтверждённых фактов · есть первоисточник</small></div>`).join("") || `<p class="muted">Блок пока пуст.</p>`}</section>`;
  }).join("");
  return `<div class="digest-paper"><div class="digest-paper-head"><span>ДАЙДЖЕСТ · ${day(payload.period?.to)}</span><h3>${esc(payload.title || "Информационная повестка GS Labs")}</h3>${payload.header ? `<p>${esc(payload.header)}</p>` : ""}</div>${sectionHtml}${payload.footer ? `<p class="digest-footer">${esc(payload.footer)}</p>` : ""}</div><div class="ready-row"><div><span class="ready-chip ${readiness.PR ? "done" : ""}">PR ${readiness.PR ? "готов" : "проверяет"}</span><span class="ready-chip ${readiness.GR ? "done" : ""}">GR ${readiness.GR ? "готов" : "проверяет"}</span></div>${readyRole ? `<button class="button primary" data-ready-role="${readyRole}" data-digest-id="${esc(digest.id)}" data-version="${digest.version}" ${readiness[readyRole] ? "disabled" : ""}>${readiness[readyRole] ? `Ваш блок ${readyRole} готов` : `Блок ${readyRole} готов`}</button>` : `<small>Выберите фокус PR или GR, чтобы подтвердить свой блок.</small>`}</div><p class="auto-send-note">Когда оба блока готовы, выпуск отправится в настроенный Telegram-канал.</p>`;
}

async function renderQueue(enablePolling = true, syncOnly = false) {
  const currentSearch = $("#queue-search")?.value;
  if (currentSearch !== undefined && currentSearch !== null) state.queueFilters.search = currentSearch;
  const filters = state.queueFilters;
  const [active, resolved, digests] = await Promise.all([
    api(`/api/queue${profileQuery()}`), api("/api/queue?resolved=1"), api("/api/digests"),
  ]);
  const fingerprint = JSON.stringify({
    profile: state.profile,
    mode: state.queueMode,
    active: active.items.map((item) => [item.signal?.signal_id, item.revision, item.latest_decision]),
    resolved: resolved.items.map((item) => [item.signal?.signal_id, item.revision, item.latest_decision]),
    digests: digests.items.map((item) => [item.id, item.version, item.status, item.payload?.readiness, item.payload?.role_content]),
  });
  if (syncOnly && fingerprint === state.workspaceFingerprint) return;
  state.workspaceFingerprint = fingerprint;
  const digestScrollTop = $(".digest-workspace")?.scrollTop || 0;
  const pageScrollTop = window.scrollY;
  state.queue = active.items; state.digests = digests.items;
  $("#queue-badge").textContent = active.items.length;
  const selected = resolved.items.filter((item) => item.latest_decision === "include");
  const selectedById = new Map(selected.map((item) => [item.signal?.signal_id, item]));
  state.digestOrder = [...state.digestOrder.filter((id) => selectedById.has(id)), ...selected.map((item) => item.signal?.signal_id).filter((id) => !state.digestOrder.includes(id))];
  let left = active.items;
  if (state.queueMode === "check") left = left.filter((item) => item.review_required);
  if (state.queueMode === "excluded") left = resolved.items.filter((item) => item.latest_decision === "exclude" && (state.profile === "ALL" || [state.profile, "BOTH"].includes(item.signal?.interest)));
  const query = filters.search.trim().toLowerCase();
  if (query) left = left.filter((item) => `${item.signal?.summary || ""} ${item.signal?.impact || ""} ${item.source_name || ""} ${item.signal?.source_title || ""} ${item.signal?.npa_identifier || ""}`.toLowerCase().includes(query));
  if (filters.source) left = left.filter((item) => (item.source_name || "") === filters.source);
  if (filters.importance) left = left.filter((item) => item.signal?.importance === filters.importance);
  if (filters.kind) left = left.filter((item) => filters.kind === "npa" ? ["npa", "npa_candidate"].includes(item.signal?.kind) : item.signal?.kind === "news");
  if (filters.date) left = left.filter((item) => queueDateMatch(item, filters.date));
  left = left.filter((item) => queueTagMatch(item, filters.tag));
  const availableSources = [...new Set([...active.items, ...resolved.items].map((item) => item.source_name).filter(Boolean))].sort((a, b) => a.localeCompare(b, "ru"));
  const sourceOptions = availableSources.map((source) => `<option value="${esc(source)}" ${filters.source === source ? "selected" : ""}>${esc(short(source, 34))}</option>`).join("");
  const tagButtons = [["all", "Все"], ["PR", "PR"], ["GR", "GR"], ["npa", "НПА"], ["urgent", "Критичное"]].map(([value, label]) => `<button class="quick-tag ${filters.tag === value ? "active" : ""}" data-queue-tag="${value}">${label}</button>`).join("");
  const newestDigest = digests.items[0];
  const latestDraft = newestDigest?.status === "draft" && newestDigest?.payload?.role_content ? newestDigest : null;
  const latestDelivered = newestDigest?.status === "delivered" ? newestDigest : null;
  const latestFailed = newestDigest?.status === "delivery_failed" ? newestDigest : null;
  const latest = latestDraft || latestDelivered || latestFailed;
  state.currentDigest = latestDraft || null;
  const selectedOrdered = state.digestOrder.map((id) => selectedById.get(id)).filter(Boolean);
  const today = new Date(); const weekAgo = new Date(today); weekAgo.setDate(today.getDate() - 6);
  const roleGroups = [
    ["PR", "PR", selectedOrdered.filter((item) => digestItemOwner(item) === "PR")],
    ["GR", "GR и НПА", selectedOrdered.filter((item) => digestItemOwner(item) === "GR")],
  ];
  state.roleSelections = Object.fromEntries(roleGroups.map(([owner, , items]) => [owner, items.map((item) => item.signal?.signal_id)]));
  const groupsHtml = roleGroups.map(([owner, label, items]) => {
    const owned = owner === state.profile;
    const access = state.profile === "ALL" ? "Выберите роль" : owned ? "Можно редактировать" : "Только просмотр";
    const content = latestDraft?.payload?.role_content?.[owner] || {};
    const currentIds = items.map((item) => item.signal?.signal_id).sort();
    const generatedIds = [...(content.signal_ids || [])].sort();
    const fresh = content.status === "generated" && JSON.stringify(currentIds) === JSON.stringify(generatedIds);
    const ready = Boolean(latestDraft?.payload?.readiness?.[owner]);
    const generatedHtml = content.text ? `<div class="generated-copy ${fresh ? "" : "stale"}"><div><strong>Текст блока</strong><span>${fresh ? `AI · ${fmt(content.updated_at)}` : "Состав изменился — обновите текст"}</span></div>${renderDigestText(content.text)}</div>` : `<div class="generation-placeholder"><strong>Текст ещё не собран</strong><span>Сначала выберите материалы, затем запустите AI.</span></div>`;
    const controls = owned ? `<div class="role-actions"><button class="button soft" data-generate-role="${owner}" ${items.length && !ready ? "" : "disabled"}>${content.text ? "Обновить текст через AI" : "Собрать текст через AI"}</button>${content.text && fresh ? `<button class="button soft icon-button" data-edit-role-content="${owner}" title="Исправить текст">✎</button><button class="button primary" data-ready-role="${owner}" data-digest-id="${esc(latestDraft.id)}" data-version="${latestDraft.version}" ${ready ? "disabled" : ""}>${ready ? "Блок готов" : "Готов к отправке"}</button>` : ""}</div>` : "";
    return `<section class="role-section ${owned ? "role-owned" : "role-locked"}"><h3><span>${label}<b>${items.length}</b></span><small>${owned ? "✎" : "🔒"} ${access}</small></h3><div class="selected-dropzone">${items.map(selectedDigestItem).join("") || `<p>Добавьте сигналы слева.</p>`}</div>${generatedHtml}${controls}</section>`;
  }).join("");
  $("#view").innerHTML = `<div class="workbench"><section class="signal-pool"><div class="workbench-head"><div><h2>Сигналы</h2><p>AI уже отсеял шум. Выберите то, что войдёт в общий выпуск.</p></div><span>${left.length} из ${active.items.length}</span></div><div class="queue-toolbar"><label class="search-field work-search"><span>⌕</span><input id="queue-search" placeholder="Найти сигнал, источник или НПА" value="${esc(filters.search)}"></label><select id="queue-source"><option value="">Все источники</option>${sourceOptions}</select><select id="queue-date"><option value="" ${!filters.date ? "selected" : ""}>За всё время</option><option value="today" ${filters.date === "today" ? "selected" : ""}>Сегодня</option><option value="week" ${filters.date === "week" ? "selected" : ""}>7 дней</option><option value="month" ${filters.date === "month" ? "selected" : ""}>30 дней</option></select><select id="queue-importance"><option value="" ${!filters.importance ? "selected" : ""}>Любая важность</option>${Object.entries(importanceLabels).map(([value, label]) => `<option value="${value}" ${filters.importance === value ? "selected" : ""}>${label}</option>`).join("")}</select><select id="queue-kind"><option value="" ${!filters.kind ? "selected" : ""}>Все категории</option><option value="news" ${filters.kind === "news" ? "selected" : ""}>Новости</option><option value="npa" ${filters.kind === "npa" ? "selected" : ""}>НПА</option></select></div><div class="queue-subbar"><div class="tabs queue-tabs"><button class="tab ${state.queueMode === "active" ? "active" : ""}" data-queue="active">Новые</button><button class="tab ${state.queueMode === "check" ? "active" : ""}" data-queue="check">Нужно проверить</button><button class="tab ${state.queueMode === "excluded" ? "active" : ""}" data-queue="excluded">Отсеяно</button></div><div class="quick-tags"><span>Теги</span>${tagButtons}</div></div><div class="work-signal-list">${left.length ? sortedQueue(left).map((item) => workSignalRow(item, state.queueMode)).join("") : empty("Ничего не найдено", "Измените поиск или снимите часть фильтров.")}</div></section><aside class="digest-workspace"><div class="workbench-head"><div><h2>Общий дайджест</h2><p>PR и GR независимо готовят свои блоки.</p></div><div class="workbench-tools"><button class="digest-expand" data-expand-digest title="Открыть крупно">↗</button><span class="live-sync"><i></i>Синхронизировано</span><button class="demo-reset" data-reset-demo>Сбросить демо</button></div></div>${latest?.status === "delivered" ? digestDraftPanel(latest) : `<div class="digest-settings"><div class="date-fields"><label>Начало периода<input id="period-from" type="date" value="${dateInputValue(weekAgo)}"></label><label>Конец периода<input id="period-to" type="date" value="${dateInputValue(today)}"></label></div><div class="digest-title-fixed"><small>Заголовок</small><strong>${esc(state.digestMeta.title)}</strong></div>${digestMetaRow("header", "Вступление")}</div><div class="selected-groups">${groupsHtml}</div>${digestMetaRow("footer", "Завершение")}`}</aside></div>`;
  if (latestFailed) {
    const workspace = $(".digest-workspace");
    const heading = workspace?.querySelector(".workbench-head")?.outerHTML || "";
    if (workspace) workspace.innerHTML = heading + digestDraftPanel(latestFailed);
  }
  const signalHead = $(".signal-pool .workbench-head");
  if (signalHead) {
    const addMaterial = document.createElement("button");
    addMaterial.id = "add-material";
    addMaterial.className = "button soft compact-action";
    addMaterial.textContent = "+ Добавить";
    signalHead.append(addMaterial);
  }
  $(".digest-workspace").scrollTop = digestScrollTop;
  window.scrollTo({ top: pageScrollTop });
  if (enablePolling && !state.workspacePoll) state.workspacePoll = setInterval(() => { if (state.view === "queue" && !state.draggedId && !$("#drawer").classList.contains("open") && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) renderQueue(false, true); }, 2200);
}

async function queueMode(mode) {
  state.queueMode = mode;
  await renderQueue(false);
}

async function openSignal(id) {
  const data = await api(`/api/signals/${encodeURIComponent(id)}`);
  const signal = data.signal || {}; const source = data.source || {}; const url = safeUrl(source.url);
  const claims = (signal.claims || []).map((claim) => `<div class="fact"><strong>${esc(claim.text)}</strong><div class="quote">«${esc(claim.evidence_quote)}»</div></div>`).join("");
  const canReview = ["PR", "GR"].includes(state.profile);
  const reviewActions = canReview
    ? `<div class="drawer-actions"><button class="button primary" data-review="include" data-id="${esc(signal.signal_id)}" data-rev="${data.revision}">Добавить в выпуск</button><button class="button soft" data-edit-signal="${esc(signal.signal_id)}" data-rev="${data.revision}">Исправить</button><button class="button danger" data-review="exclude" data-id="${esc(signal.signal_id)}" data-rev="${data.revision}">Исключить</button></div>`
    : '<div class="notice">Выберите роль PR или GR, чтобы разбирать сигналы и собирать свою часть выпуска.</div>';
  $("#drawer-content").innerHTML = `<div class="eyebrow">${esc(kindLabels[signal.kind] || "Сигнал")} · версия ${esc(data.revision)}</div><h2>${esc(signal.summary)}</h2>${metaPills(signal)}
    <h3>Что это меняет для GS Labs</h3><p class="lead">${esc(signal.impact || "Влияние пока не сформулировано")}</p><h3>Подтверждённые факты</h3>${claims || '<p class="muted">Подтверждающих фрагментов нет — такой сигнал нельзя автоматически включать в выпуск.</p>'}
    ${(signal.unknowns || []).length ? `<h3>Что пока неизвестно</h3><ul>${signal.unknowns.map((item) => `<li>${esc(typeof item === "string" ? item : item.text)}</li>`).join("")}</ul>` : ""}
    <h3>Оригинальный материал</h3><p><strong>${esc(source.name || source.title || "Источник не указан")}</strong><br><span class="muted">Опубликовано ${fmt(source.published_at)} · получено ${fmt(source.fetched_at)}</span></p>${url ? `<p><a href="${url}" target="_blank" rel="noreferrer">Открыть первоисточник ↗</a></p>` : '<p class="muted">Ссылка на оригинал недоступна.</p>'}<div class="source-box">${esc(source.text || "Текст оригинала недоступен")}</div>
    <details class="technical-details"><summary>Как получен этот вывод</summary><p>AI обработал сохранённый оригинал с контекстом GS Labs. Версия контекста: ${esc(data.analysis?.context_version || "не указана")}. Модель: ${esc(data.analysis?.model || "не указана")}.</p></details>
    ${reviewActions}`;
  state.openSignal = data; openDrawer();
}

function editSignal() {
  const data = state.openSignal; const signal = data.signal;
  const importanceOptions = Object.entries(importanceLabels).map(([value, label]) => `<option value="${value}" ${signal.importance === value ? "selected" : ""}>${label}</option>`).join("");
  const interestOptions = [["PR", "PR"], ["GR", "GR"], ["BOTH", "PR + GR"]].map(([value, label]) => `<option value="${value}" ${signal.interest === value ? "selected" : ""}>${label}</option>`).join("");
  const kindOptions = [["news", "Новость"], ["npa_candidate", "Возможный НПА"], ["npa", "Подтверждённый НПА"]].map(([value, label]) => `<option value="${value}" ${signal.kind === value ? "selected" : ""}>${label}</option>`).join("");
  $("#modal-content").innerHTML = `<div class="eyebrow">РУЧНАЯ ПРОВЕРКА</div><h2 id="modal-title">Уточнить вывод AI</h2><div class="edit-grid"><label class="wide">Короткий вывод<textarea id="edit-summary">${esc(signal.summary)}</textarea></label><label class="wide">Влияние на GS Labs<textarea id="edit-impact">${esc(signal.impact)}</textarea></label><label>Важность<select id="edit-importance">${importanceOptions}</select></label><label>Для кого<select id="edit-interest">${interestOptions}</select></label><label>Категория<select id="edit-kind">${kindOptions}</select></label><label>Срочность<select id="edit-urgency"><option value="routine" ${signal.urgency !== "urgent" ? "selected" : ""}>Плановая</option><option value="urgent" ${signal.urgency === "urgent" ? "selected" : ""}>Срочная</option></select></label></div><button class="button primary" id="save-signal" data-id="${esc(signal.signal_id)}" data-rev="${data.revision}">Сохранить изменённую версию</button>`;
  $("#modal-backdrop").hidden = false;
}

async function renderNpa() {
  const data = await api("/api/npa");
  state.npa = data.items.map((item, index) => ({ ...item, _index: index })).sort((a, b) => {
    const order = { pending: 5, selected: 4, approved: 4, delivered: 3, excluded: 2, filtered: 1 };
    return (order[b.workflow_status] || 0) - (order[a.workflow_status] || 0) || String(b.version_created_at || "").localeCompare(String(a.version_created_at || ""));
  });
  const attention = state.npa.filter((item) => item.relevance === "unknown" && item.workflow_status === "pending").length;
  $("#view").innerHTML = data.items.length ? `<div class="toolbar npa-toolbar"><div class="filter-bar compact-filters"><label class="search-field"><span aria-hidden="true">⌕</span><input id="npa-search" placeholder="Найти документ"></label><select id="npa-workflow"><option value="relevant">Для GS Labs</option><option value="pending">Требуют оценки</option><option value="delivered">Вошли в выпуск</option><option value="all">Все обнаруженные</option></select><select id="npa-stage"><option value="">Все стадии</option><option value="attention">Требуют уточнения</option><option value="adopted">Приняты</option><option value="effective">Действуют</option><option value="draft">Проекты</option></select></div><span class="status-pill ${attention ? "warn" : "ok"}" id="npa-total">${attention ? `${attention} ${plural(attention, "кандидат ждёт", "кандидата ждут", "кандидатов ждут")} оценки` : "Всё актуально"}</span></div><div class="npa-explanation"><strong>НПА не исчезает после выпуска.</strong><span>Обработанный сигнал уходит из очереди, а сам документ остаётся под наблюдением до новой версии или стадии.</span></div><div class="table-shell"><table class="table compact-table npa-table"><thead><tr><th>Нормативный объект</th><th>Юридическая стадия</th><th>Рабочий статус</th><th>Последний сигнал</th><th></th></tr></thead><tbody id="npa-rows"></tbody></table></div>` : empty("НПА ещё не выделены", "Подтверждённые нормативные объекты появятся после GR-анализа.");
  if (data.items.length) filterNpa();
}

function filterNpa() {
  const query = ($("#npa-search")?.value || "").trim().toLowerCase();
  const stage = $("#npa-stage")?.value || "";
  const workflow = $("#npa-workflow")?.value || "relevant";
  const rows = state.npa.filter((item) => {
    const text = `${item.official_identifier} ${item.version_payload?.change_summary || ""} ${item.version_payload?.summary || ""}`.toLowerCase();
    const matchesStage = !stage || (stage === "attention" ? item.stage === "unknown" || !item.official_url : item.stage === stage);
    const matchesWorkflow = workflow === "all" || (workflow === "relevant" ? item.relevance === "relevant" || ["selected", "approved", "delivered"].includes(item.workflow_status) : workflow === "delivered" ? ["delivered", "approved"].includes(item.workflow_status) : item.workflow_status === workflow);
    return (!query || text.includes(query)) && matchesStage && matchesWorkflow;
  });
  $("#npa-rows").innerHTML = rows.length ? rows.map((item) => {
    const work = npaWorkState(item);
    return `<tr class="clickable-row" data-npa="${item._index}"><td><strong>${esc(item.official_identifier)}</strong><small>${item.tracked ? "Остаётся под наблюдением" : "Наблюдение завершено"}</small></td><td><span class="stage-pill ${item.stage === "unknown" ? "unknown" : ""}">${esc(stageLabels[item.stage] || item.stage || stageLabels.unknown)}</span><small>${item.effective_at ? `Действует с ${day(item.effective_at)}` : "Дата действия не подтверждена"}</small></td><td><span class="npa-work-state ${work.className}">${work.label}</span><small>${esc(work.caption)}</small></td><td>${esc(short(item.version_payload?.change_summary || item.version_payload?.summary || "Изменение пока не описано", 105))}<small>Обновлено ${fmt(item.version_created_at || item.created_at)}</small></td><td class="chevron">›</td></tr>`;
  }).join("") : `<tr><td colspan="5">${empty("Ничего не найдено", "Измените фильтр или поисковый запрос.")}</td></tr>`;
}

function npaWorkState(item) {
  const digest = item.last_digest;
  const digestCaption = digest ? `Выпуск v${digest.version} · ${day(digest.created_at)}` : "";
  return {
    delivered: { label: "Вошёл в выпуск", caption: digestCaption, className: "done" },
    approved: { label: "Готов к отправке", caption: digestCaption, className: "ready" },
    selected: { label: "Выбран в выпуск", caption: "Ещё не отправлен", className: "ready" },
    pending: { label: item.relevance === "unknown" ? "Требует оценки" : "Ждёт разбора", caption: item.official_url ? "Влияние на GS Labs ещё не подтверждено" : "Нужны первоисточник и оценка", className: "check" },
    excluded: { label: "Отсеян", caption: "Не вошёл в выпуск", className: "muted" },
    filtered: { label: "Вне фокуса", caption: "AI не нашёл влияния на GS Labs", className: "muted" },
  }[item.workflow_status] || { label: "Ждёт разбора", caption: "Нужно решение GR", className: "check" };
}

function openNpa(index) {
  const item = state.npa.find((entry) => entry._index === index); if (!item) return;
  const payload = item.version_payload || {}; const url = safeUrl(item.official_url || item.source_url);
  const work = npaWorkState(item);
  const claims = (payload.claims || []).map((claim) => `<div class="fact"><strong>${esc(claim.text)}</strong>${(claim.evidence || []).map((evidence) => `<div class="quote">«${esc(evidence.quote)}»</div>`).join("")}</div>`).join("");
  $("#drawer-content").innerHTML = `<div class="eyebrow">НПА · версия ${esc(item.version || 1)}</div><h2>${esc(item.official_identifier)}</h2><div class="meta"><span class="stage-pill">${esc(stageLabels[item.stage] || item.stage || stageLabels.unknown)}</span><span class="source-state ${item.tracked ? "working" : "disabled"}">${item.tracked ? "Под наблюдением" : "В архиве"}</span></div><div class="npa-work-card"><span>Рабочий статус</span><strong>${esc(work.label)}</strong><small>${esc(work.caption)}</small>${item.last_digest ? `<button class="button soft" data-open-digest="${esc(item.last_digest.id)}" data-version="${item.last_digest.version}">Открыть выпуск</button>` : ""}</div>
    <h3>Что изменилось</h3><p class="lead">${esc(payload.change_summary || payload.summary || "Содержание изменения пока не подтверждено.")}</p><h3>Возможное влияние</h3><p>${esc(payload.impact_on_gs_labs || "Влияние на GS Labs пока не определено.")}</p>
    ${claims ? `<h3>Подтверждённые факты</h3>${claims}` : ""}${(payload.unknowns || []).length ? `<h3>Что нужно уточнить</h3><ul>${payload.unknowns.map((unknown) => `<li>${esc(typeof unknown === "string" ? unknown : unknown.text)}</li>`).join("")}</ul>` : ""}
    <h3>Официальный источник</h3>${url ? `<a href="${url}" target="_blank" rel="noreferrer">Открыть документ ↗</a>` : '<p class="muted">Официальная ссылка ещё не подтверждена.</p>'}`;
  openDrawer();
}

async function renderEvents(lifecycle = "active") {
  const data = await api(`/api/events?state=${lifecycle}`); state.events = data.items;
  $("#view").innerHTML = `<div class="toolbar"><div class="tabs"><button class="tab ${lifecycle === "active" ? "active" : ""}" data-event-state="active">Активные</button><button class="tab ${lifecycle === "archived" ? "active" : ""}" data-event-state="archived">Архив</button><button class="tab ${lifecycle === "all" ? "active" : ""}" data-event-state="all">Все</button></div><span class="status-pill ok">${data.items.length} ${plural(data.items.length, "сюжет", "сюжета", "сюжетов")}</span></div><label class="search-field standalone-search"><span aria-hidden="true">⌕</span><input id="event-search" placeholder="Найти сюжет"></label><div class="compact-list" id="event-rows"></div>`;
  filterEvents();
}

function filterEvents() {
  const query = ($("#event-search")?.value || "").trim().toLowerCase();
  const items = state.events.filter((item) => `${item.payload?.title || ""} ${item.payload?.summary || ""}`.toLowerCase().includes(query));
  $("#event-rows").innerHTML = items.length ? items.map((item) => {
    const payload = item.payload || {};
    const sourceCount = (payload.material_ids || []).length;
    const summary = short(payload.summary || "", 145);
    const normalizedTitle = String(payload.title || "").replace(/\s+/g, " ").trim().toLowerCase();
    const normalizedSummary = String(payload.summary || "").replace(/\s+/g, " ").trim().toLowerCase();
    const sharedStart = normalizedTitle.slice(0, 90) && normalizedTitle.slice(0, 90) === normalizedSummary.slice(0, 90);
    const showSummary = summary && normalizedSummary !== normalizedTitle && !sharedStart;
    return `<article class="event-row clean" data-event="${esc(item.id)}"><div><h3>${esc(payload.title || item.id)}</h3>${showSummary ? `<p>${esc(summary)}</p>` : ""}<div class="signal-meta"><span>${sourceCount} ${sourceCount === 1 ? "источник" : sourceCount < 5 ? "источника" : "источников"}</span><i></i><span>Обновлено ${fmt(payload.last_meaningful_update_at || item.updated_at)}</span>${item.version > 1 ? `<i></i><span>${item.version} версии</span>` : ""}</div></div><b>›</b></article>`;
  }).join("") : empty("Сюжеты не найдены", "Измените поисковый запрос.");
}

async function openEvent(id) {
  const data = await api(`/api/events/${encodeURIComponent(id)}`);
  const event = data.event || {}; const payload = event.payload || {};
  $("#drawer-content").innerHTML = `<div class="eyebrow">СОБЫТИЕ · версия ${event.version}</div><h2>${esc(payload.title || event.id)}</h2><p class="lead">${esc(payload.summary || "")}</p><div class="meta"><span class="source-state ${event.lifecycle_state === "archived" ? "disabled" : "working"}">${event.lifecycle_state === "archived" ? "В архиве" : "Активно"}</span><span class="pill">${data.materials.length} источников</span></div>
    <h3>Источники события</h3><div class="source-links">${data.materials.length ? data.materials.map((material) => {
      const url = safeUrl(material.url);
      return `<div><span>${esc(kindLabels[material.kind] || material.kind || "Источник")}</span>${url ? `<a href="${url}" target="_blank" rel="noreferrer">${esc(material.title || material.source_name || "Материал")} ↗</a>` : `<strong>${esc(material.title || material.source_name || "Материал")}</strong>`}<small>${esc(material.source_name || "")} · ${fmt(material.published_at || material.fetched_at)}</small></div>`;
    }).join("") : '<p class="muted">Связанные оригиналы не найдены.</p>'}</div><h3>Жизненный цикл</h3><p>Первый сигнал: ${fmt(payload.first_seen_at || event.first_seen_at)}<br>Последнее содержательное изменение: ${fmt(payload.last_meaningful_update_at || event.last_meaningful_update_at)}</p>`;
  openDrawer();
}

const sourceState = (value) => ({ working: "Работает", attention: "Ошибка", coverage_gap: "Есть разрыв", disabled: "На паузе", not_checked: "Ещё не проверен" })[value] || "Состояние неизвестно";

async function renderSources(fetchData = true) {
  if (fetchData) state.sources = (await api("/api/sources")).items;
  const kinds = [...new Set(state.sources.map((item) => item.kind))].sort();
  $("#view").innerHTML = `<div class="toolbar"><div class="tabs"><button class="tab ${state.sourceMode === "sources" ? "active" : ""}" data-source-mode="sources">Подключения</button><button class="tab ${state.sourceMode === "materials" ? "active" : ""}" data-source-mode="materials">Входной поток</button></div><div><button class="button soft" data-job="collect">Проверить сейчас</button> <button class="button primary" id="add-source">+ Добавить источник</button></div></div><div id="source-content"></div>`;
  if (state.sourceMode === "materials") await renderMaterials();
  else {
    $("#source-content").innerHTML = `<div class="filter-bar"><label class="search-field"><span aria-hidden="true">⌕</span><input id="source-search" placeholder="Найти источник"></label><select id="source-kind"><option value="">Все способы</option>${kinds.map((kind) => `<option value="${esc(kind)}">${esc(kindLabels[kind] || kind)}</option>`).join("")}</select><select id="source-status"><option value="">Все состояния</option><option value="working">Работает</option><option value="attention">Ошибка</option><option value="coverage_gap">Есть разрыв</option><option value="disabled">На паузе</option><option value="not_checked">Не проверен</option></select><select id="source-direction"><option value="">Все направления</option><option value="pr">Только PR</option><option value="gr">Только GR</option><option value="both">Общий</option></select></div><div class="table-shell"><table class="table compact-table"><thead><tr><th>Источник</th><th>Способ</th><th>Состояние</th><th>Последний успешный сбор</th><th>Всего материалов</th><th></th></tr></thead><tbody id="source-rows"></tbody></table></div>`;
    filterSources();
  }
}

function filterSources() {
  const query = ($("#source-search")?.value || "").trim().toLowerCase();
  const kind = $("#source-kind")?.value || ""; const status = $("#source-status")?.value || ""; const direction = $("#source-direction")?.value || "";
  const rows = state.sources.filter((item) => {
    const matchesQuery = !query || `${item.name} ${item.fetch_url} ${item.notes}`.toLowerCase().includes(query);
    return matchesQuery && (!kind || item.kind === kind) && (!status || item.state === status) && (!direction || item.direction === direction);
  });
  $("#source-rows").innerHTML = rows.length ? rows.map((item) => `<tr class="clickable-row" data-source-open="${item.id}"><td><strong>${esc(item.name)}</strong><small>${esc(directionLabels[item.direction] || item.direction)}${item.category === "regulator" ? " · регуляторный" : ""}</small></td><td>${esc(kindLabels[item.kind] || item.kind)}</td><td><span class="source-state ${esc(item.state)}">${sourceState(item.state)}</span>${item.last_error ? `<small>${esc(short(item.last_error, 80))}</small>` : ""}</td><td>${fmt(item.last_success_at || item.last_fetch_at)}</td><td><strong>${item.documents}</strong></td><td class="chevron">›</td></tr>`).join("") : `<tr><td colspan="6">${empty("Ничего не найдено", "Измените фильтры или поисковый запрос.")}</td></tr>`;
}

async function renderMaterials() {
  if (!state.materials.length) state.materials = (await api("/api/materials?limit=180")).items;
  $("#source-content").innerHTML = `<div class="diagnostic-note"><strong>Диагностика потока</strong><span>Здесь можно проверить оригиналы и сбои. Для ежедневной работы PR/GR этот экран не нужен.</span></div><div class="filter-bar compact-filters"><label class="search-field"><span aria-hidden="true">⌕</span><input id="material-search" placeholder="Найти материал или источник"></label><select id="material-kind"><option value="">Все способы</option>${[...new Set(state.materials.map((item) => item.kind))].map((kind) => `<option value="${esc(kind)}">${esc(kindLabels[kind] || kind)}</option>`).join("")}</select><select id="material-state"><option value="attention" ${state.materialMode === "attention" ? "selected" : ""}>Требуют внимания</option><option value="pending">Ожидают AI</option><option value="signal">Есть сигнал</option><option value="irrelevant">Отсеяны</option><option value="all">Весь поток</option></select></div><div class="table-shell"><table class="table compact-table"><thead><tr><th>Материал</th><th>Источник</th><th>Направление</th><th>Дата</th><th>Состояние</th></tr></thead><tbody id="material-rows"></tbody></table></div>`;
  filterMaterials();
}

const analysisState = (value) => ({ ok: "Есть сигнал", irrelevant: "Не относится к задаче", no_signal: "Нет значимого сигнала", unreadable: "Не удалось прочитать", failed: "Ошибка анализа" })[value] || "Ожидает анализа";
function filterMaterials() {
  const query = ($("#material-search")?.value || "").trim().toLowerCase();
  const kind = $("#material-kind")?.value || ""; const status = $("#material-state")?.value || "attention";
  state.materialMode = status;
  const rows = state.materials.filter((item) => {
    const matchesQuery = !query || `${item.title} ${item.source_name} ${item.url}`.toLowerCase().includes(query);
    const matchesStatus = status === "all" || (status === "attention" ? ["failed", "unreadable"].includes(item.analysis_status) : status === "pending" ? !item.analysis_status : status === "signal" ? item.analysis_status === "ok" : status === "irrelevant" ? ["irrelevant", "no_signal"].includes(item.analysis_status) : true);
    return matchesQuery && (!kind || item.kind === kind) && matchesStatus;
  });
  $("#material-rows").innerHTML = rows.length ? rows.map((item) => {
    const url = safeUrl(item.url); const tone = item.analysis_status === "failed" || item.analysis_status === "unreadable" ? "attention" : item.analysis_status ? "working" : "not_checked";
    return `<tr><td>${url ? `<a href="${url}" target="_blank" rel="noreferrer"><strong>${esc(item.title || "Без заголовка")}</strong></a>` : `<strong>${esc(item.title || "Без заголовка")}</strong>`}<small>${esc(short(item.url, 100))}</small></td><td>${esc(item.source_name)}<small>${esc(kindLabels[item.kind] || item.kind)}</small></td><td>${esc(directionLabels[item.direction] || item.direction || "Общий")}</td><td>${fmt(item.published_at || item.fetched_at)}</td><td><span class="source-state ${tone}">${analysisState(item.analysis_status)}</span></td></tr>`;
  }).join("") : `<tr><td colspan="5">${empty("Ничего не найдено", "Измените фильтры или поисковый запрос.")}</td></tr>`;
}

async function openSource(id) {
  const data = await api(`/api/sources/${id}`); const source = data.source; const url = safeUrl(source.url || source.fetch_url);
  $("#drawer-content").innerHTML = `<div class="eyebrow">${esc(kindLabels[source.kind] || source.kind)} · ${esc(directionLabels[source.direction] || source.direction)}</div><h2>${esc(source.name)}</h2><div class="meta"><span class="source-state ${esc(source.state)}">${sourceState(source.state)}</span><span class="pill">${source.documents} материалов</span></div><p class="lead">${esc(source.notes || "Описание источника не добавлено.")}</p>${source.last_error ? `<div class="notice danger"><strong>Последняя ошибка</strong><p>${esc(source.last_error)}</p></div>` : ""}
    <div class="detail-grid"><div><span>Последняя проверка</span><strong>${fmt(source.last_fetch_at)}</strong></div><div><span>Последний успех</span><strong>${fmt(source.last_success_at)}</strong></div><div><span>Тип потока</span><strong>${source.category === "regulator" ? "Регуляторный" : "Обычный"}</strong></div><div><span>Состояние мониторинга</span><strong>${source.enabled ? "Включён" : "На паузе"}</strong></div></div>
    <h3>Последние материалы</h3><div class="source-links">${data.materials.length ? data.materials.map((material) => { const materialUrl = safeUrl(material.url); return `<div>${materialUrl ? `<a href="${materialUrl}" target="_blank" rel="noreferrer">${esc(material.title || "Без заголовка")} ↗</a>` : `<strong>${esc(material.title || "Без заголовка")}</strong>`}<small>${fmt(material.published_at || material.fetched_at)} · ${analysisState(material.analysis_status)}</small></div>`; }).join("") : '<p class="muted">Материалов пока нет.</p>'}</div>
    <div class="drawer-actions">${url ? `<a class="button soft button-link" href="${url}" target="_blank" rel="noreferrer">Открыть источник ↗</a>` : ""}${source.enabled ? `<button class="button soft" data-source-action="disable" data-source="${source.id}">Поставить на паузу</button>` : `<button class="button primary" data-source-action="enable" data-source="${source.id}">Возобновить</button>`}<button class="button danger" data-source-action="decommission" data-source="${source.id}">Убрать из мониторинга</button></div>`;
  openDrawer();
}

function sourceModal() {
  state.sourcePreview = null;
  $("#modal-content").innerHTML = `<div class="eyebrow">НОВЫЙ ИСТОЧНИК</div><h2 id="modal-title">Подключить мониторинг</h2><label>Адрес сайта, RSS или Telegram-канала<input id="source-url" placeholder="https://… или https://t.me/channel"></label><div class="form-grid"><label>Название<input id="source-name" placeholder="Определим автоматически"></label><label>Назначение<select id="source-category"><option value="media">Новости и отрасль</option><option value="regulator">Регулятор и НПА</option><option value="telegram">Telegram</option></select></label><label>Направление<select id="source-direction"><option value="both">PR + GR</option><option value="pr">PR</option><option value="gr">GR</option></select></label></div><p class="context-note">Система сначала проверит подключение без сохранения и покажет реальные примеры.</p><div id="source-preview"></div><div class="hero-actions"><button class="button primary" id="preview-source">Проверить подключение</button><button class="button acid" id="save-source" hidden>Подключить</button></div>`;
  $("#modal-backdrop").hidden = false;
}

function materialModal() {
  const today = dateInputValue(new Date());
  $("#modal-content").innerHTML = `<div class="eyebrow">НОВЫЙ МАТЕРИАЛ</div><h2 id="modal-title">Добавить сигнал в обработку</h2><label>Ссылка на первоисточник<input id="material-url" type="url" placeholder="https://…"></label><div class="form-grid"><label>Заголовок<input id="material-title" placeholder="Можно оставить пустым для ссылки"></label><label>Дата публикации<input id="material-date" type="date" value="${today}"></label></div><label>Текст материала <span class="muted">(необязательно, если достаточно ссылки)</span><textarea id="material-text" rows="8" placeholder="Вставьте текст поста, письма или материала…"></textarea></label><p class="context-note">Ссылка обязательна как подтверждение происхождения. Материал сохранится как оригинал и пройдёт тот же AI-анализ, что автоматический поток.</p><button class="button primary" id="save-material">Добавить и проанализировать</button>`;
  $("#modal-backdrop").hidden = false;
}

async function renderContext() {
  const data = await api("/api/context");
  state.context = data.context || { version: "", common: { text: "", important_examples: [], unimportant_examples: [] }, pr: { text: "", important_examples: [], unimportant_examples: [] }, gr: { text: "", important_examples: [], unimportant_examples: [] } };
  const blocks = [["common", "Общий контекст"], ["pr", "PR"], ["gr", "GR и НПА"]];
  const match = String(data.version || "").match(/v?(\d+(?:\.\d+)*)/i);
  const versionLabel = data.version ? `Версия ${match?.[1] || data.version}` : "Ещё не создан";
  $("#view").innerHTML = `<article class="card form-card"><div class="context-note"><strong>Как это работает:</strong> свободное описание задаёт направление, а примеры важного и неважного помогают AI понять границу. Изменения применятся только после сохранения.</div><div class="context-tabs">${blocks.map(([key, label], index) => `<button class="tab ${index ? "" : "active"}" data-context-tab="${key}">${label}</button>`).join("")}</div>${blocks.map(([key, label], index) => {
    const block = state.context[key] || {};
    return `<section class="context-panel ${index ? "" : "active"}" data-context-panel="${key}"><label>${label}: что учитывать<textarea data-field="text">${esc(block.text || "")}</textarea></label><div class="form-grid"><label>Примеры важного · один на строку<textarea data-field="important_examples">${esc((block.important_examples || []).join("\n"))}</textarea></label><label>Примеры неважного · один на строку<textarea data-field="unimportant_examples">${esc((block.unimportant_examples || []).join("\n"))}</textarea></label></div></section>`;
  }).join("")}<div class="context-save"><label>Название обновления<input id="context-version" placeholder="Обновление от 7 сентября"></label><div class="active-version"><span>Сейчас используется</span><strong>${esc(versionLabel)}</strong></div><button class="button primary" id="save-context">Сохранить новую версию</button></div></article>`;
}

async function renderDigest() {
  const [queue, digests] = await Promise.all([api("/api/queue?resolved=1"), api("/api/digests")]);
  state.digests = digests.items;
  const included = queue.items.filter((item) => item.latest_decision === "include");
  state.queue = included;
  const includedIds = new Set(included.map((item) => item.signal?.signal_id));
  state.selected = new Set([...state.selected].filter((id) => includedIds.has(id)));
  if (!state.selected.size) included.forEach((item) => state.selected.add(item.signal?.signal_id));
  const prSelected = included.filter((item) => state.selected.has(item.signal?.signal_id) && ["PR", "BOTH"].includes(item.signal?.interest)).length;
  const grSelected = included.filter((item) => state.selected.has(item.signal?.signal_id) && ["GR", "BOTH"].includes(item.signal?.interest)).length;
  $("#view").innerHTML = `<div class="digest-grid"><section><div class="section-head"><div><h2>Материалы выпуска</h2><p>Все подтверждённые сигналы уже выбраны — лишние можно снять.</p></div><span class="status-pill ok">${included.length} подтверждено</span></div><div class="compact-list">${included.length ? included.map((item) => digestRow(item)).join("") : empty("Нет подтверждённых сигналов", "Сначала разберите новые материалы.")}</div></section><aside class="card digest-side"><div class="eyebrow">ЕДИНЫЙ ВЫПУСК</div><h2><span id="selected-count">${state.selected.size}</span> выбрано</h2><p class="digest-audience">PR: <span id="pr-selected">${prSelected}</span> · GR: <span id="gr-selected">${grSelected}</span></p><div class="date-fields"><label>Начало периода<input id="period-from" type="date"></label><label>Конец периода<input id="period-to" type="date"></label></div><label>Способ доставки<select id="recipient-channel"><option value="preview">Только предпросмотр</option><option value="telegram">Telegram</option><option value="email">Электронная почта</option></select></label><label id="recipient-label" hidden>Получатель<input id="recipient" placeholder="@канал или адрес"></label><button class="button primary wide-button" id="create-digest">Собрать черновик</button><div class="digest-history"><h3>Сохранённые выпуски</h3>${digests.items.slice(0, 7).map((digest) => `<div class="digest-version"><button data-open-digest="${esc(digest.id)}" data-version="${digest.version}"><strong>${day(digest.payload?.period?.to || digest.created_at)}</strong><span>${digestStatus[digest.status] || digest.status} · версия ${digest.version}</span></button>${digest.status === "draft" ? `<button class="approve-mini" data-approve="${esc(digest.id)}" data-version="${digest.version}">Подтвердить</button>` : ""}</div>`).join("") || '<p class="muted">Выпусков ещё нет.</p>'}</div></aside></div>`;
  const today = new Date(); const weekAgo = new Date(today); weekAgo.setDate(today.getDate() - 6);
  $("#period-to").value = dateInputValue(today); $("#period-from").value = dateInputValue(weekAgo);
}

function openDigest(id, version) {
  const digest = state.digests.find((item) => item.id === id && Number(item.version) === Number(version)); if (!digest) return;
  const payload = digest.payload || {}; const sections = { critical: "Критичное", gr: "GR", pr: "PR" };
  const generated = ["PR", "GR"].map((role) => payload.role_content?.[role]?.text ? `<article class="digest-preview generated-preview"><div class="eyebrow">${role}</div>${renderDigestText(payload.role_content[role].text)}</article>` : "").join("");
  const legacy = (payload.items || []).map((item) => `<article class="digest-preview"><div class="eyebrow">${esc(sections[item.section] || item.section)}</div><h3>${esc(item.signal?.summary || item.signal_id)}</h3><p>${esc(item.signal?.impact || "")}</p>${metaPills(item.signal || {})}</article>`).join("");
  $("#drawer-content").innerHTML = `<div class="eyebrow">ВЫПУСК · ${esc(digestStatus[digest.status] || digest.status)} · версия ${digest.version}</div><h2>${esc(payload.title || "Информационная повестка GS Labs")}</h2><p>${day(payload.period?.from)} — ${day(payload.period?.to)}</p>${payload.header ? `<p class="lead">${esc(payload.header)}</p>` : ""}${generated || legacy || '<p class="muted">В выпуске нет материалов.</p>'}${payload.footer ? `<p>${esc(payload.footer)}</p>` : ""}<div class="drawer-actions"><button class="button primary" data-export-digest="${esc(digest.id)}" data-version="${digest.version}">Скачать Markdown</button></div>`;
  openDrawer();
}

function exportDigest(id, version) {
  const digest = state.digests.find((item) => item.id === id && Number(item.version) === Number(version)); if (!digest) return;
  const payload = digest.payload || {}; const sections = { critical: "Критичное", gr: "GR", pr: "PR" };
  const lines = [`# ${payload.title || `Повестка за ${payload.period?.to || "—"}`}`, "", `Период: ${payload.period?.from || "—"} — ${payload.period?.to || "—"}`, `Статус: ${digestStatus[digest.status] || digest.status}`, ""];
  if (payload.header) lines.push(payload.header, "");
  const generatedRoles = ["PR", "GR"].filter((role) => payload.role_content?.[role]?.text);
  for (const role of generatedRoles) lines.push(`## ${role}`, "", payload.role_content[role].text, "");
  if (!generatedRoles.length) {
  for (const item of payload.items || []) {
    lines.push(`## ${sections[item.section] || item.section}`, "", `### ${item.signal?.summary || item.signal_id}`, "");
    if (item.signal?.impact) lines.push(item.signal.impact, "");
    for (const claim of item.signal?.claims || []) { lines.push(`- ${claim.text}`); if (claim.evidence_quote) lines.push(`  - Основание: «${claim.evidence_quote}»`); }
    lines.push("");
  }
  }
  if (payload.footer) lines.push(payload.footer, "");
  const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `povestka-${payload.period?.to || "draft"}-v${digest.version}.md`; link.click(); URL.revokeObjectURL(link.href);
}

function historyPresentation(item) {
  const status = item.payload?.status;
  const mapping = {
    "analysis.completed": [status === "irrelevant" ? "Материал признан нерелевантным" : "AI завершил анализ материала", "ai"],
    "workflow.completed": ["Материал полностью обработан", "system"], "workflow.failed": ["Обработка материала завершилась ошибкой", "error"],
    "signal.revised": [item.actor === "ai" ? "AI создал сигнал" : "Человек изменил сигнал", item.actor === "ai" ? "ai" : "human"],
    "research.completed": [status === "not_needed" ? "Дополнительный поиск не потребовался" : "Дополнительный поиск завершён", "research"],
    "npa.version_created": ["Сохранена новая версия НПА", "npa"], "npa.official_source_repaired": ["Подтверждён официальный источник НПА", "npa"],
    "review.include": ["Сигнал добавлен в выпуск", "human"], "review.exclude": ["Сигнал исключён человеком", "human"],
    "digest.draft_created": ["Создан черновик выпуска", "digest"], "digest.approved": ["Выпуск подтверждён", "digest"],
    "context.created": ["Сохранена новая версия контекста", "context"], "maintenance.news_retention.completed": ["Архив событий обновлён", "system"],
    "signal.provenance_repaired": ["Восстановлена связь сигнала с оригиналом", "system"],
  };
  return mapping[item.event_type] || ["Система сохранила изменение", "system"];
}

function groupedHistory(items) {
  const groups = new Map();
  for (const item of items) {
    const date = String(item.created_at || "").slice(0, 10);
    const key = `${date}|${item.event_type}|${item.actor || "system"}`;
    const group = groups.get(key) || { ...item, count: 0 };
    group.count += 1;
    if (String(item.created_at || "") > String(group.created_at || "")) Object.assign(group, item);
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

function groupedHistoryLabel(item) {
  if (item.count === 1) return historyPresentation(item);
  const labels = {
    "analysis.completed": [`AI проанализировал ${item.count} материалов`, "ai"],
    "workflow.completed": [`Система обработала ${item.count} материалов`, "system"],
    "workflow.failed": [`${item.count} материалов завершились ошибкой`, "error"],
    "signal.revised": [`Создано или обновлено ${item.count} сигналов`, item.actor === "ai" ? "ai" : "human"],
    "research.completed": [`Завершено ${item.count} дополнительных проверок`, "research"],
    "npa.version_created": [`Обновлено ${item.count} нормативных объектов`, "npa"],
    "npa.official_source_repaired": [`Подтверждены источники для ${item.count} НПА`, "npa"],
    "review.include": [`${item.count} ${plural(item.count, "сигнал добавлен", "сигнала добавлены", "сигналов добавлены")} в выпуск`, "human"],
    "review.exclude": [`${item.count} ${plural(item.count, "сигнал исключён", "сигнала исключены", "сигналов исключены")}`, "human"],
    "signal.provenance_repaired": [`Восстановлены оригиналы для ${item.count} сигналов`, "system"],
  };
  return labels[item.event_type] || [`${historyPresentation(item)[0]} · ${item.count}`, historyPresentation(item)[1]];
}
const actorLabel = (actor) => {
  if (actor === "ai") return "AI";
  if (!actor || actor === "system" || actor.startsWith("pilot-")) return "Система";
  if (actor === "demo-all") return "Рабочая команда";
  if (actor.startsWith("demo-")) return actor.slice(5).toUpperCase();
  return actor;
};

async function renderHistory() {
  const data = await api("/api/history");
  const technical = new Set(["analysis.completed", "workflow.completed", "research.completed", "signal.revised"]);
  const rawItems = state.historyMode === "all" ? data.items : data.items.filter((item) => !technical.has(item.event_type));
  const items = groupedHistory(rawItems);
  $("#view").innerHTML = `<div class="toolbar"><div class="tabs"><button class="tab ${state.historyMode === "important" ? "active" : ""}" data-history-mode="important">Главное</button><button class="tab ${state.historyMode === "all" ? "active" : ""}" data-history-mode="all">Вся обработка</button></div><span class="status-pill ok">${items.length} записей</span></div>${items.length ? `<article class="card history-card">${items.map((item) => {
    const [label, tone] = groupedHistoryLabel(item);
    return `<div class="history-event ${tone}"><time>${fmt(item.created_at)}</time><i></i><p><strong>${esc(label)}</strong><br><span>${esc(actorLabel(item.actor))}${item.object_type === "signal" ? " · сигнал" : item.object_type === "material" ? " · материал" : ""}</span></p></div>`;
  }).join("")}</article>` : empty("Здесь пока пусто", "Ключевые действия появятся после проверки сигналов и выпуска повестки.")}`;
}

const render = { overview: renderOverview, queue: renderQueue, digest: renderDigest, npa: renderNpa, sources: renderSources, materials: renderSources, events: renderEvents, context: renderContext, history: renderHistory };

function syncChrome() {
  const profileViews = new Set(["overview", "queue"]);
  $("#profile-control").hidden = !profileViews.has(state.view);
  $$('[data-profile]').forEach((button) => button.classList.toggle("active", button.dataset.profile === state.profile));
  $("#run-cycle").hidden = state.view !== "overview";
  $("#settings-nav").open = document.body.classList.contains("sidebar-collapsed") || ["sources", "materials", "events", "context", "history"].includes(state.view);
  $("#job-banner").hidden = true;
}

async function load() {
  if (state.view !== "queue") { clearInterval(state.workspacePoll); state.workspacePoll = null; }
  const meta = views[state.view] || views.overview;
  $("#section-label").textContent = meta[0]; $("#page-title").textContent = meta[1]; $("#page-subtitle").textContent = meta[2];
  syncChrome();
  $("#view").innerHTML = empty("Загружаем", "Получаем актуальное состояние системы.");
  try { await refreshStatus(); await render[state.view](); }
  catch (error) { $("#view").innerHTML = empty("Не удалось загрузить", error.message); toast(error.message, true); }
}

async function startJob(kind) {
  try {
    await api("/api/pilot/jobs", { method: "POST", body: JSON.stringify({ kind, limit: 30 }) });
    toast(kind === "collect" ? "Проверка источников запущена" : "Обновление запущено");
    await refreshStatus(); setTimeout(load, 1800);
  } catch (error) { toast(error.message, true); }
}

document.addEventListener("click", async (event) => {
  if (event.target.closest("#sidebar-toggle")) {
    document.body.classList.toggle("sidebar-collapsed");
    const collapsed = document.body.classList.contains("sidebar-collapsed");
    try { localStorage.setItem("gs-sidebar", collapsed ? "collapsed" : "expanded"); } catch {}
    syncSidebar();
    return;
  }
  const queueTag = event.target.closest("[data-queue-tag]");
  if (queueTag) { state.queueFilters.tag = queueTag.dataset.queueTag; renderQueue(false); return; }
  if (event.target.closest("[data-expand-digest]")) {
    const clone = $(".digest-workspace").cloneNode(true);
    clone.querySelectorAll("button").forEach((button) => button.remove());
    clone.querySelectorAll("[id]").forEach((node) => node.removeAttribute("id"));
    $("#modal-content").innerHTML = `<div class="eyebrow">ПРЕДПРОСМОТР</div><h2 id="modal-title">Общий дайджест</h2><div class="digest-full-preview">${clone.innerHTML}</div>`;
    $("#modal-backdrop").hidden = false;
    return;
  }
  if (event.target.closest("[data-reset-demo]")) {
    if (!window.confirm("Сбросить текущий дайджест и вернуть выбранные материалы в очередь?")) return;
    try {
      const result = await api("/api/demo/reset-digest", { method: "POST", body: JSON.stringify({ actor: "demo-reset" }) });
      state.digestOrder = [];
      state.roleSelections = { PR: [], GR: [] };
      state.currentDigest = null;
      state.workspaceFingerprint = "";
      toast(`Демо сброшено · ${result.signals_returned} материалов вернуто`);
      await renderQueue(false);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const nav = event.target.closest("[data-view]");
  if (nav) { state.view = nav.dataset.view; if (state.view === "sources") state.sourceMode = "sources"; $$(".nav-item").forEach((item) => item.classList.toggle("active", item === nav)); load(); return; }
  const go = event.target.closest("[data-go]");
  if (go) { state.view = go.dataset.go; if (go.dataset.sourceTarget) state.sourceMode = go.dataset.sourceTarget; $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === state.view)); closeDrawer(); load(); return; }
  const dashboardProfile = event.target.closest("[data-dashboard-profile]");
  if (dashboardProfile) { state.profile = dashboardProfile.dataset.dashboardProfile; state.view = "queue"; $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === "queue")); load(); return; }
  const profile = event.target.closest("[data-profile]");
  if (profile) { state.profile = profile.dataset.profile; $$("[data-profile]").forEach((button) => button.classList.toggle("active", button === profile)); load(); return; }
  if (event.target.closest("[data-open-job]")) { openJob(); return; }
  const job = event.target.closest("[data-job]"); if (job) { startJob(job.dataset.job); return; }
  const queue = event.target.closest("[data-queue]"); if (queue) { queueMode(queue.dataset.queue); return; }
  const signal = event.target.closest("[data-signal]"); if (signal && !event.target.closest("[data-review]")) { openSignal(signal.dataset.signal); return; }
  const npa = event.target.closest("[data-npa]"); if (npa) { openNpa(Number(npa.dataset.npa)); return; }
  const eventRow = event.target.closest("[data-event]"); if (eventRow) { openEvent(eventRow.dataset.event); return; }
  const eventState = event.target.closest("[data-event-state]"); if (eventState) { renderEvents(eventState.dataset.eventState); return; }
  const sourceMode = event.target.closest("[data-source-mode]"); if (sourceMode) { state.sourceMode = sourceMode.dataset.sourceMode; renderSources(false); return; }
  const historyMode = event.target.closest("[data-history-mode]"); if (historyMode) { state.historyMode = historyMode.dataset.historyMode; renderHistory(); return; }
  const digestMetaEdit = event.target.closest("[data-edit-digest-meta]");
  if (digestMetaEdit) { editDigestMeta(digestMetaEdit.dataset.editDigestMeta); return; }
  const roleContentEdit = event.target.closest("[data-edit-role-content]");
  if (roleContentEdit) { editRoleContent(roleContentEdit.dataset.editRoleContent); return; }
  const sourceOpen = event.target.closest("[data-source-open]"); if (sourceOpen && !event.target.closest("[data-source-action]")) { openSource(sourceOpen.dataset.sourceOpen); return; }
  const review = event.target.closest("[data-review]");
  if (review) {
    try { await api("/api/review", { method: "POST", body: JSON.stringify({ signal_id: review.dataset.id, revision: Number(review.dataset.rev), decision: review.dataset.review, actor: `demo-${state.profile.toLowerCase()}` }) }); const messages = { include: "Сигнал добавлен в дайджест", exclude: "Сигнал перенесён в отсеянное", restore: "Сигнал возвращён в разбор" }; toast(messages[review.dataset.review] || "Решение сохранено"); closeDrawer(); state.view === "queue" ? renderQueue(false) : load(); }
    catch (error) { toast(error.message, true); } return;
  }
  if (event.target.closest("[data-edit-signal]")) { editSignal(); return; }
  const sourceAction = event.target.closest("[data-source-action]");
  if (sourceAction) {
    if (sourceAction.dataset.sourceAction === "decommission" && !window.confirm("Убрать источник из мониторинга? Ранее собранные материалы сохранятся.")) return;
    try {
      await api(`/api/sources/${sourceAction.dataset.source}/${sourceAction.dataset.sourceAction}`, { method: "POST", body: "{}" });
      const message = sourceAction.dataset.sourceAction === "disable" ? "Источник поставлен на паузу" : sourceAction.dataset.sourceAction === "enable" ? "Мониторинг возобновлён" : "Источник удалён из мониторинга";
      toast(message); closeDrawer(); renderSources();
    } catch (error) { toast(error.message, true); } return;
  }
  const contextTab = event.target.closest("[data-context-tab]");
  if (contextTab) { $$("[data-context-tab]").forEach((item) => item.classList.toggle("active", item === contextTab)); $$("[data-context-panel]").forEach((item) => item.classList.toggle("active", item.dataset.contextPanel === contextTab.dataset.contextTab)); return; }
  const approve = event.target.closest("[data-approve]");
  if (approve) {
    try { await api(`/api/digests/${encodeURIComponent(approve.dataset.approve)}/${approve.dataset.version}/approve`, { method: "POST", body: JSON.stringify({ actor: `demo-${state.profile.toLowerCase()}` }) }); toast("Выпуск подтверждён"); closeDrawer(); renderDigest(); }
    catch (error) { toast(error.message, true); } return;
  }
  const digest = event.target.closest("[data-open-digest]"); if (digest) { openDigest(digest.dataset.openDigest, digest.dataset.version); return; }
  const exportButton = event.target.closest("[data-export-digest]"); if (exportButton) { exportDigest(exportButton.dataset.exportDigest, exportButton.dataset.version); return; }
  if (event.target.id === "add-material") { materialModal(); return; }
  if (event.target.id === "add-source") { sourceModal(); return; }
  if (event.target.id === "preview-source") {
    const button = event.target; button.disabled = true; button.textContent = "Проверяем…";
    try {
      const preview = await api("/api/sources/preview", { method: "POST", body: JSON.stringify({ url: $("#source-url").value, name: $("#source-name").value, category: $("#source-category").value, direction: $("#source-direction").value }) });
      state.sourcePreview = { ...preview, originalUrl: $("#source-url").value.trim() };
      if (!$("#source-name").value && preview.source?.name) $("#source-name").value = preview.source.name;
      $("#source-preview").innerHTML = `<div class="source-preview ${preview.can_confirm ? "ready" : "failed"}"><strong>${preview.can_confirm ? "Подключение работает" : "Подключение не подтверждено"}</strong><p>${esc(kindLabels[preview.source?.kind] || preview.source?.kind || "")} · ${preview.latency_ms || 0} мс${preview.error ? ` · ${esc(preview.error)}` : ""}</p>${(preview.examples || []).map((item) => `<div class="preview-item"><span>${day(item.published_at)}</span><b>${esc(item.title || "Без заголовка")}</b></div>`).join("") || "<small>Свежих материалов в текущем окне нет.</small>"}</div>`;
      $("#save-source").hidden = !preview.can_confirm;
    } catch (error) { state.sourcePreview = null; $("#source-preview").innerHTML = `<div class="source-preview failed"><strong>Не удалось проверить</strong><p>${esc(error.message)}</p></div>`; $("#save-source").hidden = true; }
    finally { button.disabled = false; button.textContent = "Проверить ещё раз"; } return;
  }
  if (event.target.id === "save-source") {
    try {
      const url = $("#source-url").value.trim(); if (!state.sourcePreview || state.sourcePreview.originalUrl !== url) throw new Error("Ссылка изменилась — проверьте её ещё раз");
      await api("/api/sources", { method: "POST", body: JSON.stringify({ url, name: $("#source-name").value, category: $("#source-category").value, direction: $("#source-direction").value, kind: state.sourcePreview.source.kind, fetch_url: state.sourcePreview.source.fetch_url }) });
      $("#modal-backdrop").hidden = true; toast("Источник подключён"); renderSources();
    } catch (error) { toast(error.message, true); } return;
  }
  if (event.target.id === "save-material") {
    const button = event.target;
    button.disabled = true;
    button.textContent = "Добавляем…";
    try {
      const result = await api("/api/materials/import", { method: "POST", body: JSON.stringify({ url: $("#material-url").value.trim(), title: $("#material-title").value.trim(), text: $("#material-text").value.trim(), published_at: $("#material-date").value }) });
      $("#modal-backdrop").hidden = true;
      toast(result.created ? (result.analysis === "started" ? "Материал добавлен — AI-анализ запущен" : "Материал добавлен в очередь AI") : "Этот материал уже был добавлен");
      await renderQueue(false);
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
      button.textContent = "Добавить и проанализировать";
    }
    return;
  }
  if (event.target.id === "save-signal") {
    try { await api(`/api/signals/${encodeURIComponent(event.target.dataset.id)}/revise`, { method: "POST", body: JSON.stringify({ revision: Number(event.target.dataset.rev), summary: $("#edit-summary").value, impact: $("#edit-impact").value, importance: $("#edit-importance").value, interest: $("#edit-interest").value, kind: $("#edit-kind").value, urgency: $("#edit-urgency").value, actor: `demo-${state.profile.toLowerCase()}` }) }); $("#modal-backdrop").hidden = true; closeDrawer(); toast("Изменённая версия сохранена"); load(); }
    catch (error) { toast(error.message, true); } return;
  }
  if (event.target.id === "save-context") {
    try {
      $$("[data-context-panel]").forEach((panel) => { const key = panel.dataset.contextPanel; state.context[key] = {}; panel.querySelectorAll("[data-field]").forEach((field) => { state.context[key][field.dataset.field] = field.dataset.field === "text" ? field.value : field.value.split("\n").map((item) => item.trim()).filter(Boolean); }); });
      const version = $("#context-version").value.trim(); state.context.version = version;
      await api("/api/context", { method: "PUT", body: JSON.stringify({ version, context: state.context, actor: "demo-admin" }) }); toast("Новая версия контекста сохранена"); renderContext();
    } catch (error) { toast(error.message, true); } return;
  }
  if (event.target.id === "save-digest-meta") {
    const key = event.target.dataset.metaKey;
    state.digestMeta[key] = $("#digest-meta-value").value.trim();
    state.digestMetaUpdated[key] = new Date();
    $("#modal-backdrop").hidden = true;
    toast("Оформление выпуска обновлено");
    renderQueue(false);
    return;
  }
  if (event.target.id === "save-role-content") {
    const role = event.target.dataset.role;
    try {
      const digest = state.currentDigest;
      await api(`/api/digests/${encodeURIComponent(digest.id)}/${digest.version}/role-content`, { method: "POST", body: JSON.stringify({ role, signal_ids: state.roleSelections[role], text: $("#role-content-value").value, actor: `demo-${role.toLowerCase()}` }) });
      $("#modal-backdrop").hidden = true;
      toast(`${role}-блок обновлён`);
      renderQueue(false);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const generateRole = event.target.closest("[data-generate-role]");
  if (generateRole) {
    const role = generateRole.dataset.generateRole;
    generateRole.disabled = true;
    generateRole.textContent = "AI собирает текст…";
    try {
      let digest = state.currentDigest;
      if (!digest) {
        const payload = await api("/api/digests", { method: "POST", body: JSON.stringify({ id: `weekly-${dateInputValue(new Date())}`, period_from: $("#period-from").value, period_to: $("#period-to").value, signal_ids: state.digestOrder, recipients: [{ channel: "telegram", recipient: "default" }], title: state.digestMeta.title, header: state.digestMeta.header, footer: state.digestMeta.footer, actor: `demo-${role.toLowerCase()}` }) });
        digest = { id: payload.id, version: payload.version, status: "draft", payload };
        state.currentDigest = digest;
      }
      await api(`/api/digests/${encodeURIComponent(digest.id)}/${digest.version}/generate-role`, { method: "POST", body: JSON.stringify({ role, signal_ids: state.roleSelections[role], actor: `demo-${role.toLowerCase()}` }) });
      toast(`${role}-блок собран через AI`);
      renderQueue(false);
    } catch (error) {
      toast(error.message, true);
      generateRole.disabled = false;
      generateRole.textContent = "Повторить AI-сборку";
    }
    return;
  }
  const ready = event.target.closest("[data-ready-role]");
  if (ready) {
    try {
      const result = await api(`/api/digests/${encodeURIComponent(ready.dataset.digestId)}/${ready.dataset.version}/ready`, { method: "POST", body: JSON.stringify({ role: ready.dataset.readyRole, actor: `demo-${ready.dataset.readyRole.toLowerCase()}` }) });
      toast(result.status === "delivered" ? "PR и GR готовы — дайджест отправлен в Telegram" : result.status === "delivery_failed" ? "Выпуск готов, но Telegram не принял сообщение. Проверьте настройки и повторите отправку." : `Блок ${ready.dataset.readyRole} готов. Ждём второй блок.`, result.status === "delivery_failed");
      renderQueue(false);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const retryDelivery = event.target.closest("[data-retry-delivery]");
  if (retryDelivery) {
    try {
      const result = await api(`/api/digests/${encodeURIComponent(retryDelivery.dataset.digestId)}/${retryDelivery.dataset.version}/deliver`, { method: "POST", body: "{}" });
      toast(result.status === "delivered" ? "Дайджест отправлен в Telegram" : "Telegram не принял сообщение. Проверьте настройки.", result.status !== "delivered");
      renderQueue(false);
    } catch (error) { toast(error.message, true); }
    return;
  }
});

document.addEventListener("input", (event) => {
  if (event.target.id === "queue-search") {
    state.queueFilters.search = event.target.value;
    const query = event.target.value.trim().toLowerCase();
    $$(".work-signal").forEach((item) => { item.hidden = Boolean(query) && !item.dataset.searchText.includes(query); });
  }
  if (["source-search", "source-kind", "source-status", "source-direction"].includes(event.target.id)) filterSources();
  if (["material-search", "material-kind", "material-state"].includes(event.target.id)) filterMaterials();
  if (event.target.id === "event-search") filterEvents();
  if (event.target.id === "npa-search") filterNpa();
});
document.addEventListener("change", (event) => {
  if (["queue-source", "queue-date", "queue-importance", "queue-kind"].includes(event.target.id)) {
    const key = event.target.id.replace("queue-", "");
    state.queueFilters[key] = event.target.value;
    renderQueue(false);
    return;
  }
  if (["source-kind", "source-status", "source-direction"].includes(event.target.id)) filterSources();
  if (["material-kind", "material-state"].includes(event.target.id)) filterMaterials();
  if (["npa-stage", "npa-workflow"].includes(event.target.id)) filterNpa();
  if (event.target.id === "recipient-channel") {
    const label = $("#recipient-label");
    if (label) label.hidden = event.target.value === "preview";
  }
  if (event.target.matches("[data-pick]")) {
    event.target.checked ? state.selected.add(event.target.dataset.pick) : state.selected.delete(event.target.dataset.pick);
    const chosen = state.queue.filter((item) => state.selected.has(item.signal?.signal_id));
    const count = $("#selected-count"); if (count) count.textContent = chosen.length;
    const pr = $("#pr-selected"); if (pr) pr.textContent = chosen.filter((item) => ["PR", "BOTH"].includes(item.signal?.interest)).length;
    const gr = $("#gr-selected"); if (gr) gr.textContent = chosen.filter((item) => ["GR", "BOTH"].includes(item.signal?.interest)).length;
  }
});

document.addEventListener("dragstart", (event) => {
  const item = event.target.closest("[data-selected]");
  if (!item) return;
  state.draggedId = item.dataset.selected;
  item.classList.add("dragging");
  event.dataTransfer.effectAllowed = "move";
});
document.addEventListener("dragover", (event) => {
  if (state.draggedId && event.target.closest("[data-selected]")) event.preventDefault();
});
document.addEventListener("drop", (event) => {
  const target = event.target.closest("[data-selected]");
  if (!target || !state.draggedId || target.dataset.selected === state.draggedId) return;
  event.preventDefault();
  const from = state.digestOrder.indexOf(state.draggedId); const to = state.digestOrder.indexOf(target.dataset.selected);
  if (from >= 0 && to >= 0) { const [moved] = state.digestOrder.splice(from, 1); state.digestOrder.splice(to, 0, moved); renderQueue(false); }
  state.draggedId = null;
});
document.addEventListener("dragend", () => { state.draggedId = null; $$(".dragging").forEach((item) => item.classList.remove("dragging")); });

$("#drawer-close").onclick = closeDrawer;
$("#drawer-backdrop").onclick = closeDrawer;
$("#modal-close").onclick = () => { $("#modal-backdrop").hidden = true; };
$("#refresh").onclick = load;
$("#run-cycle").onclick = () => startJob("cycle");
function syncSidebar() {
  const collapsed = document.body.classList.contains("sidebar-collapsed");
  const toggle = $("#sidebar-toggle");
  toggle.textContent = collapsed ? "›" : "‹";
  toggle.setAttribute("aria-label", collapsed ? "Развернуть меню" : "Свернуть меню");
  toggle.title = collapsed ? "Развернуть меню" : "Свернуть меню";
  if (collapsed) $("#settings-nav").open = true;
}
try {
  if (localStorage.getItem("gs-sidebar") === "expanded") document.body.classList.remove("sidebar-collapsed");
} catch {}
syncSidebar();
load();
