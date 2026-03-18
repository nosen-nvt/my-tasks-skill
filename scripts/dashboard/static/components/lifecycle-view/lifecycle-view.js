import { state, BASE } from "../../state.js";
import { fetchJSON } from "../../api.js";
import { statusBadge, duration, escapeHtml } from "../../utils.js";
import { pushView, currentView } from "../../navigation.js";
import { handleAction } from "../../actions.js";

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
  // approval_gate: Approve/Reject のみ
  if (lc.status === "approval_gate" || (lc.status === "suspend" && lc.suspend_reason === "approval_required")) {
    html += `<button class="action-btn primary" data-action="approve" data-lifecycle-id="${lc.lifecycle_id}">Approve</button>`;
    html += `<button class="action-btn danger" data-action="reject" data-lifecycle-id="${lc.lifecycle_id}">Reject</button>`;
  }
  // 後方互換: 旧 suspend 状態
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
  root.querySelectorAll("[data-action=reject]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/lifecycles/${btn.dataset.lifecycleId}/reject`, "Rejecting...");
    });
  });
  root.querySelectorAll("[data-action=resume]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      handleAction(btn, `${BASE}/api/lifecycles/${btn.dataset.lifecycleId}/approve`, "Resuming...");
    });
  });
}

function renderLifecycleDetailSections(ctx) {
  let html = "";

  if (ctx.description) {
    html += `<div class="detail-section"><div class="detail-body">${escapeHtml(ctx.description)}</div></div>`;
  }

  if (ctx.phases?.length > 0) {
    html +=
      '<div class="detail-section"><div class="detail-heading">\u30d5\u30a7\u30fc\u30ba\u8a08\u753b</div><div class="phase-list">';
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
    html += `<div class="detail-section"><div class="detail-heading">\u9054\u6210\u6761\u4ef6</div><ul class="detail-list">`;
    ctx.acceptance_criteria.forEach((c) => {
      html += `<li>${escapeHtml(c)}</li>`;
    });
    html += "</ul></div>";
  }

  if (ctx.open_questions?.length > 0) {
    html += `<div class="detail-section"><div class="detail-heading">\u672a\u6c7a\u4e8b\u9805</div><ul class="detail-list">`;
    ctx.open_questions.forEach((q) => {
      html += `<li>${escapeHtml(q)}</li>`;
    });
    html += "</ul></div>";
  }

  return html;
}

export async function renderLifecycleView(lifecycleId) {
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

export function updateLifecycleSummary(lifecycleId) {
  const meta = document.getElementById("lifecycle-meta");
  if (!meta) return;
  const lc = state.lifecycles.find((l) => l.lifecycle_id === lifecycleId);
  if (!lc) return;
  meta.innerHTML = buildLifecycleMetaHTML(lc);
  bindLifecycleActions(meta);
}

export function updateJobList(lifecycleId) {
  const section = document.getElementById("job-list-section");
  if (!section) return;
  section.innerHTML = buildJobListHTML(lifecycleId);
  bindJobClicks(section);
}
