import { state } from "../../state.js";
import { statusBadge, taskDisplayStatus, escapeHtml } from "../../utils.js";
import { pushView } from "../../navigation.js";

export function renderTaskList() {
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
