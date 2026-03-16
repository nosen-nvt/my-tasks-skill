export async function postAction(url) {
  const res = await fetch(url, { method: "POST" });
  return res.json();
}

export function showNotification(message, isError = false) {
  const el = document.createElement("div");
  el.className = "notification" + (isError ? " error" : "");
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.classList.add("fade-out"), 2500);
  setTimeout(() => el.remove(), 3000);
}

export async function handleAction(btn, url, loadingText) {
  const origText = btn.textContent;
  btn.disabled = true;
  btn.textContent = loadingText || "...";
  try {
    const result = await postAction(url);
    if (result.ok) {
      showNotification(result.message || "OK");
    } else {
      showNotification(result.error || "\u30a8\u30e9\u30fc", true);
    }
  } catch {
    showNotification("\u901a\u4fe1\u30a8\u30e9\u30fc", true);
  }
  btn.disabled = false;
  btn.textContent = origText;
}
