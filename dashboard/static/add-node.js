/* Add-node form.
 *
 * The browser holds no rules. Every constraint — which PostgreSQL versions are
 * allowed, which Spock majors, where a node may land, what it will be called —
 * comes from /api/add-node/options, and every keystroke is checked by
 * /api/add-node/preview, which is the same code path the CLI uses. The page's
 * job is to make those answers easy to give and impossible to misread.
 */

const form = document.getElementById("add-node-form");
const els = {
  notice: document.getElementById("notice"),
  role: document.getElementById("role"),
  name: document.getElementById("name"),
  nameHint: document.getElementById("name-hint"),
  leader: document.getElementById("leader"),
  source: document.getElementById("source"),
  hosts: document.getElementById("hosts"),
  pgVersion: document.getElementById("pg_version"),
  pgHint: document.getElementById("pg-hint"),
  spock: document.getElementById("spock_major"),
  spockHint: document.getElementById("spock-hint"),
  branchField: document.getElementById("branch-field"),
  branch: document.getElementById("spock_branch"),
  syncMode: document.getElementById("sync_mode"),
  syncExtra: document.querySelector(".sync-extra"),
  syncCount: document.getElementById("sync_count"),
  syncStrict: document.getElementById("sync_strict"),
  previewGrid: document.getElementById("preview-grid"),
  previewMessages: document.getElementById("preview-messages"),
  submit: document.getElementById("submit"),
  submitHint: document.getElementById("submit-hint"),
  job: document.getElementById("job"),
  jobTitle: document.getElementById("job-title"),
  jobSteps: document.getElementById("job-steps"),
  jobLog: document.getElementById("job-log"),
  jobElapsed: document.getElementById("job-elapsed"),
  jobSpinner: document.getElementById("job-spinner"),
  jobFooter: document.getElementById("job-footer"),
};

// The health page links here with a role and a leader already chosen, e.g.
// /add-node?role=standby&leader=n1 from a node that has none.
const params = new URLSearchParams(window.location.search);

const state = {
  cluster: document.body.dataset.cluster || "",
  changesAllowed: document.body.dataset.changes === "yes",
  options: null,
  role: params.get("role") === "standby" ? "standby" : "leader",
  host: null,
  spockMajor: "",
  syncMode: "async",
  jobId: null,
  poll: null,
};

const clusterQuery = () => (state.cluster ? `?cluster=${encodeURIComponent(state.cluster)}` : "");

function say(message, kind = "warn") {
  els.notice.textContent = message;
  els.notice.className = `notice ${kind}`;
  els.notice.hidden = !message;
}

/* ---------------------------------------------------------------- load */

async function load() {
  try {
    const response = await fetch(`/api/add-node/options${clusterQuery()}`);
    const data = await response.json();
    if (!response.ok) { say(data.error || "could not read the cluster"); return; }
    state.options = data;
    render();
    if (data.running_job) followJob(data.running_job.id);
  } catch (error) {
    say(`could not reach the dashboard: ${error}`);
  }
}

function render() {
  const { cluster, nodes, hosts, suggested_name: suggested } = state.options;

  els.name.placeholder = suggested;
  els.nameHint.textContent = `${suggested} is free. Letters, digits and underscores.`;

  const spockNodes = nodes.filter((n) => n.role === "spock");
  fillSelect(els.leader, spockNodes.map((n) => ({
    value: n.name, label: `${n.name} — ${n.address}:${n.pg_port}`,
  })));
  const wantedLeader = params.get("leader");
  if (wantedLeader && spockNodes.some((n) => n.name === wantedLeader)) {
    els.leader.value = wantedLeader;
  }
  fillSelect(els.source, spockNodes.map((n) => ({
    value: n.name, label: `${n.name} — ${n.address}:${n.pg_port}`,
  })));

  renderHosts(hosts);
  renderSpock();

  els.pgVersion.placeholder = cluster.pg_version;
  els.pgHint.innerHTML = pgHintText();

  els.branchField.hidden = cluster.deploy_mode !== "source";
  els.branch.placeholder = cluster.spock_branch;

  if (!state.changesAllowed) {
    els.submitHint.textContent = "read-only dashboard — preview only";
  }
  preview();
}

function pgHintText() {
  const { cluster, pg_versions: versions } = state.options;
  if (cluster.deploy_mode === "source") {
    const known = Object.values(versions || {}).flat();
    return known.length
      ? `Compiled from source. Published: ${known.slice(-6).join(", ")}.`
      : `Compiled from source. The version list could not be fetched — type an exact version.`;
  }
  return `From the ${cluster.repo_channel} channel. The cluster runs ${cluster.pg_version}; ` +
         `a new node may match it or be newer, never older.`;
}

function fillSelect(select, items) {
  select.innerHTML = "";
  items.forEach(({ value, label }) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  });
}

function renderHosts(hosts) {
  els.hosts.innerHTML = "";
  hosts.forEach((host, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "host-card";
    card.setAttribute("role", "radio");
    card.dataset.value = host.name;
    card.innerHTML = `
      <span class="host-name">${host.name}</span>
      <span class="host-addr">${host.address}</span>
      <span class="host-nodes">${
        host.in_cluster
          ? (host.nodes.length ? `runs ${host.nodes.join(", ")}` : "no nodes yet")
          : `${host.username}@ — not in the cluster`
      }</span>
      <span class="tag ${host.in_cluster ? "" : "new"}">${
        host.in_cluster ? "cluster host" : "from inventory"
      }</span>`;
    card.addEventListener("click", () => selectHost(host.name));
    els.hosts.appendChild(card);
    if (index === 0 && !state.host) selectHost(host.name, false);
  });
  markHost();
}

function selectHost(name, refresh = true) {
  state.host = name;
  markHost();
  if (refresh) preview();
}

function markHost() {
  els.hosts.querySelectorAll(".host-card").forEach((card) => {
    const on = card.dataset.value === state.host;
    card.classList.toggle("on", on);
    card.setAttribute("aria-checked", on ? "true" : "false");
  });
}

function renderSpock() {
  const { cluster, spock_majors: majors, spock_branches: branches } = state.options;
  state.spockMajor = state.spockMajor || cluster.spock_major;
  els.spock.innerHTML = "";
  majors.forEach((major) => {
    const button = document.createElement("button");
    button.type = "button";
    button.setAttribute("role", "radio");
    button.dataset.value = major;
    button.innerHTML = `<strong>spock${major}</strong><small>${
      major === cluster.spock_major ? "the cluster's" : `branch ${branches[major]}`
    }</small>`;
    button.addEventListener("click", () => {
      state.spockMajor = major;
      markSpock();
      // The branch follows the major unless it was typed by hand.
      if (!els.branch.dataset.touched) els.branch.value = "";
      els.branch.placeholder = major === cluster.spock_major
        ? cluster.spock_branch : branches[major];
      preview();
    });
    els.spock.appendChild(button);
  });
  markSpock();
  els.spockHint.textContent = majors.length > 1
    ? "A newer Spock may join older peers; an older one may not."
    : `The cluster runs spock${cluster.spock_major}.`;
}

function markSpock() {
  els.spock.querySelectorAll("button").forEach((button) => {
    const on = button.dataset.value === state.spockMajor;
    button.classList.toggle("on", on);
    button.setAttribute("aria-checked", on ? "true" : "false");
  });
}

/* ------------------------------------------------------- segmented controls */

function wireSegmented(group, onPick) {
  group.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-value]");
    if (!button) return;
    group.querySelectorAll("button").forEach((other) => {
      const on = other === button;
      other.classList.toggle("on", on);
      other.setAttribute("aria-checked", on ? "true" : "false");
    });
    onPick(button.dataset.value);
  });
  group.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    const buttons = [...group.querySelectorAll("button")];
    const current = buttons.findIndex((b) => b.classList.contains("on"));
    const next = event.key === "ArrowLeft" || event.key === "ArrowUp"
      ? (current - 1 + buttons.length) % buttons.length
      : (current + 1) % buttons.length;
    buttons[next].click();
    buttons[next].focus();
    event.preventDefault();
  });
}

function applyRole(value) {
  state.role = value;
  document.querySelectorAll("[data-role]").forEach((section) => {
    section.hidden = section.dataset.role !== value;
  });
  // A standby takes its name from the node it follows, so there is nothing
  // to type — the preview shows what it will be called.
  els.name.closest(".labelled").hidden = value === "standby";
}

wireSegmented(els.role, (value) => { applyRole(value); preview(); });

// The template ships both halves of the form; show the one this role needs,
// and mark the control so the two agree before any click.
applyRole(state.role);
els.role.querySelectorAll("button").forEach((button) => {
  const on = button.dataset.value === state.role;
  button.classList.toggle("on", on);
  button.setAttribute("aria-checked", on ? "true" : "false");
});

wireSegmented(els.syncMode, (value) => {
  state.syncMode = value;
  els.syncExtra.hidden = value === "async";
  preview();
});

/* ------------------------------------------------------------- combobox */

function wireCombo(input, itemsFor) {
  const list = document.getElementById(`${input.id}-list`);
  const toggle = input.parentElement.querySelector(".combo-toggle");
  let active = -1;

  const close = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  };

  const open = () => {
    const items = itemsFor(input.value.trim());
    list.innerHTML = "";
    if (!items.length) { close(); return; }
    items.forEach((item) => {
      const li = document.createElement("li");
      if (item.group) {
        li.className = "group";
        li.textContent = item.group;
      } else {
        li.setAttribute("role", "option");
        li.dataset.value = item.value;
        li.innerHTML = `<span>${item.value}</span>${
          item.note ? `<small>${item.note}</small>` : ""}`;
        li.addEventListener("mousedown", (event) => {
          event.preventDefault();
          input.value = item.value;
          input.dataset.touched = "1";
          close();
          preview();
        });
      }
      list.appendChild(li);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
  };

  input.addEventListener("focus", open);
  input.addEventListener("input", () => { input.dataset.touched = "1"; open(); preview(); });
  input.addEventListener("blur", () => setTimeout(close, 120));
  toggle.addEventListener("click", () => {
    if (list.hidden) { input.focus(); open(); } else close();
  });
  input.addEventListener("keydown", (event) => {
    const options = [...list.querySelectorAll("li[role='option']")];
    if (event.key === "Escape") { close(); return; }
    if (!options.length) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      active = event.key === "ArrowDown"
        ? Math.min(active + 1, options.length - 1)
        : Math.max(active - 1, 0);
      options.forEach((li, index) => li.classList.toggle("active", index === active));
      options[active].scrollIntoView({ block: "nearest" });
      event.preventDefault();
    } else if (event.key === "Enter" && active >= 0) {
      input.value = options[active].dataset.value;
      input.dataset.touched = "1";
      close();
      preview();
      event.preventDefault();
    }
  });
}

wireCombo(els.pgVersion, (typed) => {
  if (!state.options) return [];
  const { cluster, pg_versions: versions, pg_majors: majors } = state.options;
  const items = [];
  if (cluster.deploy_mode === "source") {
    majors.forEach((major) => {
      const published = (versions || {})[major] || [];
      if (!published.length) return;
      items.push({ group: `PostgreSQL ${major}` });
      published.slice().reverse().forEach((version, index) => items.push({
        value: version,
        note: index === 0 ? "newest" : (version.includes("beta") || version.includes("rc")
          ? "pre-release" : ""),
      }));
    });
  } else {
    items.push({ group: `from the ${cluster.repo_channel} channel` });
    majors.forEach((major) => items.push({
      value: major,
      note: major === cluster.pg_major ? "the cluster's major" : "newer major",
    }));
  }
  const needle = typed.toLowerCase();
  return needle
    ? items.filter((item) => item.group || item.value.toLowerCase().includes(needle))
    : items;
});

wireCombo(els.branch, () => {
  if (!state.options) return [];
  const { cluster, spock_branches: branches } = state.options;
  const seen = new Set();
  const items = [{ group: "branches" }];
  [branches[state.spockMajor], cluster.spock_branch, "main"].forEach((branch) => {
    if (branch && !seen.has(branch)) {
      seen.add(branch);
      items.push({
        value: branch,
        note: branch === branches[state.spockMajor] ? `spock${state.spockMajor} default` : "",
      });
    }
  });
  return items;
});

/* --------------------------------------------------------------- stepper */

document.querySelectorAll(".stepper button").forEach((button) => {
  button.addEventListener("click", () => {
    const input = button.parentElement.querySelector("input");
    const next = Number(input.value || 1) + Number(button.dataset.step);
    input.value = Math.min(Number(input.max), Math.max(Number(input.min), next));
    preview();
  });
});
els.syncCount.addEventListener("input", preview);
els.syncStrict.addEventListener("change", preview);
els.name.addEventListener("input", preview);
els.leader.addEventListener("change", preview);
els.source.addEventListener("change", preview);

/* --------------------------------------------------------------- preview */

function formState() {
  const standby = state.role === "standby";
  return {
    cluster: state.cluster,
    role: state.role,
    name: els.name.value.trim(),
    host: state.host,
    leader: standby ? els.leader.value : "",
    source: standby ? "" : els.source.value,
    pg_version: standby ? "" : els.pgVersion.value.trim(),
    spock_major: standby ? "" : (state.spockMajor || ""),
    spock_branch: standby ? "" : els.branch.value.trim(),
    sync_mode: standby ? state.syncMode : "",
    sync_count: standby && state.syncMode !== "async" ? Number(els.syncCount.value) : null,
    sync_strict: standby && state.syncMode !== "async" ? els.syncStrict.checked : false,
  };
}

let previewTimer = null;
function preview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 180);   // debounce typing
}

async function runPreview() {
  if (!state.options) return;
  const body = formState();
  try {
    const response = await fetch("/api/add-node/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) { say(data.error || "preview failed"); return; }
    say("");
    showPreview(data, body);
  } catch (error) {
    say(`preview failed: ${error}`);
  }
}

function showPreview(data, body) {
  const node = data.node;
  const rows = [
    ["Name", node.name],
    ["Role", node.role === "standby" ? `standby of ${body.leader || "—"}` : "Spock node"],
    ["Host", node.host ? `${node.host} (${node.address})${node.host_is_new ? " — new to the cluster" : ""}` : "—"],
    ["PostgreSQL", `${node.pg_port ? `${node.address}:${node.pg_port}` : "—"} · ${node.pg_version}`],
    ["Patroni", `${node.restapi_port ? `${node.address}:${node.restapi_port}` : "—"} · scope ${node.scope}`],
    ["Data", node.data_dir],
  ];
  if (node.role !== "standby") {
    rows.push(["Spock", `spock${node.spock_major}${node.spock_branch ? ` from ${node.spock_branch}` : ""}`]);
    if (node.bin_dir) rows.push(["Binaries", node.bin_dir]);
  } else {
    rows.push(["Replication", node.sync_mode === "async"
      ? "asynchronous"
      : `${node.sync_mode}, ${body.sync_count} standby must confirm${body.sync_strict ? ", writes block when none is available" : ""}`]);
  }
  if (node.builds_from_source) {
    rows.push(["Build", "compiled from source on the host — 20-40 minutes"]);
  }

  els.previewGrid.innerHTML = rows
    .map(([term, value]) => `<dt>${term}</dt><dd>${value}</dd>`)
    .join("");

  els.previewMessages.innerHTML = [
    ...data.errors.map((text) => `<li class="error">${text}</li>`),
    ...data.warnings.map((text) => `<li>${text}</li>`),
  ].join("");

  els.name.setAttribute("aria-invalid", data.errors.some((e) => e.includes("node name")) ? "true" : "false");
  els.submit.disabled = !data.ok || !state.changesAllowed || Boolean(state.jobId);
  els.submit.textContent = node.role === "standby" ? "Add standby" : "Add node";
  if (data.ok && state.changesAllowed && !state.jobId) {
    els.submitHint.textContent = node.builds_from_source
      ? "This compiles PostgreSQL on the host; it will take a while."
      : "Existing nodes keep serving writes throughout.";
  }
}

/* ------------------------------------------------------------------ submit */

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.submit.disabled = true;
  try {
    const response = await fetch("/api/add-node", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formState()),
    });
    const data = await response.json();
    if (!response.ok) { say(data.error || "could not start"); els.submit.disabled = false; return; }
    say("");
    followJob(data.id);
  } catch (error) {
    say(`could not start: ${error}`);
    els.submit.disabled = false;
  }
});

/* --------------------------------------------------------------- progress */

function followJob(jobId) {
  state.jobId = jobId;
  els.job.hidden = false;
  els.submit.disabled = true;
  els.job.scrollIntoView({ behavior: "smooth", block: "start" });
  clearInterval(state.poll);
  pollJob();
  state.poll = setInterval(pollJob, 2000);
}

async function pollJob() {
  try {
    const response = await fetch(`/api/jobs/${state.jobId}`);
    const job = await response.json();
    if (!response.ok) { clearInterval(state.poll); say(job.error || "lost the job"); return; }
    showJob(job);
    if (job.status !== "running") {
      clearInterval(state.poll);
      state.jobId = null;
      preview();          // the next free name and ports have moved on
      load();
    }
  } catch (error) {
    /* a dropped poll is not worth a message; the next one will tell us */
  }
}

function showJob(job) {
  els.jobTitle.textContent = job.status === "running"
    ? job.summary
    : `${job.summary} — ${job.status}`;
  els.jobElapsed.textContent = `${job.elapsed}s`;
  els.jobSpinner.classList.toggle("done", job.status !== "running");
  els.jobSpinner.textContent = job.status === "succeeded" ? "✓"
    : job.status === "failed" ? "✕" : "";

  els.jobSteps.innerHTML = job.steps.map((step) => `
    <li class="${step.status}">
      <span class="mark">${step.status === "passed" ? "✓"
        : step.status === "failed" ? "✕" : "◉"}</span>
      <span><strong>${step.name}</strong>${step.message ? `<br><span class="msg">${step.message}</span>` : ""}</span>
      <span class="secs">${step.duration ? `${step.duration}s` : ""}</span>
    </li>`).join("");

  els.jobLog.textContent = job.log || "";
  const footer = [];
  if (job.error) footer.push(job.error);
  (job.warnings || []).forEach((warning) => footer.push(warning));
  footer.push(`Logs: ${job.log_dir}`);
  els.jobFooter.textContent = footer.join(" · ");
}

/* ------------------------------------------------------------------ boot */

const picker = document.getElementById("cluster-picker");
if (picker) {
  picker.addEventListener("change", () => {
    window.location.search = `?cluster=${encodeURIComponent(picker.value)}`;
  });
}

load();
