// Small vanilla-JS frontend. It talks to the FastAPI backend and
// polls /api/jobs once a second to redraw the progress table.

const $ = (sel) => document.querySelector(sel);
const KINDS = ["video", "audio", "image"];

let config = null;
let files = [];               // everything the server found in /input
const selected = new Set();   // relative paths the user ticked

// ---------- helpers ----------

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtTime(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

function esc(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

const presetById = (id) => KINDS.flatMap((k) => config.presets[k]).find((p) => p.id === id);

// ---------- settings ----------

function setupSettings() {
  $("#input-dir").textContent = config.input_dir;
  $("#output-dir").textContent = config.output_dir;

  for (const kind of KINDS) {
    const sel = $(`#${kind}-preset`);
    sel.innerHTML =
      config.presets[kind].map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join("") +
      `<option value="">Don't touch ${kind === "image" ? "images" : kind + " files"}</option>`;
    const saved = localStorage.getItem(`preset-${kind}`);
    if (saved !== null && [...sel.options].some((o) => o.value === saved)) sel.value = saved;
    sel.addEventListener("change", () => { localStorage.setItem(`preset-${kind}`, sel.value); updateHints(); });
  }

  const crf = $("#crf");
  crf.addEventListener("input", () => { $("#crf-value").textContent = crf.value; });
  updateHints();
}

function updateHints() {
  for (const kind of KINDS) {
    const p = presetById($(`#${kind}-preset`).value);
    $(`#${kind}-hint`).textContent = p ? p.description : "Files of this type will be ignored.";
  }
  // only show the CRF slider for presets that use it, and reset it to that preset's default
  const vp = presetById($("#video-preset").value);
  $("#crf-row").hidden = !(vp && vp.uses_crf);
  if (vp && vp.uses_crf) {
    $("#crf").value = vp.default_crf;
    $("#crf-value").textContent = vp.default_crf;
  }
}

// ---------- file list ----------

async function loadFiles() {
  files = await api("/api/files");
  // forget selections for files that disappeared
  const present = new Set(files.map((f) => f.path));
  for (const p of selected) if (!present.has(p)) selected.delete(p);
  renderFiles();
}

function visibleFiles() {
  const q = $("#filter").value.toLowerCase();
  const kind = $("#kind-filter").value;
  return files.filter((f) => (!kind || f.kind === kind) && (!q || f.path.toLowerCase().includes(q)));
}

function renderFiles() {
  const vis = visibleFiles();
  $("#files").innerHTML = vis.map((f) => `
    <tr>
      <td><input type="checkbox" data-path="${esc(f.path)}" ${selected.has(f.path) ? "checked" : ""}></td>
      <td class="path">${esc(f.path)}</td>
      <td class="kind">${f.kind}</td>
      <td class="num">${fmtBytes(f.size)}</td>
    </tr>`).join("");
  $("#files-empty").hidden = files.length > 0;
  $("#select-all").checked = vis.length > 0 && vis.every((f) => selected.has(f.path));
  updateSelectionInfo();
}

function updateSelectionInfo() {
  const sel = files.filter((f) => selected.has(f.path));
  const size = sel.reduce((a, f) => a + f.size, 0);
  $("#selection-info").textContent = `${sel.length} selected (${fmtBytes(size)})`;
  $("#start").disabled = sel.length === 0;
}

function setupFileEvents() {
  // one listener on the table body instead of one per checkbox ("event delegation")
  $("#files").addEventListener("change", (e) => {
    const path = e.target.dataset.path;
    if (!path) return;
    e.target.checked ? selected.add(path) : selected.delete(path);
    renderFiles();
  });
  $("#select-all").addEventListener("change", (e) => {
    for (const f of visibleFiles()) e.target.checked ? selected.add(f.path) : selected.delete(f.path);
    renderFiles();
  });
  $("#filter").addEventListener("input", renderFiles);
  $("#kind-filter").addEventListener("change", renderFiles);
  $("#refresh").addEventListener("click", loadFiles);

  $("#start").addEventListener("click", async () => {
    const btn = $("#start");
    btn.disabled = true;
    try {
      const vp = presetById($("#video-preset").value);
      const res = await api("/api/jobs", {
        method: "POST",
        body: JSON.stringify({
          files: [...selected],
          video_preset: $("#video-preset").value || null,
          audio_preset: $("#audio-preset").value || null,
          image_preset: $("#image-preset").value || null,
          crf: vp && vp.uses_crf ? Number($("#crf").value) : null,
          skip_existing: $("#skip-existing").checked,
        }),
      });
      if (res.added === 0) alert("Nothing queued — all selected file types are set to \"Don't touch\".");
      selected.clear();
      renderFiles();
      pollJobs();
    } catch (err) {
      alert(`Couldn't start: ${err.message}`);
    } finally {
      updateSelectionInfo();
    }
  });
}

// ---------- jobs / progress ----------

const STATUS_LABEL = {
  queued: "Queued", running: "Running", done: "Done", kept_original: "Kept original",
  skipped: "Skipped", failed: "Failed", cancelled: "Cancelled",
};

function renderJobs({ jobs, summary }) {
  $("#jobs-empty").hidden = jobs.length > 0;

  // newest batch first, but running jobs always on top
  const order = { running: 0, queued: 1 };
  jobs.sort((a, b) => (order[a.status] ?? 2) - (order[b.status] ?? 2) || b.id - a.id);

  $("#jobs").innerHTML = jobs.map((j) => {
    const pct = Math.round(j.progress * 100);
    const finished = !["queued", "running"].includes(j.status);
    let meta = "";
    if (j.status === "running") {
      meta = [`${pct}%`, j.speed && j.speed !== "N/A" ? j.speed : "", j.eta != null ? `~${fmtTime(j.eta)} left` : ""]
        .filter(Boolean).join(" · ");
    } else if (finished && j.started && j.finished) {
      meta = `took ${fmtTime(j.finished - j.started)}`;
    }
    const saving = j.out_size != null && j.in_size
      ? (j.out_size < j.in_size
          ? `<div class="saving">−${Math.round((1 - j.out_size / j.in_size) * 100)}%</div>`
          : `<div class="saving none">no gain</div>`)
      : "";
    const preset = presetById(j.preset);
    const msgClass = j.status === "failed" ? "msg error" : "msg";
    return `
      <tr>
        <td class="path">
          ${esc(j.src)}
          <div class="kind">${esc(preset ? preset.label : j.preset)}${j.crf != null ? ` · CRF ${j.crf}` : ""}</div>
          ${j.message ? `<div class="${msgClass}">${esc(j.message)}</div>` : ""}
        </td>
        <td><span class="badge ${j.status}">${STATUS_LABEL[j.status] || j.status}</span></td>
        <td class="progress-cell">
          <div class="bar ${finished ? "done" : ""}"><div style="width:${pct}%"></div></div>
          <div class="meta">${meta}</div>
        </td>
        <td class="num">${fmtBytes(j.in_size)} → ${fmtBytes(j.out_size)}${saving}</td>
        <td>${finished ? "" : `<button class="icon ghost danger" data-cancel="${j.id}" title="Cancel">✕</button>`}</td>
      </tr>`;
  }).join("");

  const c = summary.counts;
  const overallPct = jobs.length ? Math.round(summary.overall_progress * 100) : 0;
  $("#overall-bar").style.width = `${overallPct}%`;
  $("#overall-bar").parentElement.classList.toggle("done", jobs.length > 0 && !c.running && !c.queued);
  const saved = summary.bytes_in - summary.bytes_out;
  const parts = [
    `<span><strong>${overallPct}%</strong> overall</span>`,
    c.running ? `<span><strong>${c.running}</strong> running</span>` : "",
    c.queued ? `<span><strong>${c.queued}</strong> queued</span>` : "",
    c.done ? `<span><strong>${c.done}</strong> done</span>` : "",
    c.kept_original ? `<span><strong>${c.kept_original}</strong> kept original</span>` : "",
    c.skipped ? `<span><strong>${c.skipped}</strong> skipped</span>` : "",
    c.failed ? `<span><strong>${c.failed}</strong> failed</span>` : "",
    summary.bytes_in
      ? `<span>Saved <strong>${fmtBytes(saved)}</strong> (${Math.round((saved / summary.bytes_in) * 100)}%)</span>`
      : "",
  ];
  $("#stats").innerHTML = parts.filter(Boolean).join("");
}

let pollTimer = null;
async function pollJobs() {
  clearTimeout(pollTimer);
  try {
    renderJobs(await api("/api/jobs"));
  } catch (err) {
    console.warn("poll failed", err);
  }
  pollTimer = setTimeout(pollJobs, document.hidden ? 5000 : 1000);
}

function setupJobEvents() {
  $("#jobs").addEventListener("click", async (e) => {
    const id = e.target.dataset.cancel;
    if (id) { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); pollJobs(); }
  });
  $("#cancel-all").addEventListener("click", async () => {
    if (!confirm("Cancel all running and queued jobs?")) return;
    await api("/api/jobs/cancel-all", { method: "POST" });
    pollJobs();
  });
  $("#clear").addEventListener("click", async () => {
    await api("/api/jobs/clear", { method: "POST" });
    pollJobs();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollJobs(); });
}

// ---------- boot ----------

(async function init() {
  config = await api("/api/config");
  setupSettings();
  setupFileEvents();
  setupJobEvents();
  await loadFiles();
  pollJobs();
})();
