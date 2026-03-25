export function statusBadge(status) {
  return `<span class="status status-${status || "unknown"}">${status || "-"}</span>`;
}

export function taskDisplayStatus(task) {
  return task.status;
}

export function duration(startedAt, finishedAt) {
  if (!startedAt) return "";
  const start = new Date(startedAt).getTime();
  const end = finishedAt ? new Date(finishedAt).getTime() : Date.now();
  const diff = Math.floor((end - start) / 1000);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m${diff % 60}s`;
  return `${Math.floor(diff / 3600)}h${Math.floor((diff % 3600) / 60)}m`;
}

export function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

export function updateElapsedTimes() {
  document.querySelectorAll("[data-started-at]").forEach((el) => {
    const startedAt = el.dataset.startedAt;
    const finishedAt = el.dataset.finishedAt || null;
    el.textContent = duration(startedAt, finishedAt);
  });
}
