/* my-tasks dashboard – Finder column view */

const BASE = window.__BASE_PATH__ || "";

const state = {
  tasks: [],
  lifecycles: [],
  jobs: [],
  selectedTaskId: null,
  selectedLifecycleId: null,
  selectedJobId: null,
  mobileColumn: 0,
  mobileColumnHistory: [],
};

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
  renderAll();
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
    renderTasksColumn();
  });

  es.addEventListener("jobs_updated", async () => {
    state.jobs = await fetchJSON(`${BASE}/api/jobs`);
    renderJobsColumn();
  });

  es.addEventListener("lifecycles_updated", async () => {
    state.lifecycles = await fetchJSON(`${BASE}/api/lifecycles`);
    renderLifecyclesColumn();
  });
}

// --- Helpers ---

function statusBadge(status) {
  return `<span class="status status-${status || "unknown"}">${status || "-"}</span>`;
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

// --- Column rendering ---

function renderTasksColumn() {
  const container = document.getElementById("tasks-list");
  const tasks = state.tasks;

  if (!tasks.length) {
    container.innerHTML = '<div class="empty-state">No tasks</div>';
    return;
  }

  // Group by project
  const groups = {};
  tasks.forEach((t) => {
    const pid = t.project_id || t.project || "(none)";
    if (!groups[pid]) groups[pid] = [];
    groups[pid].push(t);
  });

  let html = "";
  for (const pid of Object.keys(groups).sort()) {
    html += `<div class="column-group-header">${escapeHtml(pid)}</div>`;
    for (const t of groups[pid]) {
      const selected = state.selectedTaskId === t.id ? "selected" : "";
      html += `<div class="column-item ${selected}" data-task-id="${t.id}">
        <div class="item-main">
          ${statusBadge(t.status)}
          <span class="item-title">${escapeHtml(t.title || t.id)}</span>
        </div>
        <span class="chevron">\u203a</span>
      </div>`;
    }
  }

  container.innerHTML = html;
  container.querySelectorAll("[data-task-id]").forEach((el) => {
    el.addEventListener("click", () => selectTask(el.dataset.taskId));
  });
}

function renderLifecyclesColumn() {
  const container = document.getElementById("lifecycles-list");

  if (!state.selectedTaskId) {
    container.innerHTML = '<div class="empty-state">Select a task</div>';
    return;
  }

  const prefix = state.selectedTaskId + "-g";
  const taskLifecycles = state.lifecycles.filter(
    (lc) => lc.lifecycle_id && lc.lifecycle_id.startsWith(prefix)
  );

  if (!taskLifecycles.length) {
    container.innerHTML = '<div class="empty-state">No lifecycles</div>';
    return;
  }

  let html = "";
  for (const lc of taskLifecycles) {
    const selected = state.selectedLifecycleId === lc.lifecycle_id ? "selected" : "";
    const genMatch = lc.lifecycle_id.match(/-g(\d+)$/);
    const gen = genMatch ? genMatch[1] : "?";
    const jobCount = state.jobs.filter((j) => j.lifecycle_id === lc.lifecycle_id).length;

    const phases = lc.phases || [];
    const currentPhase = lc.current_phase || 0;
    const phaseInfo = phases.length > 0
      ? `Phase ${currentPhase + 1}/${phases.length}`
      : "No phases";
    const currentGoal = phases[currentPhase]?.goal || "";

    html += `<div class="column-item ${selected}" data-lifecycle-id="${lc.lifecycle_id}">
      <div class="item-main">
        ${statusBadge(lc.status)}
        <span class="item-title">Generation ${gen}</span>
      </div>
      <div class="item-detail">
        <span class="item-meta">${phaseInfo}</span>
        ${currentGoal ? `<span class="item-sub">${escapeHtml(currentGoal)}</span>` : ""}
        ${lc.suspend_reason ? `<span class="item-sub">${lc.suspend_reason}</span>` : ""}
      </div>
      ${jobCount > 0 ? '<span class="chevron">\u203a</span>' : ""}
    </div>`;
  }

  container.innerHTML = html;
  container.querySelectorAll("[data-lifecycle-id]").forEach((el) => {
    el.addEventListener("click", () => selectLifecycle(el.dataset.lifecycleId));
  });
}

function renderJobsColumn() {
  const container = document.getElementById("jobs-list");

  if (!state.selectedLifecycleId) {
    container.innerHTML = '<div class="empty-state">Select a lifecycle</div>';
    return;
  }

  const jobs = state.jobs.filter((j) => j.lifecycle_id === state.selectedLifecycleId);

  if (!jobs.length) {
    container.innerHTML = '<div class="empty-state">No jobs</div>';
    return;
  }

  // Group by phase (run field = phase index)
  const byPhase = {};
  jobs.forEach((j) => {
    const phase = j.run != null ? j.run : 0;
    if (!byPhase[phase]) byPhase[phase] = [];
    byPhase[phase].push(j);
  });

  const selectedLifecycle = state.lifecycles.find(
    (lc) => lc.lifecycle_id === state.selectedLifecycleId
  );
  const phases = selectedLifecycle?.phases || [];

  let html = "";
  const phaseKeys = Object.keys(byPhase).sort((a, b) => Number(a) - Number(b));

  for (const phaseIdx of phaseKeys) {
    const phaseGoal = phases[phaseIdx]?.goal || `Phase ${Number(phaseIdx) + 1}`;
    if (phaseKeys.length > 1) {
      html += `<div class="column-group-header">${escapeHtml(phaseGoal)}</div>`;
    }
    for (const j of byPhase[phaseIdx]) {
      const selected = state.selectedJobId === j.dispatch_id ? "selected" : "";
      const dur = j.started_at ? duration(j.started_at, j.finished_at) : "";
      html += `<div class="column-item ${selected}" data-dispatch-id="${j.dispatch_id}">
        <div class="item-main">
          ${statusBadge(j.status)}
          <span class="item-title">${j.job_type || "execute"}</span>
        </div>
        <div class="item-detail">
          <span class="item-meta">${j.dispatch_id}</span>
          ${dur ? `<span class="elapsed">${dur}</span>` : ""}
        </div>
      </div>`;
    }
  }

  container.innerHTML = html;
  container.querySelectorAll("[data-dispatch-id]").forEach((el) => {
    el.addEventListener("click", () => selectJob(el.dataset.dispatchId));
  });
}

function renderContextPreview(ctx) {
  let html = "";

  // メタ情報
  if (ctx.meta) {
    const meta = ctx.meta;
    const items = [];
    if (meta.task_id) items.push(`Task: ${meta.task_id}`);
    if (meta.remote_id) items.push(`Remote: ${meta.remote_id}`);
    if (meta.project_id) items.push(`Project: ${meta.project_id}`);
    if (meta.generation) items.push(`Generation: ${meta.generation}`);
    if (items.length) {
      html += `<div class="context-meta">${items.map((i) => `<span class="meta-item">${escapeHtml(i)}</span>`).join("")}</div>`;
    }
  }

  // 概要
  if (ctx.description) {
    html += `<div class="context-section"><div class="context-heading">概要</div><div class="context-body">${escapeHtml(ctx.description)}</div></div>`;
  }

  // フェーズ計画
  if (ctx.phases && ctx.phases.length > 0) {
    html += '<div class="context-section"><div class="context-heading">フェーズ計画</div><div class="phase-list">';
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

  // 達成条件
  if (ctx.acceptance_criteria && ctx.acceptance_criteria.length > 0) {
    html += `<div class="context-section"><div class="context-heading">達成条件</div><ul class="context-list">`;
    ctx.acceptance_criteria.forEach((c) => {
      html += `<li>${escapeHtml(c)}</li>`;
    });
    html += "</ul></div>";
  }

  // 未決事項
  if (ctx.open_questions && ctx.open_questions.length > 0) {
    html += `<div class="context-section"><div class="context-heading">未決事項</div><ul class="context-list">`;
    ctx.open_questions.forEach((q) => {
      html += `<li>${escapeHtml(q)}</li>`;
    });
    html += "</ul></div>";
  }

  // 実行プロンプト
  if (ctx.execute_prompt) {
    html += `<div class="context-section"><div class="context-heading">実行プロンプト</div><pre class="preview-text">${escapeHtml(ctx.execute_prompt)}</pre></div>`;
  }

  return html || '<div class="empty-state">No context data</div>';
}

async function renderPreview() {
  const titleEl = document.getElementById("preview-title");
  const contentEl = document.getElementById("preview-content");

  if (state.selectedJobId) {
    titleEl.textContent = `Job: ${state.selectedJobId}`;
    contentEl.innerHTML = '<div class="empty-state">Loading...</div>';
    try {
      const [data, resultData] = await Promise.all([
        fetchJSON(`${BASE}/api/jobs/${state.selectedJobId}/log`),
        fetchJSON(`${BASE}/api/jobs/${state.selectedJobId}/result`).catch(() => null),
      ]);
      let html = "";
      if (resultData && (resultData.verdict || resultData.next_status)) {
        const label = resultData.verdict ? "Verdict" : "Status";
        const value = resultData.verdict || resultData.next_status;
        html += `<div class="preview-result">
          <span class="result-label">${label}:</span> ${statusBadge(value)}
          ${resultData.summary ? `<div class="result-summary">${escapeHtml(resultData.summary)}</div>` : ""}
          ${resultData.phase_summary ? `<div class="result-summary">${escapeHtml(resultData.phase_summary)}</div>` : ""}
        </div>`;
      }
      html += `<pre class="preview-text">${escapeHtml((data.lines || []).join("\n") || "No log")}</pre>`;
      contentEl.innerHTML = html;
    } catch {
      contentEl.innerHTML = '<div class="empty-state">Failed to load</div>';
    }
  } else if (state.selectedLifecycleId) {
    titleEl.textContent = `Lifecycle: ${state.selectedLifecycleId}`;
    contentEl.innerHTML = '<div class="empty-state">Loading...</div>';
    try {
      const data = await fetchJSON(`${BASE}/api/lifecycles/${state.selectedLifecycleId}/context`);
      if (data.context && typeof data.context === "object") {
        contentEl.innerHTML = renderContextPreview(data.context);
      } else {
        contentEl.innerHTML = `<pre class="preview-text">${escapeHtml(data.content || "No context")}</pre>`;
      }
    } catch {
      contentEl.innerHTML = '<div class="empty-state">Failed to load</div>';
    }
  } else if (state.selectedTaskId) {
    titleEl.textContent = `Task: ${state.selectedTaskId}`;
    contentEl.innerHTML = '<div class="empty-state">Loading...</div>';
    try {
      const data = await fetchJSON(`${BASE}/api/tasks/${state.selectedTaskId}`);
      contentEl.innerHTML = `<pre class="preview-text">${escapeHtml(data.content || "No content")}</pre>`;
    } catch {
      contentEl.innerHTML = '<div class="empty-state">Failed to load</div>';
    }
  } else {
    titleEl.textContent = "Preview";
    contentEl.innerHTML = '<div class="empty-state">Select an item</div>';
  }
}

// --- Selection ---

function selectTask(taskId) {
  state.selectedTaskId = taskId;
  state.selectedLifecycleId = null;
  state.selectedJobId = null;

  renderTasksColumn();
  renderLifecyclesColumn();

  // Auto-select if only one lifecycle
  const prefix = taskId + "-g";
  const taskLifecycles = state.lifecycles.filter(
    (lc) => lc.lifecycle_id && lc.lifecycle_id.startsWith(prefix)
  );
  if (taskLifecycles.length === 1) {
    state.selectedLifecycleId = taskLifecycles[0].lifecycle_id;
    renderLifecyclesColumn();
    renderJobsColumn();
  } else {
    renderJobsColumn();
  }

  renderPreview();

  if (window.innerWidth < 768) {
    if (taskLifecycles.length === 0) {
      navigateToColumn(3);
    } else {
      navigateToColumn(1);
    }
  }
}

function selectLifecycle(lifecycleId) {
  state.selectedLifecycleId = lifecycleId;
  state.selectedJobId = null;

  renderLifecyclesColumn();
  renderJobsColumn();
  renderPreview();

  if (window.innerWidth < 768) {
    const lcJobs = state.jobs.filter((j) => j.lifecycle_id === lifecycleId);
    if (lcJobs.length === 0) {
      navigateToColumn(3);
    } else {
      navigateToColumn(2);
    }
  }
}

function selectJob(dispatchId) {
  state.selectedJobId = dispatchId;

  renderJobsColumn();
  renderPreview();

  if (window.innerWidth < 768) {
    navigateToColumn(3);
  }
}

// --- Mobile navigation ---

function navigateToColumn(index) {
  if (state.mobileColumn !== index) {
    state.mobileColumnHistory.push(state.mobileColumn);
  }
  state.mobileColumn = index;
  document.querySelectorAll(".column").forEach((col, i) => {
    col.classList.toggle("mobile-active", i === index);
  });
  document.getElementById("nav-back").classList.toggle("hidden", index === 0);
}

function navigateBack() {
  const prev = state.mobileColumnHistory.pop();
  if (prev != null) {
    state.mobileColumn = prev;
    document.querySelectorAll(".column").forEach((col, i) => {
      col.classList.toggle("mobile-active", i === prev);
    });
    document.getElementById("nav-back").classList.toggle("hidden", prev === 0);
  }
}

// --- Render all ---

function renderAll() {
  renderTasksColumn();
  renderLifecyclesColumn();
  renderJobsColumn();
  renderPreview();
}

// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("nav-back").addEventListener("click", navigateBack);

  // Elapsed time updater for running jobs
  setInterval(() => {
    if (state.selectedLifecycleId) {
      const hasRunning = state.jobs.some(
        (j) => j.lifecycle_id === state.selectedLifecycleId && j.status === "running"
      );
      if (hasRunning) renderJobsColumn();
    }
  }, 5000);

  loadAll();
  connectSSE();
});
