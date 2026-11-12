// Small helper used by all pages to call the backend through the gateway
let lastPod = "";

async function api(path, options = {}) {
  const res = await fetch("/api/" + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  lastPod = res.headers.get("X-Upstream-Pod") || lastPod;
  const pod = document.getElementById("pod");
  if (pod) pod.textContent = lastPod;
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    let msg = data && data.detail ? data.detail : "Something went wrong (" + res.status + ")";
    if (Array.isArray(msg)) msg = msg.map(d => d.msg).join(", ");
    throw new Error(msg);
  }
  return data;
}

const rs = n => "Rs. " + Number(n).toFixed(2);
const timeOf = iso => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; }
}
