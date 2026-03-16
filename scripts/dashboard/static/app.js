import { state, BASE } from "./state.js";
import { fetchJSON } from "./api.js";
import { updateElapsedTimes } from "./utils.js";
import { currentView, setRenderer, popView } from "./navigation.js";
import { postAction, showNotification } from "./actions.js";
import { renderTaskList } from "./components/task-list/task-list.js";
import { renderTaskView, updateTaskSummary, updateLifecycleList } from "./components/task-view/task-view.js";
import { renderLifecycleView, updateLifecycleSummary, updateJobList } from "./components/lifecycle-view/lifecycle-view.js";
import { renderJobView, clearLogRefresh } from "./components/job-view/job-view.js";

// --- Main render (router) ---

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

setRenderer(renderCurrentView);

// --- SSE ---

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

// --- Data loading ---

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

// --- Sync ---

function resetSyncButton() {
  const btn = document.getElementById("sync-btn");
  btn.disabled = false;
  btn.classList.remove("spinning");
}

function pollSyncStatus() {
  const timer = setInterval(async () => {
    try {
      const status = await fetchJSON(`${BASE}/api/sync/status`);
      if (!status.running) {
        clearInterval(timer);
        resetSyncButton();
        if (status.error) {
          showNotification("Sync failed", true);
        } else {
          showNotification("Sync completed");
        }
      }
    } catch {
      clearInterval(timer);
      resetSyncButton();
    }
  }, 1000);
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
        pollSyncStatus();
      } else {
        showNotification(result.error || "Sync failed", true);
        resetSyncButton();
      }
    } catch {
      showNotification("\u901a\u4fe1\u30a8\u30e9\u30fc", true);
      resetSyncButton();
    }
  });

  setInterval(updateElapsedTimes, 5000);

  loadAll();
  connectSSE();
});
