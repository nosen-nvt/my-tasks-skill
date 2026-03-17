import { state, BASE } from "../../state.js";
import { fetchJSON } from "../../api.js";
import { statusBadge, taskDisplayStatus, escapeHtml } from "../../utils.js";
import { pushView, currentView } from "../../navigation.js";
import { handleAction } from "../../actions.js";

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
  root.querySelectorAll("[data-action=open-session][data-task-id]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/tasks/${btn.dataset.taskId}/open-session`, "Opening...");
    });
  });
  root.querySelectorAll("[data-action=redispatch]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/tasks/${btn.dataset.taskId}/redispatch`, "Re-dispatching...");
    });
  });
  root.querySelectorAll("[data-action=reopen]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/tasks/${btn.dataset.taskId}/reopen`, "Re-opening...");
    });
  });
}

function buildTaskMetaHTML(task) {
  let html = `<span class="meta-item">${statusBadge(taskDisplayStatus(task))}</span>`;
  if (task.project_id)
    html += `<span class="meta-item">${escapeHtml(task.project_id)}</span>`;
  if (task.generation && task.generation > 1)
    html += `<span class="meta-item">Gen ${task.generation}</span>`;
  if (task.status === "pending" && task.project_id)
    html += `<button class="action-btn primary" data-action="dispatch" data-task-id="${task.id}">Dispatch</button>`;
  if (task.status === "pending")
    html += `<button class="action-btn" data-action="open-session" data-task-id="${task.id}">Open</button>`;
  const displayStatus = taskDisplayStatus(task);
  if (displayStatus === "in_review" && task.project_id)
    html += `<button class="action-btn primary" data-action="redispatch" data-task-id="${task.id}">Re-dispatch</button>`;
  if (displayStatus === "in_review")
    html += `<button class="action-btn" data-action="reopen" data-task-id="${task.id}">Re-open</button>`;
  if (task.status === "done" || task.status === "aborted")
    html += `<button class="action-btn" data-action="redispatch" data-task-id="${task.id}">Re-dispatch</button>`;
  return html;
}

function renderTaskDetailSections(data) {
  let html = "";

  if (data.description) {
    html += `<div class="detail-section"><div class="detail-body">${escapeHtml(data.description)}</div></div>`;
  }

  const sections = [
    { key: "acceptance_criteria", label: "\u9054\u6210\u6761\u4ef6" },
    { key: "preconditions", label: "\u4e8b\u524d\u6761\u4ef6" },
    { key: "open_questions", label: "\u672a\u6c7a\u4e8b\u9805" },
    { key: "completion_actions", label: "\u5b8c\u4e86\u6642\u30a2\u30af\u30b7\u30e7\u30f3" },
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
    html += `<div class="detail-section"><div class="detail-heading">\u5b9f\u884c\u30d7\u30ed\u30f3\u30d7\u30c8</div><pre class="preview-text">${escapeHtml(data.execute_prompt)}</pre></div>`;
  }

  if (data.history?.length > 0) {
    html += '<div class="detail-section"><div class="detail-heading">\u5b9f\u884c\u5c65\u6b74</div><div class="phase-list">';
    data.history.forEach((h) => {
      const label = h.generation != null ? `G${h.generation}` : (h.date || "?");
      const text = h.summary || h.note || "";
      html += `<div class="phase-item"><span class="phase-num">${escapeHtml(label)}</span><span class="phase-goal">${escapeHtml(text)}</span></div>`;
    });
    html += "</div></div>";
  }

  return html;
}

export async function renderTaskView(taskId) {
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

export function updateTaskSummary(taskId) {
  const meta = document.getElementById("task-meta");
  if (!meta) return;
  const task = state.tasks.find((t) => t.id === taskId);
  if (!task) return;
  meta.innerHTML = buildTaskMetaHTML(task);
  bindTaskActions(meta);
  document.getElementById("header-title").textContent =
    task.title || task.id;
}

export function updateLifecycleList(taskId) {
  const section = document.getElementById("lifecycle-list-section");
  if (!section) return;
  section.innerHTML = buildLifecycleListHTML(taskId);
  bindLifecycleClicks(section);
}
