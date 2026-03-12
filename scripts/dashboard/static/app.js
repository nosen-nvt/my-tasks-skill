/* my-tasks dashboard */

const BASE = window.__BASE_PATH__ || "";

const state = {
  projects: [],
  datasources: [],
  tasks: [],
  lifecycles: [],
  jobs: [],
  selectedProject: null,
  activeTab: "tasks",
};

// --- Fetch helpers ---

async function fetchJSON(url) {
  const res = await fetch(url);
  return res.ok ? res.json() : [];
}

async function loadAll() {
  const [projects, datasources, tasks, lifecycles, jobs] = await Promise.all([
    fetchJSON(`${BASE}/api/projects`),
    fetchJSON(`${BASE}/api/datasources`),
    fetchJSON(`${BASE}/api/tasks`),
    fetchJSON(`${BASE}/api/lifecycles`),
    fetchJSON(`${BASE}/api/jobs`),
  ]);
  state.projects = projects;
  state.datasources = datasources;
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
    renderTasks();
    renderSidebar();
  });

  es.addEventListener("jobs_updated", async () => {
    state.jobs = await fetchJSON(`${BASE}/api/jobs`);
    renderJobs();
  });

  es.addEventListener("lifecycles_updated", async () => {
    state.lifecycles = await fetchJSON(`${BASE}/api/lifecycles`);
    renderLifecycles();
  });
}

// --- Rendering ---

function statusBadge(status) {
  const cls = `status status-${status || "unknown"}`;
  return `<span class="${cls}">${status || "-"}</span>`;
}

function elapsed(startedAt) {
  if (!startedAt) return "";
  const start = new Date(startedAt).getTime();
  const diff = Math.floor((Date.now() - start) / 1000);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m${diff % 60}s`;
  return `${Math.floor(diff / 3600)}h${Math.floor((diff % 3600) / 60)}m`;
}

function renderSidebar() {
  const projectList = document.getElementById("project-list");
  const dsList = document.getElementById("datasource-list");

  // Projects
  const projectIds = new Set();
  state.tasks.forEach((t) => {
    const pid = t.project_id || t.project || "";
    if (pid) projectIds.add(pid);
  });
  state.projects.forEach((p) => {
    const pid = p.id || p._filename || "";
    if (pid) projectIds.add(pid);
  });

  const allItem = `<li class="${!state.selectedProject ? "active" : ""}" data-project="">All (${state.tasks.length})</li>`;
  const items = [...projectIds]
    .sort()
    .map((pid) => {
      const count = state.tasks.filter((t) => (t.project_id || t.project || "") === pid).length;
      const active = state.selectedProject === pid ? "active" : "";
      return `<li class="${active}" data-project="${pid}">${pid} <span class="badge">${count}</span></li>`;
    });
  projectList.innerHTML = allItem + items.join("");

  projectList.querySelectorAll("li").forEach((li) => {
    li.addEventListener("click", () => {
      state.selectedProject = li.dataset.project || null;
      renderAll();
    });
  });

  // Datasources
  dsList.innerHTML = state.datasources
    .map((ds) => `<li>${ds.id || ds._filename || ""}</li>`)
    .join("");
}

function filteredTasks() {
  if (!state.selectedProject) return state.tasks;
  return state.tasks.filter((t) => (t.project_id || t.project || "") === state.selectedProject);
}

function filteredLifecycles() {
  if (!state.selectedProject) return state.lifecycles;
  return state.lifecycles.filter((lc) => lc.project_id === state.selectedProject);
}

function filteredJobs() {
  if (!state.selectedProject) return state.jobs;
  return state.jobs.filter((j) => j.project_id === state.selectedProject);
}

function renderTasks() {
  const container = document.getElementById("tasks-container");
  const tasks = filteredTasks();

  if (!tasks.length) {
    container.innerHTML = '<div class="empty-state">No tasks</div>';
    return;
  }

  // Table (desktop)
  const thead = `<tr><th>ID</th><th>Title</th><th>Status</th><th>Project</th><th>Source</th></tr>`;
  const tbody = tasks
    .map(
      (t) =>
        `<tr class="clickable" data-task-id="${t.id}">
      <td>${t.id}</td>
      <td>${escapeHtml(t.title || "")}</td>
      <td>${statusBadge(t.status)}</td>
      <td>${t.project_id || t.project || ""}</td>
      <td>${t.datasource_id || ""}</td>
    </tr>`
    )
    .join("");

  // Cards (mobile)
  const cards = tasks
    .map(
      (t) =>
        `<div class="card" data-task-id="${t.id}">
      <div class="card-title">${escapeHtml(t.title || "")}</div>
      <div class="card-meta">
        ${statusBadge(t.status)}
        <span>${t.project_id || t.project || ""}</span>
        <span>${t.id}</span>
      </div>
    </div>`
    )
    .join("");

  container.innerHTML = `<table>${thead}${tbody}</table><div class="card-list">${cards}</div>`;

  container.querySelectorAll("[data-task-id]").forEach((el) => {
    el.addEventListener("click", () => showTaskDetail(el.dataset.taskId));
  });
}

function renderLifecycles() {
  const container = document.getElementById("lifecycles-container");
  const lcs = filteredLifecycles();

  if (!lcs.length) {
    container.innerHTML = '<div class="empty-state">No lifecycles</div>';
    return;
  }

  const thead = `<tr><th>ID</th><th>Project</th><th>Status</th><th>Runs</th><th>Dispatch</th><th>Updated</th></tr>`;
  const tbody = lcs
    .map(
      (lc) =>
        `<tr class="clickable" data-lifecycle-id="${lc.lifecycle_id}">
      <td>${lc.lifecycle_id}</td>
      <td>${lc.project_id}</td>
      <td>${statusBadge(lc.status)}${lc.suspend_reason ? ` <span class="elapsed">(${lc.suspend_reason})</span>` : ""}</td>
      <td>${lc.run_count || 0}/${lc.max_runs || 5}</td>
      <td>${lc.current_dispatch_id || "-"}</td>
      <td>${lc.updated_at || ""}</td>
    </tr>`
    )
    .join("");

  const cards = lcs
    .map(
      (lc) =>
        `<div class="card" data-lifecycle-id="${lc.lifecycle_id}">
      <div class="card-title">${lc.lifecycle_id} - ${lc.project_id}</div>
      <div class="card-meta">
        ${statusBadge(lc.status)}
        <span>Runs: ${lc.run_count || 0}/${lc.max_runs || 5}</span>
        ${lc.suspend_reason ? `<span>(${lc.suspend_reason})</span>` : ""}
      </div>
    </div>`
    )
    .join("");

  container.innerHTML = `<table>${thead}${tbody}</table><div class="card-list">${cards}</div>`;

  container.querySelectorAll("[data-lifecycle-id]").forEach((el) => {
    el.addEventListener("click", () => showLifecycleContext(el.dataset.lifecycleId));
  });
}

function renderJobs() {
  const container = document.getElementById("jobs-container");
  const jobs = filteredJobs();

  if (!jobs.length) {
    container.innerHTML = '<div class="empty-state">No jobs</div>';
    return;
  }

  const thead = `<tr><th>Dispatch ID</th><th>Type</th><th>Lifecycle</th><th>Run</th><th>Status</th><th>PID</th><th>Started</th><th>Elapsed</th></tr>`;
  const tbody = jobs
    .map(
      (j) =>
        `<tr class="clickable" data-dispatch-id="${j.dispatch_id}">
      <td>${j.dispatch_id}</td>
      <td>${j.job_type || "execute"}</td>
      <td>${j.lifecycle_id || "-"}</td>
      <td>${j.run != null ? j.run : "-"}</td>
      <td>${statusBadge(j.status)}</td>
      <td>${j.pid || "-"}</td>
      <td>${j.started_at || "-"}</td>
      <td class="elapsed-cell">${j.status === "running" ? elapsed(j.started_at) : (j.finished_at ? elapsed(j.started_at) : "-")}</td>
    </tr>`
    )
    .join("");

  const cards = jobs
    .map(
      (j) =>
        `<div class="card" data-dispatch-id="${j.dispatch_id}">
      <div class="card-title">${j.dispatch_id}</div>
      <div class="card-meta">
        ${statusBadge(j.status)}
        <span>${j.job_type || "execute"}</span>
        ${j.status === "running" ? `<span class="elapsed">${elapsed(j.started_at)}</span>` : ""}
      </div>
    </div>`
    )
    .join("");

  container.innerHTML = `<table>${thead}${tbody}</table><div class="card-list">${cards}</div>`;

  container.querySelectorAll("[data-dispatch-id]").forEach((el) => {
    el.addEventListener("click", () => showJobLog(el.dataset.dispatchId));
  });
}

function renderAll() {
  renderSidebar();
  renderTasks();
  renderLifecycles();
  renderJobs();
}

// --- Detail panel ---

async function showTaskDetail(taskId) {
  const panel = document.getElementById("detail-panel");
  const overlay = document.getElementById("detail-overlay");
  const title = document.getElementById("detail-title");
  const content = document.getElementById("detail-content");

  title.textContent = `Task: ${taskId}`;
  content.textContent = "Loading...";
  panel.classList.remove("hidden");
  overlay.classList.remove("hidden");

  try {
    const data = await fetchJSON(`${BASE}/api/tasks/${taskId}`);
    const task = state.tasks.find((t) => t.id === taskId);
    const generation = task ? (task.generation || 1) : 1;
    const lifecycleId = `${taskId}-g${generation}`;

    let html = `<pre class="task-content">${escapeHtml(data.content || "No content")}</pre>`;

    // Find matching lifecycles and jobs
    const matchedLc = state.lifecycles.find((lc) => lc.lifecycle_id === lifecycleId);
    const matchedJobs = state.jobs.filter((j) => j.lifecycle_id === lifecycleId);

    if (matchedLc || matchedJobs.length) {
      html += `<div class="lifecycle-tree">`;
      html += `<h4>Lifecycle: ${escapeHtml(lifecycleId)}</h4>`;

      if (matchedLc) {
        html += `<div class="lc-status">Status: ${statusBadge(matchedLc.status)} Runs: ${matchedLc.run_count || 0}/${matchedLc.max_runs || 5}</div>`;
      }

      if (matchedJobs.length) {
        // Group jobs by run
        const byRun = {};
        matchedJobs.forEach((j) => {
          const run = j.run != null ? j.run : "-";
          if (!byRun[run]) byRun[run] = [];
          byRun[run].push(j);
        });

        const runKeys = Object.keys(byRun).sort((a, b) => {
          if (a === "-") return -1;
          if (b === "-") return 1;
          return Number(a) - Number(b);
        });

        for (const run of runKeys) {
          html += `<div class="run-group">`;
          html += `<div class="run-header">Run ${run}</div>`;
          for (const j of byRun[run]) {
            html += `<div class="run-job clickable" data-dispatch-id="${j.dispatch_id}">`;
            html += `<span class="run-job-type">${j.job_type || "execute"}</span> `;
            html += `${statusBadge(j.status)} `;
            html += `<span class="run-job-id">${j.dispatch_id}</span>`;
            if (j.finished_at) html += ` <span class="elapsed">${elapsed(j.started_at)}</span>`;
            html += `</div>`;
          }
          html += `</div>`;
        }
      }

      html += `</div>`;
    }

    content.innerHTML = html;

    // Attach click handlers for job log
    content.querySelectorAll("[data-dispatch-id]").forEach((el) => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        showJobLog(el.dataset.dispatchId);
      });
    });
  } catch {
    content.textContent = "Failed to load";
  }
}

async function showLifecycleContext(lifecycleId) {
  const panel = document.getElementById("detail-panel");
  const overlay = document.getElementById("detail-overlay");
  const title = document.getElementById("detail-title");
  const content = document.getElementById("detail-content");

  title.textContent = `Lifecycle: ${lifecycleId}`;
  content.textContent = "Loading...";
  panel.classList.remove("hidden");
  overlay.classList.remove("hidden");

  try {
    const data = await fetchJSON(`${BASE}/api/lifecycles/${lifecycleId}/context`);
    content.innerHTML = `<pre class="task-content">${escapeHtml(data.content || "No content")}</pre>`;
  } catch {
    content.textContent = "Failed to load";
  }
}

async function showJobLog(dispatchId) {
  const panel = document.getElementById("detail-panel");
  const overlay = document.getElementById("detail-overlay");
  const title = document.getElementById("detail-title");
  const content = document.getElementById("detail-content");

  title.textContent = `Job Log: ${dispatchId}`;
  content.textContent = "Loading...";
  panel.classList.remove("hidden");
  overlay.classList.remove("hidden");

  try {
    const data = await fetchJSON(`${BASE}/api/jobs/${dispatchId}/log`);
    content.textContent = (data.lines || []).join("\n") || "No log";
  } catch {
    content.textContent = "Failed to load";
  }
}

function closeDetail() {
  document.getElementById("detail-panel").classList.add("hidden");
  document.getElementById("detail-overlay").classList.add("hidden");
}

// --- Utility ---

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// --- Init ---

document.addEventListener("DOMContentLoaded", () => {
  // Tab navigation (mobile)
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".content-section").forEach((s) => s.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`section-${btn.dataset.tab}`).classList.add("active");
      state.activeTab = btn.dataset.tab;
    });
  });

  // Mobile menu toggle
  document.getElementById("menu-toggle").addEventListener("click", () => {
    document.getElementById("sidebar").classList.toggle("open");
  });

  // Close sidebar on click outside (mobile)
  document.addEventListener("click", (e) => {
    const sidebar = document.getElementById("sidebar");
    const toggle = document.getElementById("menu-toggle");
    if (sidebar.classList.contains("open") && !sidebar.contains(e.target) && e.target !== toggle) {
      sidebar.classList.remove("open");
    }
  });

  // Detail panel close
  document.getElementById("detail-close").addEventListener("click", closeDetail);
  document.getElementById("detail-overlay").addEventListener("click", closeDetail);

  // Elapsed time updater for running jobs
  setInterval(() => {
    document.querySelectorAll(".elapsed-cell").forEach((cell) => {
      const row = cell.closest("tr");
      if (!row) return;
      const dispatchId = row.dataset.dispatchId;
      const job = state.jobs.find((j) => j.dispatch_id === dispatchId);
      if (job && job.status === "running" && job.started_at) {
        cell.textContent = elapsed(job.started_at);
      }
    });
  }, 5000);

  loadAll();
  connectSSE();
});
