async function jsonFetch(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let body;
  try { body = await res.json(); } catch { body = { error: "non-JSON response" }; }
  return { ok: res.ok, status: res.status, body };
}

function setResult(el, ok, body) {
  el.classList.remove("ok", "err");
  el.classList.add(ok ? "ok" : "err");
  el.textContent = JSON.stringify(body, null, 2);
}

async function refreshStatus() {
  const bar = document.getElementById("status-bar");
  const health = await jsonFetch("/api/health");
  if (!health.ok) {
    bar.className = "err";
    bar.textContent = `Connection error: ${health.body.error || health.status}`;
    return;
  }
  const st = await jsonFetch("/api/status");
  if (!st.ok) {
    bar.className = "err";
    bar.textContent = `Status error: ${st.body.error}`;
    return;
  }
  bar.className = "ok";
  if (!st.body.exists) {
    bar.textContent = `Connected. Collection does not exist yet — apply a validator to create it.`;
  } else {
    const v = st.body.validator?.$jsonSchema;
    const tag = v?.properties?.schema_version?.pattern || "(none)";
    const active = st.body.activeVersion || "(none)";
    const mode = st.body.serverEnforced ? "server-enforced ($jsonSchema)"
                                        : "client-enforced (fallback in Flask app)";
    bar.textContent =
      `Connected. count=${st.body.count}, ` +
      `activeVersion=${active}, mode=${mode}, ` +
      `validationLevel=${st.body.validationLevel ?? "n/a"}, ` +
      `validationAction=${st.body.validationAction ?? "n/a"}, ` +
      `server schema_version pattern=${tag}`;
  }
}

document.getElementById("apply-btn").addEventListener("click", async () => {
  const out = document.getElementById("apply-result");
  out.textContent = "Applying…";
  const payload = {
    version: document.getElementById("version").value,
    validationLevel: document.getElementById("level").value,
    validationAction: document.getElementById("action").value,
  };
  const r = await jsonFetch("/api/apply-validator", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  setResult(out, r.ok, r.body);
  refreshStatus();
});

document.getElementById("status-btn").addEventListener("click", refreshStatus);

document.getElementById("drop-btn").addEventListener("click", async () => {
  if (!confirm("Drop the collection? This deletes ALL documents.")) return;
  const out = document.getElementById("apply-result");
  const r = await jsonFetch("/api/drop", { method: "POST" });
  setResult(out, r.ok, r.body);
  refreshStatus();
});

document.getElementById("insert-btn").addEventListener("click", async () => {
  const out = document.getElementById("insert-result");
  out.textContent = "Inserting…";
  let parsed;
  try { parsed = JSON.parse(document.getElementById("doc").value); }
  catch (e) { setResult(out, false, { error: `Invalid JSON: ${e.message}` }); return; }
  const r = await jsonFetch("/api/insert", {
    method: "POST",
    body: JSON.stringify({ document: parsed }),
  });
  setResult(out, r.ok, r.body);
});

document.getElementById("sample-missing-channel").addEventListener("click", () => {
  document.getElementById("doc").value = JSON.stringify({
    request_id: "test-missing-channel",
    request_status: "COMPLETED",
    event: "XYZ",
    schema_version: "v2",
    request_received_date_time: { "$date": new Date().toISOString() },
  }, null, 2);
});

document.getElementById("sample-missing-id").addEventListener("click", () => {
  document.getElementById("doc").value = JSON.stringify({
    request_status: "COMPLETED",
    event: "XYZ",
    channel: "ABC",
    schema_version: "v2",
    request_received_date_time: { "$date": new Date().toISOString() },
  }, null, 2);
});

document.getElementById("recent-btn").addEventListener("click", async () => {
  const out = document.getElementById("recent-result");
  out.textContent = "Loading…";
  const r = await jsonFetch("/api/recent");
  setResult(out, r.ok, r.body);
});

refreshStatus();

// ---- Chat panel ----

const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const chatSend = document.getElementById("chat-send");
const chatClear = document.getElementById("chat-clear");
const chatHistory = [];

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderMarkdownLite(text) {
  const parts = [];
  let last = 0;
  const re = /```(\w+)?\n([\s\S]*?)```/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    parts.push(escapeHtml(text.slice(last, m.index)));
    parts.push(`<pre><code>${escapeHtml(m[2])}</code></pre>`);
    last = m.index + m[0].length;
  }
  parts.push(escapeHtml(text.slice(last)));
  return parts.join("").replace(/`([^`]+)`/g, "<code>$1</code>");
}

function appendMsg(role, text) {
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  if (role === "assistant") {
    div.innerHTML = renderMarkdownLite(text);
  } else {
    div.textContent = text;
  }
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
  return div;
}

appendMsg("assistant",
  "Hi! Ask me about anything on this page — applying validators, validationLevel/Action, " +
  "or say e.g. \"generate a $jsonSchema validator that requires 'orderId' (string) and 'amount' (int >= 0)\".");

chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;
  appendMsg("user", text);
  chatHistory.push({ role: "user", content: text });
  chatInput.value = "";
  chatSend.disabled = true;
  const pending = appendMsg("assistant", "…");
  try {
    const r = await jsonFetch("/api/chat", {
      method: "POST",
      body: JSON.stringify({ messages: chatHistory }),
    });
    if (r.ok && r.body.ok) {
      pending.innerHTML = renderMarkdownLite(r.body.reply || "(empty reply)");
      chatHistory.push({ role: "assistant", content: r.body.reply || "" });
    } else {
      pending.className = "chat-msg error";
      pending.textContent = `Error: ${r.body.error || r.status}`;
    }
  } catch (err) {
    pending.className = "chat-msg error";
    pending.textContent = `Error: ${err.message}`;
  } finally {
    chatSend.disabled = false;
    chatLog.scrollTop = chatLog.scrollHeight;
    chatInput.focus();
  }
});

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.requestSubmit();
  }
});

chatClear.addEventListener("click", () => {
  chatHistory.length = 0;
  chatLog.innerHTML = "";
  appendMsg("assistant", "Cleared. Ask me anything about this page or request a validator.");
});
