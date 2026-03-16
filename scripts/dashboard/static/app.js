/* my-tasks dashboard – Drill-down view */

const BASE = window.__BASE_PATH__ || "";

const state = {
  tasks: [],
  lifecycles: [],
  jobs: [],
  viewStack: [{ type: "tasks" }],
};

let logRefreshTimer = null;

// --- Fetch helpers ---

async function fetchJSON(url) {
  const res = await fetch(url);
  return res.ok ? res.json() : [];
}

async function loadAll() {
  const [tasks, lifecycles, jobs] = await Promise.all([
    fetchJSON(`${BASE}/api/tasks`),
    fetchJSON(`${BASE}/api/lifecycles`),
    fetchJSON(`${BASE}/api/jobs`),
  ]);
  state.tasks = tasks;
  state.lifecycles = lifecycles;
  state.jobs = jobs;
  renderCurrentView();
}

// --- SSE ---

function connectSSE() {
  const es = new EventSource(`${BASE}/api/events`);
  const dot = document.getElementById("connection-status");

  es.onopen = () => {
    dot.className = "status-dot connected";
    dot.title = "SSE connected";
  };

  es.onerror = () => {
    dot.className = "status-dot disconnected";
    dot.title = "SSE disconnected";
    es.close();
    setTimeout(connectSSE, 3000);
  };

  es.addEventListener("tasks_updated", async () => {
    state.tasks = await fetchJSON(`${BASE}/api/tasks`);
    handleSSEUpdate("tasks");
  });

  es.addEventListener("jobs_updated", async () => {
    state.jobs = await fetchJSON(`${BASE}/api/jobs`);
    handleSSEUpdate("jobs");
  });

  es.addEventListener("lifecycles_updated", async () => {
    state.lifecycles = await fetchJSON(`${BASE}/api/lifecycles`);
    handleSSEUpdate("lifecycles");
  });
}

function handleSSEUpdate(dataType) {
  const view = currentView();
  switch (view.type) {
    case "tasks":
      if (dataType === "tasks") renderTaskList();
      break;
    case "task":
      if (dataType === "tasks") updateTaskSummary(view.taskId);
      if (dataType === "lifecycles") updateLifecycleList(view.taskId);
      break;
    case "lifecycle":
      if (dataType === "lifecycles") updateLifecycleSummary(view.lifecycleId);
      if (dataType === "jobs") updateJobList(view.lifecycleId);
      break;
    case "job":
      if (dataType === "jobs") {
        const job = state.jobs.find((j) => j.dispatch_id === view.dispatchId);
        if (job && job.status !== "running") renderJobView(view.dispatchId);
      }
      break;
  }
}

// --- Action helpers ---

async function postAction(url) {
  const res = await fetch(url, { method: "POST" });
  return res.json();
}

function showNotification(message, isError = false) {
  const el = document.createElement("div");
  el.className = "notification" + (isError ? " error" : "");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add("fade-out"), 2500);
  setTimeout(() => el.remove(), 3000);
}

async function handleAction(btn, url, loadingText) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = loadingText || "...";
  try {
    const result = await postAction(url);
    if (result.ok) {
      showNotification(result.message || "OK");
      btn.disabled = false;
      btn.textContent = origText;
    } else {
      showNotification(result.error || "エラー", true);
      btn.disabled = false;
      btn.textContent = origText;
    }
  } catch {
    showNotification("通信エラー", true);
    btn.disabled = false;
    btn.textContent = origText;
  }
}

// --- Helpers ---

function statusBadge(status) {
  return `<span class="status status-${status || "unknown"}">${status || "-"}</span>`;
}

function taskDisplayStatus(task) {
  if (task.status !== "in_progress") return task.status;
  const prefix = task.id + "-g";
  const taskLCs = state.lifecycles.filter(
    (lc) => lc.lifecycle_id && lc.lifecycle_id.startsWith(prefix)
  );
  if (!taskLCs.length) return "in_review";
  const latest = taskLCs[taskLCs.length - 1];
  const activeStatuses = ["planning", "phase_executing", "phase_evaluating"];
  return activeStatuses.includes(latest.status) ? "running" : "in_review";
}

function duration(startedAt, finishedAt) {
  if (!startedAt) return "";
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const diff = Math.floor((end - start) / 1000);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m${diff % 60}s`;
  return `${Math.floor(diff / 3600)}h${Math.floor((diff % 3600) / 60)}m`;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// --- Navigation ---

function currentView() {
  return state.viewStack[state.viewStack.length - 1];
}

function pushView(view) {
  state.viewStack.push(view);
  renderCurrentView();
}

function popView() {
  if (state.viewStack.length > 1) {
    state.viewStack.pop();
    renderCurrentView();
  }
}

// --- Main render ---

function renderCurrentView() {
  clearLogRefresh();
  const view = currentView();
  const container = document.getElementById("view-container");
  const titleEl = document.getElementById("header-title");
  const backBtn = document.getElementById("nav-back");

  backBtn.classList.toggle("hidden", state.viewStack.length <= 1);

  switch (view.type) {
    case "tasks":
      titleEl.textContent = "my-tasks";
      renderTaskList();
      break;
    case "task": {
      const task = state.tasks.find((t) => t.id === view.taskId);
      titleEl.textContent = task?.title || view.taskId;
      renderTaskView(view.taskId);
      break;
    }
    case "lifecycle": {
      const genMatch = view.lifecycleId.match(/-g(\d+)$/);
      titleEl.textContent = `Generation ${genMatch ? genMatch[1] : "?"}`;
      renderLifecycleView(view.lifecycleId);
      break;
    }
    case "job":
      titleEl.textContent = view.dispatchId;
      renderJobView(view.dispatchId);
      break;
  }

  container.scrollTop = 0;
}

// --- Task List (Screen 1) ---

function renderTaskList() {
  const container = document.getElementById("view-container");
  const tasks = state.tasks;

  if (!tasks.length) {
    container.innerHTML = '<div class="empty-state">No tasks</div>';
    return;
  }

  const groups = {};
  tasks.forEach((t) => {
    const pid = t.project_id || t.project || "(none)";
    if (!groups[pid]) groups[pid] = [];
    groups[pid].push(t);
  });

  let html = "";
  for (const pid of Object.keys(groups).sort()) {
    html += `<div class="section-header">${escapeHtml(pid)}</div>`;
    for (const t of groups[pid]) {
      html += `<div class="list-item" data-task-id="${t.id}">
        <div class="item-main">
          ${statusBadge(taskDisplayStatus(t))}
          <span class="item-title">${escapeHtml(t.title || t.id)}</span>
        </div>
        <span class="chevron">\u203a</span>
      </div>`;
    }
  }

  container.innerHTML = html;
  container.querySelectorAll("[data-task-id]").forEach((el) => {
    el.addEventListener("click", () =>
      pushView({ type: "task", taskId: el.dataset.taskId })
    );
  });
}

// --- Task View (Screen 2): task context + lifecycle list ---

function buildLifecycleListHTML(taskId) {
  const prefix = taskId + "-g";
  const taskLifecycles = state.lifecycles.filter(
    (lc) => lc.lifecycle_id && lc.lifecycle_id.startsWith(prefix)
  );

  if (!taskLifecycles.length) return "";

  let html = '<div class="section-header">Lifecycles</div>';
  for (const lc of taskLifecycles) {
    const genMatch = lc.lifecycle_id.match(/-g(\d+)$/);
    const gen = genMatch ? genMatch[1] : "?";
    const phases = lc.phases || [];
    const currentPhase = lc.current_phase || 0;
    const phaseInfo =
      phases.length > 0 ? `Phase ${currentPhase + 1}/${phases.length}` : "";
    const currentGoal = phases[currentPhase]?.goal || "";

    html += `<div class="list-item" data-lifecycle-id="${lc.lifecycle_id}">
      <div class="item-main">
        ${statusBadge(lc.status)}
        <span class="item-title">Generation ${gen}</span>
      </div>
      <div class="item-detail">
        ${phaseInfo ? `<span class="item-meta">${phaseInfo}</span>` : ""}
        ${currentGoal ? `<span class="item-sub">${escapeHtml(currentGoal)}</span>` : ""}
        ${lc.suspend_reason ? `<span class="item-sub suspend">${lc.suspend_reason}</span>` : ""}
      </div>
      <span class="chevron">\u203a</span>
    </div>`;
  }

  return html;
}

function bindLifecycleClicks(root) {
  root.querySelectorAll("[data-lifecycle-id]").forEach((el) => {
    el.addEventListener("click", () =>
      pushView({ type: "lifecycle", lifecycleId: el.dataset.lifecycleId })
    );
  });
}

function bindTaskActions(root) {
  root.querySelectorAll("[data-action=dispatch]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/tasks/${btn.dataset.taskId}/dispatch`, "Dispatching...");
    });
  });
  root.querySelectorAll("[data-action=redispatch]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/tasks/${btn.dataset.taskId}/redispatch`, "Re-dispatching...");
    });
  });
}

async function renderTaskView(taskId) {
  const container = document.getElementById("view-container");
  const task = state.tasks.find((t) => t.id === taskId);

  let html = '<div class="context-card">';
  if (task) {
    html += '<div class="context-meta" id="task-meta">';
    html += buildTaskMetaHTML(task);
    html += "</div>";
  }
  html +=
    '<div id="task-detail-content"><div class="empty-state">Loading...</div></div>';
  html += "</div>";
  html += `<div id="lifecycle-list-section">${buildLifecycleListHTML(taskId)}</div>`;

  container.innerHTML = html;
  bindLifecycleClicks(container);
  bindTaskActions(container);

  try {
    const data = await fetchJSON(`${BASE}/api/tasks/${taskId}`);
    const detailEl = document.getElementById("task-detail-content");
    if (
      !detailEl ||
      currentView().type !== "task" ||
      currentView().taskId !== taskId
    )
      return;
    if (data.content) {
      detailEl.innerHTML = `<pre class="preview-text">${escapeHtml(data.content)}</pre>`;
    } else {
      detailEl.innerHTML = renderTaskDetailSections(data);
    }
  } catch {
    const detailEl = document.getElementById("task-detail-content");
    if (detailEl)
      detailEl.innerHTML = '<div class="empty-state">Failed to load</div>';
  }
}

function buildTaskMetaHTML(task) {
  let html = `<span class="meta-item">${statusBadge(taskDisplayStatus(task))}</span>`;
  if (task.project_id)
    html += `<span class="meta-item">${escapeHtml(task.project_id)}</span>`;
  if (task.generation && task.generation > 1)
    html += `<span class="meta-item">Gen ${task.generation}</span>`;
  if (task.status === "pending" && task.project_id)
    html += `<button class="action-btn primary" data-action="dispatch" data-task-id="${task.id}">Dispatch</button>`;
  if (task.status === "done" || task.status === "aborted")
    html += `<button class="action-btn" data-action="redispatch" data-task-id="${task.id}">Re-dispatch</button>`;
  return html;
}

function updateTaskSummary(taskId) {
  const meta = document.getElementById("task-meta");
  if (!meta) return;
  const task = state.tasks.find((t) => t.id === taskId);
  if (!task) return;
  meta.innerHTML = buildTaskMetaHTML(task);
  bindTaskActions(meta);
  document.getElementById("header-title").textContent =
    task.title || task.id;
}

function updateLifecycleList(taskId) {
  const section = document.getElementById("lifecycle-list-section");
  if (!section) return;
  section.innerHTML = buildLifecycleListHTML(taskId);
  bindLifecycleClicks(section);
}

function renderTaskDetailSections(data) {
  let html = "";

  if (data.description) {
    html += `<div class="detail-section"><div class="detail-body">${escapeHtml(data.description)}</div></div>`;
  }

  const sections = [
    { key: "acceptance_criteria", label: "達成条件" },
    { key: "preconditions", label: "事前条件" },
    { key: "open_questions", label: "未決事項" },
    { key: "completion_actions", label: "完了時アクション" },
  ];
  for (const { key, label } of sections) {
    if (data[key]?.length > 0) {
      html += `<div class="detail-section"><div class="detail-heading">${label}</div><ul class="detail-list">`;
      data[key].forEach((item) => {
        html += `<li>${escapeHtml(item)}</li>`;
      });
      html += "</ul></div>";
    }
  }

  if (data.execute_prompt) {
    html += `<div class="detail-section"><div class="detail-heading">実行プロンプト</div><pre class="preview-text">${escapeHtml(data.execute_prompt)}</pre></div>`;
  }

  if (data.history?.length > 0) {
    html += '<div class="detail-section"><div class="detail-heading">実行履歴</div>';
    data.history.forEach((h) => {
      html += `<div class="phase-item"><span class="phase-num">G${h.generation || "?"}</span><span class="phase-goal">${escapeHtml(h.summary || "")}</span></div>`;
    });
    html += "</div>";
  }

  return html;
}

// --- Lifecycle View (Screen 3): lifecycle context + job list ---

function buildJobListHTML(lifecycleId) {
  const jobs = state.jobs.filter((j) => j.lifecycle_id === lifecycleId);
  if (!jobs.length) return "";

  const lc = state.lifecycles.find((l) => l.lifecycle_id === lifecycleId);
  const phases = lc?.phases || [];

  const byPhase = {};
  jobs.forEach((j) => {
    const phase = j.run != null ? j.run : 0;
    if (!byPhase[phase]) byPhase[phase] = [];
    byPhase[phase].push(j);
  });

  const phaseKeys = Object.keys(byPhase).sort((a, b) => Number(a) - Number(b));
  let html = '<div class="section-header">Jobs</div>';

  for (const phaseIdx of phaseKeys) {
    const phaseGoal =
      phases[phaseIdx]?.goal || `Phase ${Number(phaseIdx) + 1}`;
    if (phaseKeys.length > 1) {
      html += `<div class="subsection-header">${escapeHtml(phaseGoal)}</div>`;
    }
    for (const j of byPhase[phaseIdx]) {
      const dur = j.started_at
        ? duration(j.started_at, j.finished_at)
        : "";
      html += `<div class="list-item" data-dispatch-id="${j.dispatch_id}">
        <div class="item-main">
          ${statusBadge(j.status)}
          <span class="item-title">${j.job_type || "execute"}</span>
          ${dur ? `<span class="elapsed" data-started-at="${j.started_at}" data-finished-at="${j.finished_at || ""}">${dur}</span>` : ""}
        </div>
        <div class="item-detail">
          <span class="item-meta">${j.dispatch_id}</span>
        </div>
        <span class="chevron">\u203a</span>
      </div>`;
    }
  }

  return html;
}

function bindJobClicks(root) {
  root.querySelectorAll("[data-dispatch-id]").forEach((el) => {
    el.addEventListener("click", () =>
      pushView({ type: "job", dispatchId: el.dataset.dispatchId })
    );
  });
}

async function renderLifecycleView(lifecycleId) {
  const container = document.getElementById("view-container");
  const lc = state.lifecycles.find((l) => l.lifecycle_id === lifecycleId);

  let html = '<div class="context-card">';
  if (lc) {
    html += '<div class="context-meta" id="lifecycle-meta">';
    html += buildLifecycleMetaHTML(lc);
    html += "</div>";
  }
  html +=
    '<div id="lifecycle-detail-content"><div class="empty-state">Loading...</div></div>';
  html += "</div>";
  html += `<div id="job-list-section">${buildJobListHTML(lifecycleId)}</div>`;

  container.innerHTML = html;
  bindJobClicks(container);
  bindLifecycleActions(container);

  try {
    const data = await fetchJSON(
      `${BASE}/api/lifecycles/${lifecycleId}/context`
    );
    const detailEl = document.getElementById("lifecycle-detail-content");
    if (
      !detailEl ||
      currentView().type !== "lifecycle" ||
      currentView().lifecycleId !== lifecycleId
    )
      return;
    if (data.context && typeof data.context === "object") {
      detailEl.innerHTML = renderLifecycleDetailSections(data.context);
    } else if (data.content) {
      detailEl.innerHTML = `<pre class="preview-text">${escapeHtml(data.content)}</pre>`;
    } else {
      detailEl.innerHTML = "";
    }
  } catch {
    const detailEl = document.getElementById("lifecycle-detail-content");
    if (detailEl) detailEl.innerHTML = "";
  }
}

function buildLifecycleMetaHTML(lc) {
  let html = `<span class="meta-item">${statusBadge(lc.status)}</span>`;
  const phases = lc.phases || [];
  const currentPhase = lc.current_phase || 0;
  if (phases.length > 0) {
    html += `<span class="meta-item">Phase ${currentPhase + 1}/${phases.length}</span>`;
  }
  if (lc.suspend_reason) {
    html += `<span class="meta-item suspend">${lc.suspend_reason}</span>`;
  }
  if (lc.status === "suspend" && lc.suspend_reason === "approval_required") {
    html += `<button class="action-btn primary" data-action="approve" data-lifecycle-id="${lc.lifecycle_id}">Approve</button>`;
  }
  if (lc.status === "suspend") {
    html += `<button class="action-btn primary" data-action="open-session" data-lifecycle-id="${lc.lifecycle_id}">Open</button>`;
  }
  if (lc.status === "suspend" && lc.suspend_reason !== "approval_required") {
    html += `<button class="action-btn" data-action="resume" data-lifecycle-id="${lc.lifecycle_id}">Resume</button>`;
  }
  return html;
}

function bindLifecycleActions(root) {
  root.querySelectorAll("[data-action=approve]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/lifecycles/${btn.dataset.lifecycleId}/approve`, "Approving...");
    });
  });
  root.querySelectorAll("[data-action=open-session]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/lifecycles/${btn.dataset.lifecycleId}/open-session`, "Opening...");
    });
  });
  root.querySelectorAll("[data-action=resume]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/lifecycles/${btn.dataset.lifecycleId}/approve`, "Resuming...");
    });
  });
}

function updateLifecycleSummary(lifecycleId) {
  const meta = document.getElementById("lifecycle-meta");
  if (!meta) return;
  const lc = state.lifecycles.find((l) => l.lifecycle_id === lifecycleId);
  if (!lc) return;
  meta.innerHTML = buildLifecycleMetaHTML(lc);
  bindLifecycleActions(meta);
}

function updateJobList(lifecycleId) {
  const section = document.getElementById("job-list-section");
  if (!section) return;
  section.innerHTML = buildJobListHTML(lifecycleId);
  bindJobClicks(section);
}

function renderLifecycleDetailSections(ctx) {
  let html = "";

  if (ctx.description) {
    html += `<div class="detail-section"><div class="detail-body">${escapeHtml(ctx.description)}</div></div>`;
  }

  if (ctx.phases?.length > 0) {
    html +=
      '<div class="detail-section"><div class="detail-heading">フェーズ計画</div><div class="phase-list">';
    ctx.phases.forEach((p, i) => {
      const status = p.status || "pending";
      html += `<div class="phase-item phase-${status}">
        <span class="phase-num">${i + 1}</span>
        <span class="phase-goal">${escapeHtml(p.goal || "")}</span>
        ${statusBadge(status)}
        ${p.summary ? `<div class="phase-summary">${escapeHtml(p.summary)}</div>` : ""}
      </div>`;
    });
    html += "</div></div>";
  }

  if (ctx.acceptance_criteria?.length > 0) {
    html += `<div class="detail-section"><div class="detail-heading">達成条件</div><ul class="detail-list">`;
    ctx.acceptance_criteria.forEach((c) => {
      html += `<li>${escapeHtml(c)}</li>`;
    });
    html += "</ul></div>";
  }

  if (ctx.open_questions?.length > 0) {
    html += `<div class="detail-section"><div class="detail-heading">未決事項</div><ul class="detail-list">`;
    ctx.open_questions.forEach((q) => {
      html += `<li>${escapeHtml(q)}</li>`;
    });
    html += "</ul></div>";
  }

  return html;
}

// --- Job View (Screen 4): job log + result ---

async function renderJobView(dispatchId) {
  const container = document.getElementById("view-container");
  container.innerHTML = '<div class="empty-state">Loading...</div>';

  try {
    const [logData, resultData] = await Promise.all([
      fetchJSON(`${BASE}/api/jobs/${dispatchId}/log`),
      fetchJSON(`${BASE}/api/jobs/${dispatchId}/result`).catch(() => null),
    ]);

    if (
      currentView().type !== "job" ||
      currentView().dispatchId !== dispatchId
    )
      return;

    let html = "";
    if (resultData && (resultData.verdict || resultData.next_status)) {
      const label = resultData.verdict ? "Verdict" : "Status";
      const value = resultData.verdict || resultData.next_status;
      html += `<div class="result-card">
        <span class="result-label">${label}:</span> ${statusBadge(value)}
        ${resultData.summary ? `<div class="result-summary">${escapeHtml(resultData.summary)}</div>` : ""}
        ${resultData.phase_summary ? `<div class="result-summary">${escapeHtml(resultData.phase_summary)}</div>` : ""}
      </div>`;
    }
    html += `<pre class="preview-text">${escapeHtml((logData.lines || []).join("\n") || "No log")}</pre>`;
    container.innerHTML = html;

    const job = state.jobs.find((j) => j.dispatch_id === dispatchId);
    if (job && job.status === "running") {
      startLogRefresh(dispatchId);
    }
  } catch {
    container.innerHTML = '<div class="empty-state">Failed to load</div>';
  }
}

function startLogRefresh(dispatchId) {
  clearLogRefresh();
  logRefreshTimer = setInterval(async () => {
    if (
      currentView().type !== "job" ||
      currentView().dispatchId !== dispatchId
    ) {
      clearLogRefresh();
      return;
    }
    try {
      const logData = await fetchJSON(
        `${BASE}/api/jobs/${dispatchId}/log`
      );
      const preEl = document.querySelector(".preview-text");
      if (preEl) {
        preEl.textContent =
          (logData.lines || []).join("\n") || "No log";
      }
    } catch {
      /* ignore */
    }
  }, 5000);
}

function clearLogRefresh() {
  if (logRefreshTimer) {
    clearInterval(logRefreshTimer);
    logRefreshTimer = null;
  }
}

// --- Elapsed time updater ---

function updateElapsedTimes() {
  document.querySelectorAll("[data-started-at]").forEach((el) => {
    const startedAt = el.dataset.startedAt;
    const finishedAt = el.dataset.finishedAt || null;
    el.textContent = duration(startedAt, finishedAt);
  });
}

// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("nav-back").addEventListener("click", popView);

  const syncBtn = document.getElementById("sync-btn");
  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    syncBtn.classList.add("spinning");
    try {
      const result = await postAction(`${BASE}/api/sync`);
      if (result.ok) {
        showNotification("Sync started");
      } else {
        showNotification(result.error || "Sync failed", true);
      }
    } catch {
      showNotification("通信エラー", true);
    }
    // sync 完了は SSE tasks_updated で通知されるため、少し待ってからボタンを復帰
    setTimeout(() => {
      syncBtn.disabled = false;
      syncBtn.classList.remove("spinning");
    }, 5000);
  });

  setInterval(updateElapsedTimes, 5000);

  loadAll();
  connectSSE();
});
