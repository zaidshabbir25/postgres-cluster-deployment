/* Deploy form.
 *
 * Like the add-node form, the browser decides nothing: every keystroke posts to
 * /api/deploy/preview, which runs `topology.plan_cluster` — the same call the
 * deployment makes. The table under "What will be built" is therefore the
 * cluster that will exist, ports and scopes included, and its warnings are the
 * ones the run will print.
 */

const els = {
  form: document.getElementById("deploy-form"),
  notice: document.getElementById("notice"),
  hosts: document.getElementById("hosts"),
  hostsHint: document.getElementById("hosts-hint"),
  addHost: document.getElementById("add-host"),
  saveHost: document.getElementById("save-host"),
  newHostHint: document.getElementById("new-host-hint"),
  mode: document.getElementById("deploy_mode"),
  nodeCount: document.getElementById("node_count"),
  nodesHint: document.getElementById("nodes-hint"),
  pgMajor: document.getElementById("pg_major"),
  versionField: document.getElementById("version-field"),
  pgVersion: document.getElementById("pg_version"),
  versionHint: document.getElementById("version-hint"),
  channelField: document.getElementById("channel-field"),
  channel: document.getElementById("repo_channel"),
  spock: document.getElementById("spock_major"),
  branchField: document.getElementById("branch-field"),
  branch: document.getElementById("spock_branch"),
  standbys: document.getElementById("standbys"),
  syncBlock: document.getElementById("sync-block"),
  syncMode: document.getElementById("sync_mode"),
  syncExtra: document.querySelector(".sync-extra"),
  syncCount: document.getElementById("sync_count"),
  syncStrict: document.getElementById("sync_strict"),
  planSummary: document.getElementById("plan-summary"),
  planTable: document.getElementById("plan-table"),
  messages: document.getElementById("preview-messages"),
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

const state = {
  changesAllowed: document.body.dataset.changes === "yes",
  options: null,
  hosts: new Set(),
  mode: "packages",
  pgMajor: "17",
  channel: "release",
  spockMajor: "50",
  standbys: new Set(),
  syncMode: "async",
  jobId: null,
  poll: null,
};

function say(message, kind = "warn") {
  els.notice.textContent = message;
  els.notice.className = `notice ${kind}`;
  els.notice.hidden = !message;
}

/* ----------------------------------------------------------------- load */

async function load() {
  try {
    const response = await fetch("/api/deploy/options");
    const data = await response.json();
    state.options = data;
    render();
    if (data.running_job) followJob(data.running_job.id);
  } catch (error) {
    say(`could not reach the dashboard: ${error}`);
  }
}

function render() {
  const { hosts, defaults, inventory_problem: problem } = state.options;

  if (problem) say(`${problem} — add a machine below to get started.`);
  renderHosts(hosts);
  renderMajors();
  renderSpock();
  renderStandbys();

  document.getElementById("cluster_name").value = defaults.cluster_name;
  document.getElementById("db_name").value = defaults.db_name;
  document.getElementById("db_user").value = defaults.db_user;
  document.getElementById("data_root").value = defaults.data_root;
  document.getElementById("base_port").value = defaults.base_port;
  document.getElementById("base_restapi_port").value = defaults.base_restapi_port;
  els.nodeCount.value = defaults.node_count;

  if (!state.changesAllowed) els.submitHint.textContent = "read-only dashboard — plan only";
  preview();
}

function renderHosts(hosts) {
  els.hosts.innerHTML = "";
  hosts.forEach((host) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "host-card";
    card.setAttribute("role", "checkbox");
    card.dataset.value = host.name;
    card.innerHTML = `
      <span class="host-name">${host.name}</span>
      <span class="host-addr">${host.address}</span>
      <span class="host-nodes">${host.local ? "local — no SSH" : `${host.username}@ssh`}</span>`;
    card.addEventListener("click", () => {
      if (state.hosts.has(host.name)) state.hosts.delete(host.name);
      else state.hosts.add(host.name);
      markHosts();
      preview();
    });
    els.hosts.appendChild(card);
    state.hosts.add(host.name);            // everything in the inventory, by default
  });
  markHosts();
  els.addHost.open = hosts.length === 0;
}

function markHosts() {
  let chosen = 0;
  els.hosts.querySelectorAll(".host-card").forEach((card) => {
    const on = state.hosts.has(card.dataset.value);
    card.classList.toggle("on", on);
    card.setAttribute("aria-checked", on ? "true" : "false");
    if (on) chosen += 1;
  });
  els.hostsHint.textContent = chosen
    ? `${chosen} machine${chosen === 1 ? "" : "s"} chosen. One Spock node per machine; ` +
      `any beyond that share a machine on separate ports.`
    : "Choose at least one machine.";
}

function renderMajors() {
  const { pg_majors: majors, defaults } = state.options;
  state.pgMajor = defaults.pg_major;
  els.pgMajor.innerHTML = majors.map((major) => `
    <button type="button" role="radio" data-value="${major}"
            aria-checked="${major === state.pgMajor}"
            class="${major === state.pgMajor ? "on" : ""}">
      <strong>${major}</strong></button>`).join("");
}

function renderSpock() {
  const { spock_majors: majors, spock_branches: branches, defaults } = state.options;
  state.spockMajor = defaults.spock_major;
  els.spock.innerHTML = majors.map((major) => `
    <button type="button" role="radio" data-value="${major}"
            aria-checked="${major === state.spockMajor}"
            class="${major === state.spockMajor ? "on" : ""}">
      <strong>spock${major}</strong><small>${branches[major]}</small></button>`).join("");
  els.branch.placeholder = branches[state.spockMajor];
}

function renderStandbys() {
  const count = Number(els.nodeCount.value || 2);
  const names = Array.from({ length: count }, (_, index) => `n${index + 1}`);
  [...state.standbys].forEach((name) => {
    if (!names.includes(name)) state.standbys.delete(name);   // node count shrank
  });
  els.standbys.innerHTML = names.map((name) => `
    <button type="button" class="chip ${state.standbys.has(name) ? "on" : ""}"
            role="checkbox" aria-checked="${state.standbys.has(name)}"
            data-value="${name}">${name}</button>`).join("");
  els.syncBlock.hidden = state.standbys.size === 0;
}

els.standbys.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  const name = chip.dataset.value;
  if (state.standbys.has(name)) state.standbys.delete(name);
  else state.standbys.add(name);
  renderStandbys();
  preview();
});

/* ------------------------------------------------------------- controls */

const refresh = debounce(() => preview());

wireSegmented(els.mode, (value) => {
  state.mode = value;
  els.versionField.hidden = value !== "source";
  els.branchField.hidden = value !== "source";
  els.channelField.hidden = value === "source";
  els.versionHint.textContent = versionHint();
  preview();
});

wireSegmented(els.pgMajor, (value) => {
  state.pgMajor = value;
  if (!els.pgVersion.dataset.touched) els.pgVersion.value = newestFor(value) || "";
  els.versionHint.textContent = versionHint();
  preview();
});

wireSegmented(els.channel, (value) => { state.channel = value; preview(); });

wireSegmented(els.spock, (value) => {
  state.spockMajor = value;
  const branches = state.options.spock_branches;
  els.branch.placeholder = branches[value];
  if (!els.branch.dataset.touched) els.branch.value = "";
  preview();
});

wireSegmented(els.syncMode, (value) => {
  state.syncMode = value;
  els.syncExtra.hidden = value === "async";
  preview();
});

wireCombo(els.pgVersion, (typed) => {
  const published = (state.options.pg_versions || {})[state.pgMajor] || [];
  const items = published.slice().reverse().map((version, index) => ({
    value: version,
    note: index === 0 ? "newest"
      : (version.includes("beta") || version.includes("rc") ? "pre-release" : ""),
  }));
  const needle = typed.toLowerCase();
  return needle ? items.filter((item) => item.value.toLowerCase().includes(needle)) : items;
}, refresh);

wireCombo(els.branch, () => {
  const branches = state.options.spock_branches;
  return [{ group: "branches" },
          { value: branches[state.spockMajor], note: `spock${state.spockMajor} default` },
          { value: "main" }]
    .filter((item, index, all) =>
      item.group || all.findIndex((other) => other.value === item.value) === index);
}, refresh);

wireSteppers(document, () => { renderStandbys(); preview(); });

["cluster_name", "db_name", "db_user", "data_root", "node_count",
 "base_port", "base_restapi_port", "sync_count"].forEach((id) => {
  document.getElementById(id).addEventListener("input", () => {
    if (id === "node_count") renderStandbys();
    refresh();
  });
});
document.getElementById("clean").addEventListener("change", refresh);
els.syncStrict.addEventListener("change", refresh);

function newestFor(major) {
  const published = (state.options.pg_versions || {})[major] || [];
  return published[published.length - 1] || "";
}

function versionHint() {
  if (state.mode !== "source") return "";
  const published = (state.options.pg_versions || {})[state.pgMajor] || [];
  return published.length
    ? `Published for ${state.pgMajor}: ${published.join(", ")}`
    : `The version list could not be fetched — type an exact version.`;
}

/* -------------------------------------------------------------- preview */

function formState() {
  return {
    hosts: [...state.hosts],
    deploy_mode: state.mode,
    node_count: Number(els.nodeCount.value || 2),
    standby_of: [...state.standbys],
    pg_major: state.pgMajor,
    pg_version: state.mode === "source" ? els.pgVersion.value.trim() : "",
    spock_major: state.spockMajor,
    spock_branch: state.mode === "source" ? els.branch.value.trim() : "",
    repo_channel: state.channel,
    cluster_name: document.getElementById("cluster_name").value.trim(),
    db_name: document.getElementById("db_name").value.trim(),
    db_user: document.getElementById("db_user").value.trim(),
    data_root: document.getElementById("data_root").value.trim(),
    base_port: Number(document.getElementById("base_port").value || 5432),
    base_restapi_port: Number(document.getElementById("base_restapi_port").value || 8008),
    clean: document.getElementById("clean").checked,
    sync_mode: state.standbys.size ? state.syncMode : "off",
    sync_count: Number(els.syncCount.value || 1),
    sync_strict: els.syncStrict.checked,
  };
}

async function preview() {
  try {
    const response = await fetch("/api/deploy/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formState()),
    });
    showPreview(await response.json());
  } catch (error) {
    say(`preview failed: ${error}`);
  }
}

function showPreview(data) {
  const { summary, nodes, etcd } = data;
  els.planSummary.innerHTML = `
    <span><b>${summary.cluster}</b></span>
    <span>${summary.mode === "source" ? "built from source" : "native packages"}</span>
    <span>PostgreSQL ${summary.postgres}</span>
    <span>spock${summary.spock}</span>
    <span>${summary.hosts.length} host${summary.hosts.length === 1 ? "" : "s"}</span>`;

  els.planTable.innerHTML = nodes.length ? `
    <thead><tr><th>Node</th><th>Role</th><th>Host</th><th>PostgreSQL</th>
      <th>Patroni</th><th>Scope</th><th>Follows</th></tr></thead>
    <tbody>${nodes.map((node) => `
      <tr>
        <td><b>${node.name}</b></td><td>${node.role}</td><td>${node.host}</td>
        <td>${node.address}:${node.pg_port}</td>
        <td>:${node.restapi_port}</td>
        <td>${node.scope}</td><td>${node.follows || "—"}</td>
      </tr>`).join("")}</tbody>` : "";

  els.messages.innerHTML = [
    ...data.errors.map((text) => `<li class="error">${text}</li>`),
    ...data.warnings.map((text) => `<li>${text}</li>`),
    ...(etcd && etcd.length ? [`<li class="plain">etcd: ${etcd.join(", ")}</li>`] : []),
  ].join("");

  els.submit.disabled = !data.ok || !state.changesAllowed || Boolean(state.jobId);
  if (data.ok && state.changesAllowed && !state.jobId) {
    els.submitHint.textContent = state.mode === "source"
      ? "Compiles PostgreSQL on every host — allow 20-40 minutes each."
      : "Installs packages on every host; usually a few minutes.";
  }
}

/* ----------------------------------------------------- adding a machine */

els.saveHost.addEventListener("click", async () => {
  const body = {
    host: document.getElementById("new-address").value.trim(),
    name: document.getElementById("new-name").value.trim(),
    username: document.getElementById("new-user").value.trim() || "root",
    port: Number(document.getElementById("new-port").value || 22),
    key_file: document.getElementById("new-key").value.trim(),
    local: document.getElementById("new-local").checked,
  };
  els.newHostHint.textContent = "";
  try {
    const response = await fetch("/api/inventory/hosts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) { els.newHostHint.textContent = data.error; return; }
    ["new-address", "new-name", "new-key"].forEach((id) => {
      document.getElementById(id).value = "";
    });
    els.newHostHint.textContent = `${data.host.name} added.`;
    await load();
  } catch (error) {
    els.newHostHint.textContent = `could not add it: ${error}`;
  }
});

/* --------------------------------------------------------------- submit */

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.submit.disabled = true;
  try {
    const response = await fetch("/api/deploy", {
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

/* -------------------------------------------------------------- progress */

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
    if (!response.ok) { clearInterval(state.poll); return; }
    showJob(job);
    if (job.status !== "running") {
      clearInterval(state.poll);
      state.jobId = null;
      if (job.status === "succeeded") {
        els.submitHint.innerHTML =
          `Deployed. <a href="/">Open the health page</a>.`;
      }
      preview();
    }
  } catch (error) {
    /* a dropped poll is not worth a message */
  }
}

function showJob(job) {
  els.jobTitle.textContent = job.status === "running"
    ? job.summary : `${job.summary} — ${job.status}`;
  els.jobElapsed.textContent = `${job.elapsed}s`;
  els.jobSpinner.classList.toggle("done", job.status !== "running");
  els.jobSpinner.textContent = job.status === "succeeded" ? "✓"
    : job.status === "failed" ? "✕" : "";

  els.jobSteps.innerHTML = job.steps.map((step) => `
    <li class="${step.status}">
      <span class="mark">${step.status === "passed" ? "✓"
        : step.status === "failed" ? "✕" : "◉"}</span>
      <span><strong>${step.name}</strong>${
        step.message ? `<br><span class="msg">${step.message}</span>` : ""}</span>
      <span class="secs">${step.duration ? `${step.duration}s` : ""}</span>
    </li>`).join("");

  els.jobLog.textContent = job.log || "";
  els.jobLog.scrollTop = els.jobLog.scrollHeight;
  const footer = [];
  if (job.error) footer.push(job.error);
  (job.warnings || []).forEach((warning) => footer.push(warning));
  footer.push(`Logs: ${job.log_dir}`);
  els.jobFooter.textContent = footer.join(" · ");
}

load();
