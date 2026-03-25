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

function buildRelatedJobsWarningHTML(taskData) {
  if (!taskData?.related_jobs?.length) return "";
  const runningRelated = taskData.related_jobs.filter((jobId) => {
    const job = state.jobs.find((j) => j.dispatch_id === jobId);
    return job && (job.status === "running" || job.status === "queued");
  });
  if (!runningRelated.length) return "";
  return `<div class="related-jobs-warning">\u26a0 \u30ec\u30d3\u30e5\u30fc\u30a8\u30fc\u30b8\u30a7\u30f3\u30c8\u304c\u4f5c\u696d\u4e2d\u3067\u3059 (${runningRelated.join(", ")})</div>`;
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
  const hasPrUrl = !!taskData?.pr?.url;

  if (status === "pending") {
    if (hasProject)
      html += `<button class="action-btn primary" data-action="plan" data-task-id="${task.id}">Plan</button>`;
    html += `<button class="action-btn danger" data-action="abort" data-task-id="${task.id}">Abort</button>`;
  }

  if (status === "planning") {
    html += `<button class="action-btn" data-action="plan" data-task-id="${task.id}">Plan</button>`;
    if (hasPrompt && hasProject)
      html += `<button class="action-btn primary" data-action="dispatch" data-task-id="${task.id}">Dispatch</button>`;
    html += `<button class="action-btn danger" data-action="abort" data-task-id="${task.id}">Abort</button>`;
  }

  if (status === "executing") {
    html += `<button class="action-btn danger" data-action="abort" data-task-id="${task.id}">Abort</button>`;
  }

  if (status === "in_review") {
    if (hasSession)
      html += `<button class="action-btn primary" data-action="resume" data-task-id="${task.id}">Resume</button>`;
    if (hasPrUrl)
      html += `<button class="action-btn" data-action="feedback" data-task-id="${task.id}">Feedback</button>`;
    html += `<button class="action-btn success" data-action="complete" data-task-id="${task.id}">Done</button>`;
    html += `<button class="action-btn danger" data-action="abort" data-task-id="${task.id}">Abort</button>`;
  }

  return html;
}

function bindTaskActions(root) {
  const actions = {
    plan: { endpoint: "plan", text: "Planning..." },
    dispatch: { endpoint: "dispatch", text: "Dispatching..." },
    resume: { endpoint: "resume", text: "Resuming..." },
    feedback: { endpoint: "feedback", text: "Collecting..." },
    complete: { endpoint: "complete", text: "Completing..." },
    abort: { endpoint: "abort", text: "Aborting..." },
  };

  for (const [action, config] of Object.entries(actions)) {
    root.querySelectorAll(`[data-action="${action}"]`).forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        handleAction(
          btn,
          `${BASE}/api/tasks/${btn.dataset.taskId}/action/${config.endpoint}`,
          config.text
        );
      });
    });
  }
}

function buildTaskMetaHTML(task) {
  let html = "";
  html += `<span class="meta-item" style="font-family:monospace;font-weight:600;">${escapeHtml(task.id)}</span>`;
  html += `<span class="meta-item">${statusBadge(taskDisplayStatus(task))}</span>`;
  if (task.project_id)
    html += `<span class="meta-item">${escapeHtml(task.project_id)}</span>`;
  if (task.datasource_id)
    html += `<span class="meta-item">ds:${escapeHtml(task.datasource_id)}</span>`;
  if (task.remote_id)
    html += `<span class="meta-item">ref:${escapeHtml(task.remote_id)}</span>`;
  return html;
}

function renderTaskDetailSections(data) {
  let html = "";

  // Runtime info
  const infoItems = [];
  if (data.branch) infoItems.push({ label: "Branch", value: data.branch });
  if (data.dispatch_id) infoItems.push({ label: "Dispatch", value: data.dispatch_id });
  if (data.session_id) infoItems.push({ label: "Session", value: data.session_id.substring(0, 8) + "..." });

  if (infoItems.length > 0) {
    html += '<div class="detail-section"><div class="detail-body" style="font-size:12px;">';
    html += infoItems
      .map(
        (item) =>
          `<span style="margin-right:16px;"><span style="color:var(--text-muted);">${item.label}:</span> ${escapeHtml(item.value)}</span>`
      )
      .join("");
    html += "</div></div>";
  }

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

  if (data.pr?.url) {
    html += `<div class="detail-section"><div class="detail-heading">PR</div><div class="detail-body"><a href="${escapeHtml(data.pr.url)}" target="_blank">${escapeHtml(data.pr.url)}</a></div></div>`;
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

    if (data.yaml_error) {
      detailEl.innerHTML = `<div class="related-jobs-warning">\u26a0 YAML \u30d1\u30fc\u30b9\u30a8\u30e9\u30fc: ${escapeHtml(data.yaml_error)}</div><pre class="preview-text">${escapeHtml(data.content)}</pre>`;
    } else if (data.content) {
      detailEl.innerHTML = `<pre class="preview-text">${escapeHtml(data.content)}</pre>`;
    } else {
      detailEl.innerHTML = renderTaskDetailSections(data);
    }

    // Render actions with taskData context
    const actionsEl = document.getElementById("task-actions");
    if (actionsEl && task) {
      let actionsHTML = `<div class="task-actions">${buildTaskActionsHTML(task, data)}</div>`;
      actionsHTML += buildRelatedJobsWarningHTML(data);
      actionsEl.innerHTML = actionsHTML;
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
