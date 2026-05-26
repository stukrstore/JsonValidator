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
