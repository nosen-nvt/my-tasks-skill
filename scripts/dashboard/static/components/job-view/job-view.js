import { state, BASE } from "../../state.js";
import { fetchJSON } from "../../api.js";
import { statusBadge, escapeHtml } from "../../utils.js";
import { currentView } from "../../navigation.js";

let logRefreshTimer = null;

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

export function clearLogRefresh() {
  if (logRefreshTimer) {
    clearInterval(logRefreshTimer);
    logRefreshTimer = null;
  }
}

export async function renderJobView(dispatchId) {
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
