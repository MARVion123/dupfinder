/* Duplicate File Finder for Synology NAS UI - vanilla JS, no build step. */
(function () {
  "use strict";

  var state = {
    scanId: null,
    sort: "wasted",
    dir: "desc",
    offset: 0,
    limit: 100,
    total: 0,
    groups: [],
    open: {},                 // group id -> true
    selected: {},             // file id -> {size, path}
    token: localStorage.getItem("dupfinder_token") || "",
    scanRunning: false,
    lastRoot: null,
    pickPath: ""
  };

  var $ = function (id) { return document.getElementById(id); };

  // ---------------------------------------------------------------- utils
  function fmtBytes(n) {
    n = Number(n || 0);
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0;
    while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + units[i];
  }
  function fmtNum(n) { return Number(n || 0).toLocaleString(); }
  function fmtDate(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function fmtDuration(s) {
    s = Math.max(0, Math.round(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return h + "h " + m + "m";
    if (m) return m + "m " + sec + "s";
    return sec + "s";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function splitPath(p) {
    var i = String(p).lastIndexOf("/");
    if (i < 0) return ["", p];
    return [p.slice(0, i + 1), p.slice(i + 1)];
  }

  var toastTimer = null;
  function toast(msg, isError) {
    var el = $("toast");
    el.textContent = msg;
    el.className = "toast" + (isError ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.add("hidden"); }, isError ? 7000 : 3500);
  }

  // ---------------------------------------------------------------- api
  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (state.token) headers.Authorization = "Bearer " + state.token;
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (res.status === 401) {
          var t = window.prompt("This server requires an access token:");
          if (t) { localStorage.setItem("dupfinder_token", t); state.token = t; }
          throw new Error("Authentication required");
        }
        if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
        return data;
      });
    });
  }

  function fail(err) { toast(err.message || String(err), true); }

  // ---------------------------------------------------------------- status polling
  function pollStatus() {
    api("/api/status").then(function (data) {
      renderScanStatus(data.scan);
      renderAiStatus(data.ai);
      window.__pillow = data.pillow;
    }).catch(function () { /* transient - keep polling */ });
  }

  function renderScanStatus(scan) {
    var running = scan.state === "running";
    var panel = $("progress");
    $("btnCancel").classList.toggle("hidden", !running);
    $("btnRescan").classList.toggle("hidden", running || !state.lastRoot);
    $("btnPick").disabled = running;

    if (scan.root) state.lastRoot = scan.root;

    if (running) {
      panel.classList.remove("hidden");
      $("progPhase").textContent =
        "Step " + scan.phase_index + "/" + scan.phase_total + " — " + scan.phase_label;
      $("progElapsed").textContent = fmtDuration(scan.elapsed) + " elapsed";
      var bar = $("progBar");
      if (scan.phase_progress === null || scan.phase_progress === undefined) {
        bar.classList.add("indeterminate");
        bar.style.width = "35%";
      } else {
        bar.classList.remove("indeterminate");
        bar.style.width = Math.min(100, scan.phase_progress) + "%";
      }
      $("progPath").textContent = scan.current || "";
      $("progStats").innerHTML =
        stat("Files indexed", fmtNum(scan.files_seen)) +
        stat("Data seen", fmtBytes(scan.bytes_seen)) +
        stat("Hashed", fmtBytes(scan.bytes_hashed)) +
        stat("Reused from cache", fmtNum(scan.cache_hits)) +
        stat("Groups so far", fmtNum(scan.groups_found));
    } else {
      panel.classList.add("hidden");
    }

    var sub = $("scanSubtitle");
    if (scan.state === "idle" && !scan.root) {
      sub.textContent = "No scan yet";
    } else {
      var word = { running: "Scanning", done: "Finished", cancelled: "Stopped",
                   error: "Failed", idle: "Ready" }[scan.state] || scan.state;
      sub.textContent = word + " · " + (scan.root || "") +
        (scan.state !== "running" && scan.elapsed ? " · " + fmtDuration(scan.elapsed) : "");
    }
    if (scan.error) sub.textContent += " · " + scan.error;

    if (state.scanRunning && !running) {
      state.scanRunning = false;
      state.offset = 0;
      loadScans();
      loadGroups();
      toast(scan.state === "cancelled"
        ? "Scan stopped — partial results kept."
        : "Scan finished.");
    }
    state.scanRunning = running;
    if (running && !state.__tick) {
      state.__tick = setInterval(function () {
        if (Date.now() - (state.__lastLoad || 0) > 6000) { state.__lastLoad = Date.now(); loadGroups(true); }
      }, 6000);
    }
    if (!running && state.__tick) { clearInterval(state.__tick); state.__tick = null; }
  }

  function stat(label, value) {
    return "<span><strong>" + esc(value) + "</strong> " + esc(label) + "</span>";
  }

  function renderAiStatus(ai) {
    var running = ai.state === "running";
    $("aiProgress").classList.toggle("hidden", !running);
    $("btnSuggest").disabled = running || state.scanRunning;
    if (running) {
      var pct = ai.todo ? Math.round(ai.done * 100 / ai.todo) : 0;
      $("aiBar").style.width = pct + "%";
      $("aiNote").textContent = ai.done + " of " + ai.todo + " groups reviewed" +
        (ai.model ? " · " + ai.model : "");
    }
    if (state.__aiWas && !running) {
      state.__aiWas = false;
      loadGroups();
      toast(ai.error ? ("Suggestions finished with an issue: " + ai.error)
                     : "Suggestions ready.", !!ai.error);
    }
    state.__aiWas = running;
  }

  // ---------------------------------------------------------------- groups
  function query() {
    var p = new URLSearchParams();
    if (state.scanId) p.set("scan_id", state.scanId);
    p.set("sort", state.sort);
    p.set("dir", state.dir);
    p.set("limit", state.limit);
    p.set("offset", state.offset);
    p.set("kind", $("fKind").value);
    p.set("min_similarity", $("fSim").value);
    p.set("min_size", $("fSize").value);
    if ($("fSearch").value.trim()) p.set("q", $("fSearch").value.trim());
    if ($("fSuggested").checked) p.set("suggested_only", "1");
    return p.toString();
  }

  function loadGroups(quiet) {
    state.__lastLoad = Date.now();
    return api("/api/groups?" + query()).then(function (data) {
      state.groups = data.groups || [];
      state.total = data.total || 0;
      if (data.scan_id) state.scanId = data.scan_id;
      var s = data.summary || {};
      $("sumGroups").textContent = fmtNum(s.groups || 0);
      $("sumFiles").textContent = fmtNum(s.files || 0);
      $("sumWasted").textContent = fmtBytes(s.wasted || 0);
      $("sumShown").textContent = fmtNum(state.groups.length);
      renderGrid();
    }).catch(function (err) { if (!quiet) fail(err); });
  }

  function simBadge(g) {
    var cls = g.kind === "exact" ? "exact"
      : g.similarity >= 90 ? "high" : g.similarity >= 75 ? "mid" : "low";
    var text = g.similarity >= 99.5 ? "100%" : g.similarity.toFixed(0) + "%";
    var ver = g.verified ? '<span class="verified" title="Confirmed byte for byte">✔ verified</span>' : "";
    return '<span class="badge ' + cls + '">' + text + "</span>" + ver;
  }

  function renderGrid() {
    var body = $("gridBody");
    body.innerHTML = "";
    $("emptyState").classList.toggle("hidden", state.groups.length > 0);

    state.groups.forEach(function (g) {
      var parts = splitPath(g.files && g.files[0] ? g.files[0].path : g.label || "");
      var tr = document.createElement("tr");
      tr.className = "group-row" + (state.open[g.id] ? " open" : "");
      tr.dataset.gid = g.id;
      tr.innerHTML =
        '<td class="col-check"><input type="checkbox" class="pick-group" data-gid="' + g.id + '"' +
          (groupFullySelected(g) ? " checked" : "") + ' title="Select the suggested deletions in this group"></td>' +
        "<td>" + simBadge(g) + "</td>" +
        '<td><span class="gname" title="' + esc(g.files && g.files[0] ? g.files[0].path : "") + '">' +
          esc(parts[1] || g.label || "(group)") + "</span>" +
          '<span class="gsub">' + esc(parts[0]) + "</span></td>" +
        '<td class="num">' + g.file_count + "</td>" +
        '<td class="num">' + fmtBytes(g.max_size) + "</td>" +
        '<td class="num"><strong>' + fmtBytes(g.wasted_bytes) + "</strong></td>" +
        '<td class="num">' + g.folder_span + "</td>" +
        '<td><div class="sugg">' + (g.sugg_summary
            ? '<span class="who">' + esc(g.sugg_source) +
              (g.sugg_confidence != null ? " · " + g.sugg_confidence + "%" : "") + "</span><br>" +
              esc(g.sugg_summary)
            : '<span class="muted">—</span>') + "</div></td>";
      body.appendChild(tr);

      if (state.open[g.id]) body.appendChild(detailRow(g));
    });

    $("pageInfo").textContent = state.total
      ? (state.offset + 1) + "–" + Math.min(state.offset + state.limit, state.total) + " of " + fmtNum(state.total)
      : "";
    $("btnPrev").disabled = state.offset === 0;
    $("btnNext").disabled = state.offset + state.limit >= state.total;

    document.querySelectorAll("th.sortable").forEach(function (th) {
      th.classList.toggle("sorted", th.dataset.sort === state.sort);
      th.classList.toggle("asc", th.dataset.sort === state.sort && state.dir === "asc");
    });
    renderActionBar();
  }

  function detailRow(g) {
    var tr = document.createElement("tr");
    tr.className = "detail";
    var rows = (g.files || []).map(function (f) {
      var parts = splitPath(f.path);
      var gone = f.status !== "present";
      var tag = f.action
        ? '<span class="tag ' + f.action + '">' + f.action + "</span>"
        : "";
      return "<tr>" +
        '<td style="width:28px"><input type="checkbox" class="pick-file" data-fid=' + f.id +
          ' data-size=' + f.size + ' data-path="' + esc(f.path) + '"' +
          (state.selected[f.id] ? " checked" : "") + (gone ? " disabled" : "") + "></td>" +
        '<td class="fpath' + (gone ? " gone" : "") + '"><span class="fdir">' + esc(parts[0]) +
          '</span><span class="fname">' + esc(parts[1]) + "</span>" +
          (f.reason ? '<div class="reason">' + esc(f.reason) + "</div>" : "") +
          (gone ? '<div class="reason">' + esc(f.status) + "</div>" : "") + "</td>" +
        '<td class="num">' + (f.similarity >= 99.5 ? "100%" : f.similarity.toFixed(0) + "%") + "</td>" +
        '<td class="num">' + fmtBytes(f.size) + "</td>" +
        '<td class="num muted">' + esc(fmtDate(f.mtime)) + "</td>" +
        "<td>" + tag + "</td>" +
        "</tr>";
    }).join("");

    tr.innerHTML = '<td colspan="8"><div class="detail-inner">' +
      (g.merge_plan ? '<div class="merge"><strong>Merge plan:</strong> ' + esc(g.merge_plan) + "</div>" : "") +
      '<table class="files">' + rows + "</table>" +
      '<div class="detail-actions">' +
        '<button class="ghost small act-select-sugg" data-gid=' + g.id + '>Select suggested deletions</button>' +
        '<button class="ghost small act-select-others" data-gid=' + g.id + '>Keep newest, select the rest</button>' +
        '<button class="ghost small act-clear-group" data-gid=' + g.id + '>Clear this group</button>' +
        '<button class="ghost small act-suggest-one" data-gid=' + g.id + '>Ask Claude about this group</button>' +
      "</div></div></td>";
    return tr;
  }

  // ---------------------------------------------------------------- selection
  function suggestedIds(g) {
    return (g.files || [])
      .filter(function (f) { return f.action === "delete" && f.status === "present"; })
      .map(function (f) { return f; });
  }
  function groupFullySelected(g) {
    var s = suggestedIds(g);
    return s.length > 0 && s.every(function (f) { return state.selected[f.id]; });
  }
  function select(f, on) {
    if (on) state.selected[f.id] = { size: f.size, path: f.path };
    else delete state.selected[f.id];
  }
  function renderActionBar() {
    var ids = Object.keys(state.selected);
    var bytes = ids.reduce(function (a, k) { return a + (state.selected[k].size || 0); }, 0);
    $("actionBar").classList.toggle("hidden", ids.length === 0);
    $("selCount").textContent = ids.length + (ids.length === 1 ? " file" : " files");
    $("selBytes").textContent = fmtBytes(bytes);
  }

  // ---------------------------------------------------------------- events
  function bind() {
    $("gridBody").addEventListener("click", function (ev) {
      var t = ev.target;

      if (t.classList.contains("pick-group")) {
        ev.stopPropagation();
        var g = state.groups.find(function (x) { return x.id == t.dataset.gid; });
        if (!g) return;
        var sugg = suggestedIds(g);
        if (!sugg.length) { toast("No suggested deletions in this group yet — open it and pick manually."); t.checked = false; return; }
        sugg.forEach(function (f) { select(f, t.checked); });
        renderGrid();
        return;
      }
      if (t.classList.contains("pick-file")) {
        ev.stopPropagation();
        select({ id: t.dataset.fid, size: Number(t.dataset.size), path: t.dataset.path }, t.checked);
        renderActionBar();
        return;
      }
      if (t.classList.contains("act-select-sugg")) {
        var g1 = state.groups.find(function (x) { return x.id == t.dataset.gid; });
        suggestedIds(g1).forEach(function (f) { select(f, true); });
        renderGrid(); return;
      }
      if (t.classList.contains("act-select-others")) {
        var g2 = state.groups.find(function (x) { return x.id == t.dataset.gid; });
        var present = (g2.files || []).filter(function (f) { return f.status === "present"; });
        var newest = present.slice().sort(function (a, b) { return b.mtime - a.mtime; })[0];
        present.forEach(function (f) { if (!newest || f.id !== newest.id) select(f, true); });
        renderGrid(); return;
      }
      if (t.classList.contains("act-clear-group")) {
        var g3 = state.groups.find(function (x) { return x.id == t.dataset.gid; });
        (g3.files || []).forEach(function (f) { select(f, false); });
        renderGrid(); return;
      }
      if (t.classList.contains("act-suggest-one")) {
        api("/api/suggest", { method: "POST", body: {
          scan_id: state.scanId, group_ids: [Number(t.dataset.gid)] } })
          .then(function (r) {
            toast(r.engine === "ai" ? "Claude is looking at this group…" : "Suggestion updated (local rules).");
            if (r.engine !== "ai") setTimeout(loadGroups, 200);
          }).catch(fail);
        return;
      }

      var row = t.closest("tr.group-row");
      if (row) {
        var gid = row.dataset.gid;
        state.open[gid] = !state.open[gid];
        renderGrid();
      }
    });

    document.querySelectorAll("th.sortable").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.dataset.sort;
        if (state.sort === key) state.dir = state.dir === "desc" ? "asc" : "desc";
        else { state.sort = key; state.dir = key === "name" ? "asc" : "desc"; }
        state.offset = 0;
        loadGroups();
      });
    });

    $("checkAll").addEventListener("change", function () {
      var on = this.checked, any = false;
      state.groups.forEach(function (g) {
        suggestedIds(g).forEach(function (f) { any = true; select(f, on); });
      });
      if (!any && on) toast("No suggested deletions on this page yet. Run AI suggestions first.");
      renderGrid();
    });

    ["fKind", "fSim", "fSize", "fSuggested"].forEach(function (id) {
      $(id).addEventListener("change", function () { state.offset = 0; loadGroups(); });
    });
    var searchTimer = null;
    $("fSearch").addEventListener("input", function () {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { state.offset = 0; loadGroups(); }, 350);
    });
    $("fScan").addEventListener("change", function () {
      state.scanId = Number(this.value) || null;
      state.offset = 0; state.open = {}; state.selected = {};
      loadGroups();
    });

    $("btnPrev").addEventListener("click", function () {
      state.offset = Math.max(0, state.offset - state.limit); loadGroups();
    });
    $("btnNext").addEventListener("click", function () {
      state.offset += state.limit; loadGroups();
    });

    $("btnClearSel").addEventListener("click", function () {
      state.selected = {}; renderGrid();
    });

    $("btnDelete").addEventListener("click", function () {
      var ids = Object.keys(state.selected).map(Number);
      if (!ids.length) return;
      var mode = $("delMode").value;
      var wording = {
        quarantine: "moved into a .dupfinder-trash folder — you can restore them from the log",
        recycle: "moved into the DSM recycle bin of their share",
        permanent: "deleted permanently and cannot be recovered"
      }[mode];
      confirmThen(
        "Delete " + ids.length + " file" + (ids.length === 1 ? "" : "s") + "?",
        "They will be " + wording + ".",
        function () {
          api("/api/delete", { method: "POST", body: {
            file_ids: ids, mode: mode, confirm: true } })
            .then(function (r) {
              var msg = r.deleted.length + " removed, " + fmtBytes(r.freed_bytes) + " freed";
              if (r.skipped.length) msg += " · " + r.skipped.length + " kept for safety";
              if (r.failed.length) msg += " · " + r.failed.length + " failed";
              toast(msg, r.failed.length > 0);
              state.selected = {};
              loadGroups();
            }).catch(fail);
        });
    });

    $("btnCancel").addEventListener("click", function () {
      api("/api/scan/cancel", { method: "POST" })
        .then(function () { toast("Stopping…"); }).catch(fail);
    });
    $("btnRescan").addEventListener("click", function () {
      if (!state.lastRoot) return;
      startScan(state.lastRoot);
    });
    $("btnAiCancel").addEventListener("click", function () {
      api("/api/suggest/cancel", { method: "POST" }).catch(fail);
    });
    $("btnSuggest").addEventListener("click", function () {
      api("/api/suggest", { method: "POST", body: { scan_id: state.scanId } })
        .then(function (r) {
          if (r.engine === "ai") toast("Claude is reviewing " + r.groups + " groups…");
          else { toast("Used local rules for " + r.groups + " groups" + (r.note ? " — " + r.note : "")); loadGroups(); }
        }).catch(fail);
    });

    $("btnPick").addEventListener("click", function () { openPicker(state.lastRoot || ""); });
    $("btnStartScan").addEventListener("click", function () {
      if (!state.pickPath) { toast("Pick a folder first", true); return; }
      startScan(state.pickPath);
      closeModals();
    });
    $("pickList").addEventListener("click", function (ev) {
      var li = ev.target.closest("li");
      if (li) openPicker(li.dataset.path);
    });

    $("btnSettings").addEventListener("click", openSettings);
    $("btnSaveSettings").addEventListener("click", saveSettings);
    $("btnLog").addEventListener("click", openLog);
    $("btnEmptyTrash").addEventListener("click", function () {
      confirmThen("Empty quarantine?",
        "Everything quarantined during this scan will be deleted for good.",
        function () {
          api("/api/quarantine/empty", { method: "POST", body: { scan_id: state.scanId } })
            .then(function (r) { toast("Removed " + r.removed + " quarantined files"); openLog(); })
            .catch(fail);
        });
    });

    document.addEventListener("click", function (ev) {
      if (ev.target.hasAttribute("data-close") || ev.target.classList.contains("modal")) closeModals();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") closeModals();
    });
  }

  // ---------------------------------------------------------------- scan
  function startScan(root) {
    var body = {
      root: root,
      options: {
        verify_bytes: $("optVerify").checked,
        near_duplicates: $("optNear").checked,
        near_threshold: Number($("optThreshold").value) || 70
      }
    };
    api("/api/scan", { method: "POST", body: body }).then(function (r) {
      state.scanId = r.scan_id;
      state.lastRoot = root;
      state.open = {}; state.selected = {}; state.offset = 0;
      state.scanRunning = true;
      toast("Scan started — it will keep running until it finishes or you stop it.");
      loadScans();
      pollStatus();
    }).catch(fail);
  }

  function loadScans() {
    return api("/api/scans").then(function (data) {
      var sel = $("fScan");
      sel.innerHTML = "";
      (data.scans || []).forEach(function (s) {
        var o = document.createElement("option");
        o.value = s.id;
        o.textContent = "#" + s.id + " " + s.root + " (" + s.state + ")";
        sel.appendChild(o);
      });
      if (state.scanId) sel.value = state.scanId;
      else if (data.scans && data.scans.length) {
        state.scanId = data.scans[0].id;
        sel.value = state.scanId;
      }
      if (data.scans && data.scans.length) state.lastRoot = state.lastRoot || data.scans[0].root;
    }).catch(function () {});
  }

  // ---------------------------------------------------------------- picker
  function openPicker(path) {
    $("modalPick").classList.remove("hidden");
    api("/api/browse?path=" + encodeURIComponent(path || "")).then(function (data) {
      state.pickPath = data.path || "";
      $("pickPath").textContent = data.path
        ? data.path + (data.file_count ? "  (" + data.file_count + " files here)" : "")
        : "Pick a volume";
      var list = $("pickList");
      list.innerHTML = "";
      if (data.parent) {
        var up = document.createElement("li");
        up.textContent = "⬑ up one level";
        up.dataset.path = data.parent;
        list.appendChild(up);
      } else if (data.path) {
        var roots = document.createElement("li");
        roots.textContent = "⬑ all volumes";
        roots.dataset.path = "";
        list.appendChild(roots);
      }
      (data.dirs || []).forEach(function (d) {
        var li = document.createElement("li");
        li.textContent = "📁 " + d.name;
        li.dataset.path = d.path;
        list.appendChild(li);
      });
      if (!data.dirs || !data.dirs.length) {
        var li2 = document.createElement("li");
        li2.className = "muted";
        li2.textContent = "(no sub-folders — scanning here covers this folder's files)";
        list.appendChild(li2);
      }
      $("btnStartScan").disabled = !data.path;
    }).catch(fail);
  }

  // ---------------------------------------------------------------- settings
  function openSettings() {
    api("/api/config").then(function (c) {
      $("setModel").value = c.ai_model;
      $("setEffort").value = c.ai_effort;
      $("setKey").value = "";
      $("setKey").placeholder = c.anthropic_api_key === "set"
        ? "a key is configured — leave blank to keep it"
        : "not set (or export ANTHROPIC_API_KEY)";
      $("setMaxGroups").value = c.ai_max_groups;
      $("setDelMode").value = c.delete_mode;
      $("setProtect").checked = !!c.protect_last_copy;
      $("setImages").checked = !!c.image_similarity;
      $("setFuzzyMiB").value = Math.round((c.fuzzy_max_bytes || 0) / 1048576);
      $("setExcludes").value = (c.exclude_dirs || []).join("\n");
      $("setRoots").value = (c.roots_allowlist || []).join("\n");
      $("settingsNote").textContent = window.__pillow
        ? "Pillow detected — perceptual image matching is available."
        : "Pillow is not installed, so image similarity is limited to fuzzy hashing.";
      $("modalSettings").classList.remove("hidden");
    }).catch(fail);
  }

  function saveSettings() {
    var body = {
      ai_model: $("setModel").value.trim(),
      ai_effort: $("setEffort").value,
      ai_max_groups: Number($("setMaxGroups").value) || 300,
      delete_mode: $("setDelMode").value,
      protect_last_copy: $("setProtect").checked,
      image_similarity: $("setImages").checked,
      fuzzy_max_bytes: Math.max(0, Number($("setFuzzyMiB").value) || 0) * 1048576,
      exclude_dirs: $("setExcludes").value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean),
      roots_allowlist: $("setRoots").value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean)
    };
    if ($("setKey").value.trim()) body.anthropic_api_key = $("setKey").value.trim();
    api("/api/config", { method: "POST", body: body }).then(function () {
      toast("Settings saved");
      closeModals();
      $("delMode").value = body.delete_mode;
      pollStatus();
    }).catch(fail);
  }

  // ---------------------------------------------------------------- log
  function openLog() {
    api("/api/actions?scan_id=" + (state.scanId || "")).then(function (data) {
      var body = $("logBody");
      body.innerHTML = "";
      if (!data.actions || !data.actions.length) {
        body.innerHTML = '<tr><td class="muted">Nothing has been deleted yet.</td></tr>';
      }
      (data.actions || []).forEach(function (a) {
        var tr = document.createElement("tr");
        var restorable = a.ok && a.dst_path && a.action !== "restore";
        tr.innerHTML =
          "<td>" + esc(fmtDate(a.created_at)) + "</td>" +
          "<td>" + esc(a.action) + "</td>" +
          '<td class="fpath">' + esc(a.src_path) + "</td>" +
          '<td class="num">' + fmtBytes(a.size) + "</td>" +
          "<td>" + (a.ok ? "✔" : '<span style="color:var(--danger)">✕ ' + esc(a.message || "") + "</span>") + "</td>" +
          "<td>" + (restorable
            ? '<button class="ghost small act-restore" data-aid=' + a.id + ">Restore</button>"
            : "") + "</td>";
        body.appendChild(tr);
      });
      $("modalLog").classList.remove("hidden");
    }).catch(fail);
  }

  document.addEventListener("click", function (ev) {
    if (ev.target.classList && ev.target.classList.contains("act-restore")) {
      api("/api/restore", { method: "POST", body: { action_ids: [Number(ev.target.dataset.aid)] } })
        .then(function (r) {
          if (r.restored.length) toast("Restored to " + r.restored[0].path);
          if (r.failed.length) toast(r.failed[0].message, true);
          openLog(); loadGroups();
        }).catch(fail);
    }
  });

  // ---------------------------------------------------------------- modals
  var confirmCallback = null;
  function confirmThen(title, text, cb) {
    $("confirmTitle").textContent = title;
    $("confirmText").textContent = text;
    confirmCallback = cb;
    $("modalConfirm").classList.remove("hidden");
  }
  function closeModals() {
    document.querySelectorAll(".modal").forEach(function (m) { m.classList.add("hidden"); });
  }

  // ---------------------------------------------------------------- boot
  function init() {
    bind();
    $("btnConfirmGo").addEventListener("click", function () {
      var cb = confirmCallback; confirmCallback = null; closeModals();
      if (cb) cb();
    });
    api("/api/config").then(function (c) { $("delMode").value = c.delete_mode; }).catch(function () {});
    loadScans().then(loadGroups);
    pollStatus();
    setInterval(pollStatus, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
