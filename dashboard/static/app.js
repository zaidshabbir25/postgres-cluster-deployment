/* Cluster dashboard front end.
 *
 * The server already does the slow work and caches it, so this only polls a
 * JSON endpoint and re-renders. Everything is built with textContent rather
 * than innerHTML: node names, package versions and error strings all originate
 * on remote hosts, and none of them should be able to inject markup here.
 */

(function () {
  "use strict";

  const body = document.body;
  const pollInterval = Math.max(5, parseInt(body.dataset.interval, 10) || 20) * 1000;
  let cluster = body.dataset.cluster || "";
  let timer = null;
  let inFlight = false;

  const el = {
    dot: document.getElementById("overall-dot"),
    badge: document.getElementById("overall-badge"),
    name: document.getElementById("cluster-name"),
    freshness: document.getElementById("freshness"),
    refresh: document.getElementById("refresh"),
    picker: document.getElementById("cluster-picker"),
    notice: document.getElementById("notice"),
    stats: document.getElementById("stats"),
    nodes: document.getElementById("nodes"),
    scopes: document.getElementById("scopes"),
    etcd: document.getElementById("etcd"),
    hosts: document.querySelector("#hosts-table tbody"),
    problems: document.getElementById("problems"),
    problemsSection: document.getElementById("problems-section"),
    problemCount: document.getElementById("problem-count"),
  };

  // ---------------------------------------------------------------- helpers

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function statusClass(status) {
    return ["ok", "degraded", "down"].includes(status) ? status : "unknown";
  }

  /* Render a value that may legitimately be absent. */
  function show(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback || "—";
    return String(value);
  }

  function defList(pairs) {
    const list = make("dl", "kv");
    pairs.forEach(function (pair) {
      if (pair[1] === null || pair[1] === undefined) return;
      list.appendChild(make("dt", null, pair[0]));
      list.appendChild(make("dd", null, pair[1]));
    });
    return list;
  }

  function statCard(key, value, small) {
    const card = make("div", "stat");
    card.appendChild(make("div", "k", key));
    card.appendChild(make("div", small ? "v small" : "v", value));
    return card;
  }

  // ---------------------------------------------------------------- render

  function renderHeader(data) {
    const status = statusClass(data.status);
    el.dot.className = "dot " + status;
    el.badge.className = "badge " + status;
    el.badge.textContent = (data.status || "unknown").toUpperCase();
    if (data.cluster) el.name.textContent = data.cluster;

    const bits = [];
    if (typeof data.stale_seconds === "number") {
      bits.push("updated " + data.stale_seconds + "s ago");
    }
    if (typeof data.collection_seconds === "number") {
      bits.push("collected in " + data.collection_seconds + "s");
    }
    el.freshness.textContent = bits.join(" · ") || "—";
    el.freshness.classList.toggle("spin", Boolean(data.collecting));
  }

  function renderNotice(data) {
    // An error alongside existing node data means the refresh is failing but
    // the last good snapshot is still on screen — say so rather than blanking.
    if (data.error) {
      el.notice.hidden = false;
      el.notice.textContent = (data.nodes && data.nodes.length)
        ? "Last refresh failed (showing the previous snapshot): " + data.error
        : data.error;
    } else {
      el.notice.hidden = true;
      el.notice.textContent = "";
    }
  }

  function renderStats(data) {
    clear(el.stats);
    const summary = data.summary || {};
    const etcd = data.etcd || {};

    el.stats.appendChild(statCard(
      "Nodes healthy",
      show(summary.nodes_ok, "0") + " / " + show(summary.nodes, "0")
    ));
    el.stats.appendChild(statCard("Spock nodes", show(summary.spock_nodes, "0")));
    el.stats.appendChild(statCard("Standbys", show(summary.standby_nodes, "0")));
    el.stats.appendChild(statCard(
      "Hosts up",
      show(summary.hosts_up, "0") + " / " + show(summary.hosts, "0")
    ));
    el.stats.appendChild(statCard(
      "etcd",
      show(etcd.healthy_count, "?") + " / " + show(etcd.member_count, "?") + " healthy",
      true
    ));
    el.stats.appendChild(statCard("Problems", show(summary.problem_count, "0")));
  }

  function renderNodes(data) {
    clear(el.nodes);
    const nodes = data.nodes || [];
    if (!nodes.length) {
      el.nodes.appendChild(make("p", "hint", "No nodes to show."));
      return;
    }

    nodes.forEach(function (node) {
      const card = make("div", "card status-" + statusClass(node.status));

      const head = make("div", "card-head");
      const left = make("div");
      left.appendChild(make("span", "name", node.name));
      left.appendChild(document.createTextNode(" "));
      left.appendChild(make("span", "role", node.role === "spock" ? "spock node" : "standby"));
      head.appendChild(left);
      head.appendChild(make("span", "badge " + statusClass(node.status),
                            (node.status || "unknown").toUpperCase()));
      card.appendChild(head);

      const subs = node.role === "spock"
        ? show(node.subscriptions_replicating, "0") + " / " + show(node.subscriptions_expected, "0")
        : null;

      const writes = node.accepting_writes === true ? "yes"
        : node.accepting_writes === false ? "no (read-only)" : "unknown";

      card.appendChild(defList([
        ["Address", node.address + ":" + node.pg_port],
        ["Patroni", show(node.patroni_role) + " / " + show(node.patroni_state)],
        ["Scope", node.scope],
        ["Accepts writes", writes],
        ["PostgreSQL", show(node.pg_version)],
        ["Spock", node.role === "spock" ? show(node.spock_version) : null],
        ["Subscriptions", subs],
        ["Follows", node.follows || null],
        ["Standbys", (node.leader_of && node.leader_of.length) ? node.leader_of.join(", ") : null],
        ["Timeline", show(node.timeline, null)],
      ]));

      if (node.slots && node.slots.length) {
        const slots = make("div");
        slots.style.marginTop = ".5rem";
        node.slots.forEach(function (slot) {
          const pill = make("span", "pill" + (slot.active ? "" : " bad"),
                            slot.name + " " + (slot.active ? "active" : "idle " + slot.retained_wal));
          slots.appendChild(pill);
        });
        card.appendChild(slots);
      }

      if (node.problems && node.problems.length) {
        const list = make("ul", "card-problems");
        node.problems.forEach(function (problem) {
          list.appendChild(make("li", null, problem));
        });
        card.appendChild(list);
      }

      el.nodes.appendChild(card);
    });
  }

  function renderScopes(data) {
    clear(el.scopes);
    const scopes = data.scopes || {};
    const names = Object.keys(scopes);
    if (!names.length) {
      el.scopes.appendChild(make("p", "hint", "No Patroni scopes reported."));
      return;
    }

    names.sort().forEach(function (name) {
      const scope = scopes[name];
      const card = make("div", "card status-" + statusClass(scope.status));

      const head = make("div", "card-head");
      head.appendChild(make("span", "name", name));
      head.appendChild(make("span", "badge " + statusClass(scope.status),
                            (scope.status || "unknown").toUpperCase()));
      card.appendChild(head);

      const members = make("div");
      (scope.members || []).forEach(function (member) {
        const role = (member.role || "").toLowerCase();
        const isLeader = role === "leader" || role === "master" || role === "primary";
        const healthy = ["running", "streaming"].includes((member.state || "").toLowerCase());
        const pill = make(
          "span",
          "pill " + (!healthy ? "bad" : isLeader ? "leader" : "replica"),
          member.name + " · " + show(member.role) + " · " + show(member.state)
        );
        members.appendChild(pill);
      });
      if (!(scope.members || []).length) {
        members.appendChild(make("span", "pill bad", "no members answered"));
      }
      card.appendChild(members);

      card.appendChild(defList([
        ["Leader", show(scope.leader)],
        ["Members", show((scope.members || []).length, "0") + " of " +
                    show((scope.expected_members || []).length, "0") + " expected"],
        ["Queried via", show(scope.queried_from)],
      ]));

      if (scope.problems && scope.problems.length) {
        const list = make("ul", "card-problems");
        scope.problems.forEach(function (problem) {
          list.appendChild(make("li", null, problem));
        });
        card.appendChild(list);
      }

      el.scopes.appendChild(card);
    });
  }

  function renderEtcd(data) {
    clear(el.etcd);
    const etcd = data.etcd || {};

    const head = make("div", "card-head");
    head.appendChild(make("span", "name", "etcd"));
    head.appendChild(make("span", "badge " + statusClass(etcd.status),
                          (etcd.status || "unknown").toUpperCase()));
    el.etcd.appendChild(head);

    el.etcd.appendChild(defList([
      ["Healthy", show(etcd.healthy_count, "?") + " / " + show(etcd.member_count, "?")],
      ["Endpoints", (etcd.endpoints || []).join(", ") || "—"],
      ["Queried from", show(etcd.queried_from)],
      ["Note", etcd.note || null],
    ]));
  }

  function renderHosts(data) {
    clear(el.hosts);
    (data.hosts || []).forEach(function (host) {
      const row = make("tr");
      const memory = host.memory_mb || {};
      const disk = (host.disks || [])
        .map(function (d) { return d.mount + " " + d.used_pct + "%"; })
        .join(", ");

      [
        host.name,
        host.address,
        show(host.platform) + (host.arch ? " (" + host.arch + ")" : ""),
        show(host.load_average),
        memory.total ? (memory.used || "?") + " / " + memory.total + " MB" : "—",
        disk || "—",
        host.is_etcd_member ? "member" : "—",
      ].forEach(function (value) {
        row.appendChild(make("td", null, value));
      });

      const statusCell = make("td");
      statusCell.appendChild(make("span", "badge " + statusClass(host.status),
                                  (host.status || "unknown").toUpperCase()));
      if (host.error) statusCell.appendChild(make("div", "hint", host.error));
      row.appendChild(statusCell);

      el.hosts.appendChild(row);
    });
  }

  function renderProblems(data) {
    clear(el.problems);
    const problems = data.problems || [];
    el.problemsSection.hidden = problems.length === 0;
    el.problemCount.textContent = problems.length ? "(" + problems.length + ")" : "";
    problems.forEach(function (problem) {
      el.problems.appendChild(make("li", null, problem));
    });
  }

  function render(data) {
    renderHeader(data);
    renderNotice(data);
    renderStats(data);
    renderNodes(data);
    renderScopes(data);
    renderEtcd(data);
    renderHosts(data);
    renderProblems(data);
  }

  // ------------------------------------------------------------------ poll

  function url(path) {
    return path + (cluster ? "?cluster=" + encodeURIComponent(cluster) : "");
  }

  async function poll() {
    if (inFlight) return;
    inFlight = true;
    try {
      const response = await fetch(url("/api/health"), {
        headers: { Accept: "application/json" },
      });
      const data = await response.json();
      render(data);
    } catch (error) {
      // Losing the dashboard itself is a different failure from an unhealthy
      // cluster, so label it as such instead of showing everything as down.
      renderNotice({ error: "Cannot reach the dashboard server: " + error.message });
      el.dot.className = "dot unknown";
      el.badge.className = "badge unknown";
      el.badge.textContent = "OFFLINE";
    } finally {
      inFlight = false;
    }
  }

  function schedule() {
    if (timer) clearInterval(timer);
    timer = setInterval(poll, pollInterval);
  }

  el.refresh.addEventListener("click", async function () {
    el.refresh.disabled = true;
    el.refresh.textContent = "Refreshing…";
    try {
      await fetch(url("/api/refresh"), { method: "POST" });
      // Give the background collector a moment to start before reading back.
      setTimeout(poll, 800);
    } catch (error) {
      renderNotice({ error: "Refresh request failed: " + error.message });
    } finally {
      setTimeout(function () {
        el.refresh.disabled = false;
        el.refresh.textContent = "Refresh";
      }, 1500);
    }
  });

  if (el.picker) {
    el.picker.addEventListener("change", function () {
      cluster = el.picker.value;
      history.replaceState(null, "", url(""));
      poll();
    });
  }

  // Stop polling a hidden tab; catch up as soon as it is visible again.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer) clearInterval(timer);
      timer = null;
    } else {
      poll();
      schedule();
    }
  });

  poll();
  schedule();
})();
