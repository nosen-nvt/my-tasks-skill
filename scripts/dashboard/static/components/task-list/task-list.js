import { state, BASE } from "../../state.js";
import { statusBadge, taskDisplayStatus, escapeHtml } from "../../utils.js";
import { pushView } from "../../navigation.js";
import { handleAction } from "../../actions.js";

function buildRoutineButtonsHTML() {
  if (!state.routines.length) return "";
  let html = '<div class="section-header">Routines</div>';
  html += '<div class="routine-actions">';
  for (const r of state.routines) {
    html += `<button class="action-btn primary routine-btn" data-routine-id="${escapeHtml(r.routine_id)}">${escapeHtml(r.label)}</button>`;
  }
  html += "</div>";
  return html;
}

export function renderTaskList() {
  const container = document.getElementById("view-container");
  const tasks = state.tasks;

  let html = buildRoutineButtonsHTML();

  if (!tasks.length) {
    html += '<div class="empty-state">No tasks</div>';
    container.innerHTML = html;
  } else {
    const groups = {};
    tasks.forEach((t) => {
      const pid = t.project_id || t.project || "(none)";
      if (!groups[pid]) groups[pid] = [];
      groups[pid].push(t);
    });

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

  // ルーチンボタンのクリックハンドラ
  container.querySelectorAll("[data-routine-id]").forEach((btn) => {
    btn.addEventListener("click", () => {
      handleAction(btn, `${BASE}/api/routines/${btn.dataset.routineId}/open-session`, "起動中...");
    });
  });
}
