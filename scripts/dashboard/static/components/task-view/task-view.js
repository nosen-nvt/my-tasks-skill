import { state, BASE } from "../../state.js";
import { fetchJSON } from "../../api.js";
import { statusBadge, taskDisplayStatus, escapeHtml, duration } from "../../utils.js";
import { pushView, currentView } from "../../navigation.js";
import { handleAction } from "../../actions.js";

function buildJobListHTML(taskData) {
  if (!taskData?.dispatch_id) return "";
  // dispatch_id に紐づくジョブを表示
  const taskJobs = state.jobs.filter(
    (j) => j.dispatch_id === taskData.dispatch_id
  );
  if (!taskJobs.length) return "";

  let html = '<div class="section-header">Current Job</div>';
  for (const job of taskJobs) {
    const dur = duration(job.started_at, job.finished_at);
    html += `<div class="list-item" data-dispatch-id="${job.dispatch_id}">
      <div class="item-main">
        ${statusBadge(job.status)}
        <span class="item-title">${escapeHtml(job.dispatch_id)}</span>
      </div>
      <div class="item-detail">
        ${dur ? `<span class="item-meta" data-started-at="${job.started_at || ""}" data-finished-at="${job.finished_at || ""}">${dur}</span>` : ""}
        ${job.session_id ? `<span class="item-sub">session: ${escapeHtml(job.session_id.substring(0, 8))}...</span>` : ""}
      </div>
      <span class="chevron">\u203a</span>
    </div>`;
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

function buildTaskActionsHTML(task, taskData) {
  let html = "";
  const status = task.status;
  const hasPrompt = !!taskData?.execute_prompt;
  const hasSession = !!taskData?.session_id;
  const hasProject = !!task.project_id;
  const hasPrUrl = !!taskData?.pr_url;

  if (status === "pending") {
    // Plan: always available for pending tasks with a project
    if (hasProject)
      html += `<button class="action-btn" data-action="plan" data-task-id="${task.id}">Plan</button>`;
    // Dispatch: available if execute_prompt exists
    if (hasPrompt && hasProject)
      html += `<button class="action-btn primary" data-action="dispatch" data-task-id="${task.id}">Dispatch</button>`;
    // Open: general interactive session
    html += `<button class="action-btn" data-action="open-session" data-task-id="${task.id}">Open</button>`;
  }

  if (status === "in_progress") {
    // Resume: available if session_id exists
    if (hasSession)
      html += `<button class="action-btn primary" data-action="resume" data-task-id="${task.id}">Resume</button>`;
    // Feedback: available if pr_url exists
    if (hasPrUrl)
      html += `<button class="action-btn" data-action="feedback" data-task-id="${task.id}">Feedback</button>`;
    // Open: general interactive session
    html += `<button class="action-btn" data-action="open-session" data-task-id="${task.id}">Open</button>`;
    // Done
    html += `<button class="action-btn success" data-action="complete" data-task-id="${task.id}">Done</button>`;
    // Abort
    html += `<button class="action-btn danger" data-action="abort" data-task-id="${task.id}">Abort</button>`;
  }

  if (status === "done" || status === "aborted") {
    // Open: can always open a session
    html += `<button class="action-btn" data-action="open-session" data-task-id="${task.id}">Open</button>`;
  }

  return html;
}

function bindTaskActions(root) {
  const actions = {
    plan: { endpoint: "plan", text: "Planning..." },
    dispatch: { endpoint: "dispatch", text: "Dispatching..." },
    resume: { endpoint: "resume", text: "Resuming..." },
    feedback: { endpoint: "feedback", text: "Collecting..." },
    "open-session": { endpoint: "open-session", text: "Opening..." },
    complete: { endpoint: "complete", text: "Completing..." },
    abort: { endpoint: "abort", text: "Aborting..." },
  };

  for (const [action, config] of Object.entries(actions)) {
    root.querySelectorAll(`[data-action="${action}"]`).forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        handleAction(
          btn,
          `${BASE}/api/tasks/${btn.dataset.taskId}/${config.endpoint}`,
          config.text
        );
      });
    });
  }
}

function buildTaskMetaHTML(task) {
  let html = `<span class="meta-item">${statusBadge(taskDisplayStatus(task))}</span>`;
  if (task.project_id)
    html += `<span class="meta-item">${escapeHtml(task.project_id)}</span>`;
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
    { key: "completion_actions", label: "\u5b8c\u4e86\u6642\u30a2\u30af\u30b7\u30e7\u30f3" },
  ];
  for (const { key, label } of sections) {
    if (data[key]?.length > 0) {
      html += `<div class="detail-section"><div class="detail-heading">${label}</div><ul class="detail-list">`;
      data[key].forEach((item) => {
        html += `<li>${escapeHtml(typeof item === "string" ? item : JSON.stringify(item))}</li>`;
      });
      html += "</ul></div>";
    }
  }

  if (data.execute_prompt) {
    html += `<div class="detail-section"><div class="detail-heading">\u5b9f\u884c\u30d7\u30ed\u30f3\u30d7\u30c8</div><pre class="preview-text">${escapeHtml(data.execute_prompt)}</pre></div>`;
  }

  if (data.pr_url) {
    html += `<div class="detail-section"><div class="detail-heading">PR</div><div class="detail-body"><a href="${escapeHtml(data.pr_url)}" target="_blank">${escapeHtml(data.pr_url)}</a></div></div>`;
  }

  // History
  const history = data.history?.length > 0 ? data.history : null;
  if (history) {
    html += '<div class="detail-section"><div class="detail-heading">\u5b9f\u884c\u5c65\u6b74</div><div class="phase-list">';
    history.forEach((h) => {
      const label = h.dispatch_id || "?";
      const exitBadge = h.exit_code != null ? statusBadge(h.exit_code === 0 ? "done" : "failed") : "";
      const dur = duration(h.started_at, h.finished_at);
      html += `<div class="phase-item">
        <span class="phase-num">${escapeHtml(label)}</span>
        ${exitBadge}
        ${dur ? `<span class="item-meta">${dur}</span>` : ""}
        ${h.summary ? `<span class="phase-goal">${escapeHtml(h.summary)}</span>` : ""}
      </div>`;
    });
    html += "</div></div>";
  }

  // Feedback
  const feedback = data.feedback?.length > 0 ? data.feedback : null;
  if (feedback) {
    html += '<div class="detail-section"><div class="detail-heading">\u30d5\u30a3\u30fc\u30c9\u30d0\u30c3\u30af</div>';
    for (const group of feedback) {
      html += `<div class="feedback-group"><div class="feedback-group-header">${escapeHtml(group.collected_at || "")}</div>`;
      const items = group.items || [];
      for (const item of items) {
        html += `<div class="feedback-item">
          <span class="feedback-source">[${escapeHtml(item.source || "")}]</span>
          ${item.author ? `<span class="feedback-author">${escapeHtml(item.author)}</span>` : ""}
          <span class="feedback-body">${escapeHtml(item.body || "")}</span>
        </div>`;
      }
      html += "</div>";
    }
    html += "</div>";
  }

  return html;
}

export async function renderTaskView(taskId) {
  const container = document.getElementById("view-container");
  const task = state.tasks.find((t) => t.id === taskId);

  // First render with loading state
  let html = '<div class="context-card">';
  if (task) {
    html += '<div class="context-meta" id="task-meta">';
    html += buildTaskMetaHTML(task);
    html += "</div>";
    html += '<div id="task-actions"></div>';
  }
  html += '<div id="task-detail-content"><div class="empty-state">Loading...</div></div>';
  html += "</div>";
  html += '<div id="job-list-section"></div>';

  container.innerHTML = html;

  // Load task detail
  try {
    const data = await fetchJSON(`${BASE}/api/tasks/${taskId}`);
    const detailEl = document.getElementById("task-detail-content");
    if (!detailEl || currentView().type !== "task" || currentView().taskId !== taskId) return;

    if (data.content) {
      detailEl.innerHTML = `<pre class="preview-text">${escapeHtml(data.content)}</pre>`;
    } else {
      detailEl.innerHTML = renderTaskDetailSections(data);
    }

    // Render actions with taskData context
    const actionsEl = document.getElementById("task-actions");
    if (actionsEl && task) {
      actionsEl.innerHTML = `<div class="task-actions">${buildTaskActionsHTML(task, data)}</div>`;
      bindTaskActions(actionsEl);
    }

    // Render job list
    const jobSection = document.getElementById("job-list-section");
    if (jobSection) {
      jobSection.innerHTML = buildJobListHTML(data);
      bindJobClicks(jobSection);
    }
  } catch {
    const detailEl = document.getElementById("task-detail-content");
    if (detailEl) detailEl.innerHTML = '<div class="empty-state">Failed to load</div>';
  }
}

export function updateTaskSummary(taskId) {
  const meta = document.getElementById("task-meta");
  if (!meta) return;
  const task = state.tasks.find((t) => t.id === taskId);
  if (!task) return;
  meta.innerHTML = buildTaskMetaHTML(task);
  document.getElementById("header-title").textContent = task.title || task.id;
}
