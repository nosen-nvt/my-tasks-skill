export async function fetchJSON(url) {
  const res = await fetch(url);
  return res.ok ? res.json() : [];
}
