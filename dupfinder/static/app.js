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
    detail: {},               // group id -> files enriched with image metadata
    selected: {},             // file id -> {size, path}
    dryRun: false,
    token: localStorage.getItem("dupfinder_token") || "",
    scanRunning: false,
    lastRoot: null,
    pickPath: "",
    // The two sides of a folder comparison, and what the picker is currently
    // being opened for. Paths are chosen, never typed: on a NAS they are long
    // enough that a text box shows a third of one, and a typo reads as "no
    // duplicates" rather than as a mistake.
    sideA: "",
    sideB: "",
    pickFor: "scan"
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
      renderScanStatus(data.scan, data.resumable);
      renderAiStatus(data.ai);
      window.__pillow = data.pillow;
      window.__video = data.video;
    }).catch(function () { /* transient - keep polling */ });
  }

  // The phase names the server stores are keys; these are what the progress
  // panel already shows for the same steps.
  var PHASE_LABELS = {
    walk: "indexing", size: "grouping by size", quick: "partial hashing",
    md5: "full hashing", verify: "byte-for-byte verification",
    fuzzy: "near-duplicate analysis", group: "building groups",
  };
  function phaseLabel(key) { return PHASE_LABELS[key] || key; }

  function renderScanStatus(scan, resumable) {
    var running = scan.state === "running";
    var panel = $("progress");
    $("btnCancel").classList.toggle("hidden", !running);
    $("btnPause").classList.toggle("hidden", !running);
    $("btnRescan").classList.toggle("hidden", running || !state.lastRoot);
    $("btnPick").disabled = running;

    // What is parked, named rather than implied: a bare "Resume" with no path
    // and no count is a button nobody dares press.
    state.resumable = resumable || null;
    var offer = !running && !!resumable;
    $("btnResume").classList.toggle("hidden", !offer);
    $("resumeBar").classList.toggle("hidden", !offer);
    if (offer) {
      var step = resumable.phase ? phaseLabel(resumable.phase) : "";
      $("resumeWhat").textContent =
        resumable.root
        + (resumable.files_seen ? " · " + fmtNum(resumable.files_seen) + " files indexed" : "")
        + (step ? " · stopped during " + step : "")
        + ". The hashes are on disk; carrying on costs another walk, not the hashing again.";
    }

    if (scan.root) state.lastRoot = scan.root;

    if (running) {
      panel.classList.remove("hidden");
      // A resumed scan starts at step 1 again and looks, from the outside,
      // exactly like one that threw the work away. It did not - the indexing
      // is the cheap part and the hashes are still in the cache - but nothing
      // said so, and "Step 1/7" after an hours-long scan reads as a loss.
      $("progPhase").textContent =
        "Step " + scan.phase_index + "/" + scan.phase_total + " — " + scan.phase_label
        + (scan.resumed ? "  ·  carrying on a paused scan" : "");
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
        // Indexing reads no file contents, so a cache count of zero here is
        // correct and not a sign that the resume failed. Said plainly,
        // because this is the exact moment it looks like it did.
        (scan.resumed && scan.phase === "walk"
          ? '<span class="note">Indexing runs again — it reads no file'
            + ' contents. The hashes stay in the cache and are reused from'
            + ' step 3 on.</span>'
          : "") +
        stat("Groups so far", fmtNum(scan.groups_found)) +
        helperStat(scan.helpers) +
        (scan.note ? '<span class="note">' + esc(scan.note) + "</span>" : "");
    } else {
      panel.classList.add("hidden");
    }

    var sub = $("scanSubtitle");
    if (scan.state === "idle" && !scan.root) {
      sub.textContent = "No scan yet";
    } else {
      var word = { running: "Scanning", done: "Finished", cancelled: "Stopped",
                   paused: "Paused", error: "Failed", idle: "Ready" }[scan.state]
                 || scan.state;
      sub.textContent = word + " · " + (scan.root || "") +
        (scan.state !== "running" && scan.elapsed ? " · " + fmtDuration(scan.elapsed) : "");
    }
    if (scan.error) sub.textContent += " · " + scan.error;

    if (state.scanRunning && !running) {
      state.scanRunning = false;
      state.offset = 0;
      loadScans();
      loadGroups();
      toast(scan.state === "paused"
        ? "Paused — carry on whenever you like, the hashes are kept."
        : scan.state === "cancelled"
        ? "Scan stopped — partial results kept."
        : "Scan finished.");
    }
    state.scanRunning = running;
    if (running && !state.__tick) {
      // Exact groups are published as soon as they are proven, well before the
      // near-duplicate pass finishes, so this refresh actually has something to
      // fetch during a scan. 3s is responsive without hammering a NAS that is
      // busy hashing.
      state.__tick = setInterval(function () {
        if (Date.now() - (state.__lastLoad || 0) > 3000) { state.__lastLoad = Date.now(); loadGroups(true); }
      }, 3000);
    }
    if (!running && state.__tick) { clearInterval(state.__tick); state.__tick = null; }
  }

  // What another machine is contributing, and whether it is still believed.
  // Silent until a helper actually shows up, so a scan that runs here alone
  // does not carry a row of zeroes explaining a feature nobody switched on.
  function helperStat(h) {
    if (!h || !h.enabled) return "";
    if (!h.trusted) {
      return '<span class="note warn">A helper returned wrong signatures. ' +
             'Its answers were discarded and the rest of this scan runs here.</span>';
    }
    if (!h.helpers && !h.done_by_helpers) return "";
    var done = h.done_by_helpers || 0, back = h.taken_back || 0;
    var share = (done + back) ? Math.round(done * 100 / (done + back)) : 0;
    return stat("Helper machines", fmtNum(h.helpers)) +
           stat("Hashed by helper", fmtNum(done) + " (" + share + "%)") +
           (back ? stat("Taken back", fmtNum(back)) : "");
  }

  function stat(label, value) {
    return "<span><strong>" + esc(value) + "</strong> " + esc(label) + "</span>";
  }

  function renderAiStatus(ai) {
    var running = ai.state === "running";
    $("aiProgress").classList.toggle("hidden", !running);

    var b = $("btnSuggest");
    // Only a suggestion run blocks this. A scan used to as well, which meant
    // the results on screen - belonging to a finished scan - could not be
    // acted on because some other scan happened to be busy. The button simply
    // did nothing, and said nothing about why.
    b.disabled = running;

    // And say which of the two things a click does. Without the SDK this is
    // the local rule engine and nothing leaves the NAS; with it, group
    // metadata goes to Claude. The difference is big enough that the button
    // has to name the one it means - the server has told us all along in
    // ai.available, and nothing here ever read it.
    var avail = ai.available || {};
    if (running) {
      b.textContent = "Suggestions running…";
      b.title = "A suggestion run is already in progress.";
    } else if (avail.ok) {
      b.textContent = "AI suggestions";
      b.title = "Sends group metadata - paths, sizes, timestamps, similarity - to "
        + (avail.model || "Claude") + ". File contents are never sent.";
    } else {
      b.textContent = "Suggest (local rules)";
      b.title = (avail.reason || "The Anthropic SDK is unavailable.")
        + " Clicking fills the suggestion column using the local rule engine;"
        + " nothing leaves the NAS.";
    }
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
    // Both or neither: one side alone is the search box above, and sending a
    // half comparison would silently return everything.
    if (state.sideA && state.sideB) { p.set("a", state.sideA); p.set("b", state.sideB); }
    return p.toString();
  }

  function loadGroups(quiet) {
    state.__lastLoad = Date.now();
    // The cached detail carries file statuses, which a delete has just made
    // stale. The quiet refresh during a running scan keeps it: nothing is
    // being deleted then, and dropping it would re-read EXIF every 6 seconds.
    if (!quiet) state.detail = {};
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

  // The preview is a plain <img>, so the browser fetches it itself and the
  // Authorization header we use for the JSON calls is not available. The
  // server accepts the token in the query string for exactly this case.
  function thumbUrl(fid) {
    return "/api/thumb?file_id=" + fid +
      (state.token ? "&token=" + encodeURIComponent(state.token) : "");
  }

  function metaLine(f) {
    var m = f.meta;
    if (!m) return "";
    var bits = [];
    if (m.width && m.height) bits.push(m.width + " × " + m.height);
    if (m.taken) bits.push("taken " + esc(m.taken));
    if (m.camera) bits.push(esc(m.camera));
    if (m.format) bits.push(esc(m.format));
    return bits.length ? '<div class="fmeta">' + bits.join(" · ") + "</div>" : "";
  }

  function detailRow(g) {
    var tr = document.createElement("tr");
    tr.className = "detail";
    // Metadata arrives from /api/group/<id> a moment after the row opens;
    // until then the list we already have is rendered, so nothing flickers
    // into existence except the extra detail.
    var files = state.detail[g.id] || g.files || [];
    var rows = files.map(function (f) {
      var parts = splitPath(f.path);
      // "linked" is not gone: the file is still there and still opens,
      // its bytes are just shared now. Striking it through would be a lie.
      var linked = f.status === "linked";
      var gone = f.status !== "present" && !linked;
      var tag = linked
        ? '<span class="tag linked" title="Shares its bytes with an identical copy - no extra space, every path still works">⇄ linked</span>'
        : (f.action
          ? '<span class="tag ' + f.action + '">' + f.action + "</span>"
          : "");
      var thumb = f.is_image && !gone
        ? '<img class="thumb" loading="lazy" alt="" src="' + thumbUrl(f.id) + '"' +
          ' onerror="this.classList.add(\'broken\')">'
        : "";
      return "<tr>" +
        '<td style="width:28px"><input type="checkbox" class="pick-file" data-fid=' + f.id +
          ' data-size=' + f.size + ' data-path="' + esc(f.path) + '"' +
          (state.selected[f.id] ? " checked" : "") + (gone || linked ? " disabled" : "") + "></td>" +
        '<td class="fpath' + (gone ? " gone" : "") + '"><div class="fwrap">' + thumb +
          '<div class="ftext"><span class="fdir">' + esc(parts[0]) +
          '</span><span class="fname">' + esc(parts[1]) + "</span>" +
          metaLine(f) +
          (f.reason ? '<div class="reason">' + esc(f.reason) + "</div>" : "") +
          (gone ? '<div class="reason">' + esc(f.status) + "</div>" : "") +
          "</div></div></td>" +
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

  // Reading EXIF means opening every image in the group, so it happens once
  // per group on expand rather than for every row of every results page.
  function loadDetail(gid) {
    if (state.detail[gid]) return;
    api("/api/group/" + gid).then(function (d) {
      state.detail[gid] = d.files || [];
      if (state.open[gid]) renderGrid();
    }).catch(function () { /* the row is already usable without the extras */ });
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
    renderDeleteButton();
  }

  // The button has to say which of the two things it does. A red "Delete
  // selected" that quietly only pretends is the worst of both worlds.
  function renderDeleteButton() {
    var b = $("btnDelete");
    b.textContent = state.dryRun ? "Simulate deletion" : "Delete selected";
    b.classList.toggle("danger", !state.dryRun);
    b.classList.toggle("primary", state.dryRun);
    $("actionBar").classList.toggle("simulating", state.dryRun);
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
        if (state.open[gid]) loadDetail(gid);
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
    // The two comparison fields debounce like the search box: a path is typed
    // one character at a time, and a request per keystroke would hammer a
    // query that does a self-join over every group.
    ["fSearch"].forEach(function (id) {
      $(id).addEventListener("input", function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function () { state.offset = 0; loadGroups(); }, 350);
      });
    });
    $("fFolderDepth").addEventListener("change", openFolders);
    $("fScan").addEventListener("change", function () {
      state.scanId = Number(this.value) || null;
      state.offset = 0; state.open = {}; state.detail = {}; state.selected = {};
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

    $("optDryRun").addEventListener("change", function () {
      state.dryRun = this.checked;
      renderDeleteButton();
      // Persist it: this is a safety setting, and one that silently reset on
      // reload would be worse than not having it.
      api("/api/config", { method: "POST", body: { dry_run: state.dryRun } })
        .catch(fail);
    });

    $("btnDelete").addEventListener("click", function () {
      var ids = Object.keys(state.selected).map(Number);
      if (!ids.length) return;
      var mode = $("delMode").value;
      var wording = {
        quarantine: "moved into a .dupfinder-trash folder — you can restore them from the log",
        recycle: "moved into the DSM recycle bin of their share",
        permanent: "deleted permanently and cannot be recovered",
        link: "replaced by a cross-reference to an identical copy — every path keeps working, the bytes exist only once"
      }[mode];
      var n = ids.length + " file" + (ids.length === 1 ? "" : "s");
      var ask = state.dryRun
        ? ["Simulate deleting " + n + "?",
           "Nothing will be touched. Every check runs and the log records what " +
           "a real run in " + mode + " mode would have done."]
        : ["Delete " + n + "?", "They will be " + wording + "."];
      confirmThen(ask[0], ask[1], function () {
        api("/api/delete", { method: "POST", body: {
          file_ids: ids, mode: mode, confirm: true, dry_run: state.dryRun } })
          .then(function (r) {
            var msg = r.dry_run
              ? "Simulated: " + r.deleted.length + " would be removed, " +
                fmtBytes(r.freed_bytes) + " would be freed"
              : r.deleted.length + " removed, " + fmtBytes(r.freed_bytes) + " freed";
            if (r.skipped.length) msg += " · " + r.skipped.length + " kept for safety";
            if (r.failed.length) msg += " · " + r.failed.length + " failed";
            toast(msg, r.failed.length > 0);
            // A simulation changed nothing, so the selection stays put - the
            // point is to look at the log and then decide. A real delete clears
            // the selection AND the header "select all" tick, which renderGrid
            // never touches on its own.
            if (!r.dry_run) {
              state.selected = {};
              var all = $("checkAll");
              if (all) all.checked = false;
            }
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

    $("btnPick").addEventListener("click", function () {
      openPicker(state.lastRoot || "", "scan");
    });

    $("btnPause").addEventListener("click", function () {
      api("/api/scan/pause", { method: "POST", body: {} })
        .then(function () {
          toast("Paused. Everything found so far is kept — resume when you like.");
        }).catch(fail);
    });

    function doResume() {
      var id = state.resumable && state.resumable.scan_id;
      api("/api/scan/resume", { method: "POST", body: { scan_id: id || null } })
        .then(function () {
          toast("Carrying on. The walk runs again; the hashing does not.");
          loadGroups();
        }).catch(fail);
    }
    $("btnResume").addEventListener("click", doResume);
    $("btnResumeBar").addEventListener("click", doResume);

    $("btnDiscard").addEventListener("click", function () {
      var id = state.resumable && state.resumable.scan_id;
      confirmThen(
        "Discard the paused scan?",
        "Its files, hashes and groups stay exactly where they are — only the "
        + "offer to carry on goes away. The hash cache is untouched, so a "
        + "fresh scan over the same folder is still cheap.",
        function () {
          api("/api/scan/discard", { method: "POST", body: { scan_id: id || null } })
            .then(function () { $("resumeBar").classList.add("hidden"); })
            .catch(fail);
        });
    });
    $("btnStartScan").addEventListener("click", function () {
      if (!state.pickPath) { toast("Pick a folder first", true); return; }
      if (state.pickFor === "scan") {
        startScan(state.pickPath);
      } else {
        if (state.pickFor === "a") state.sideA = state.pickPath;
        else state.sideB = state.pickPath;
        renderCompareBar();
        state.offset = 0;
        loadGroups();
      }
      closeModals();
    });

    $("btnCompare").addEventListener("click", function () {
      var bar = $("compareBar");
      // toggle() returns true when the class was ADDED, i.e. when the bar
      // just became hidden.
      var nowHidden = bar.classList.toggle("hidden");
      if (nowHidden) {                     // closing it drops the comparison
        state.sideA = state.sideB = "";
        renderCompareBar();
        state.offset = 0;
        loadGroups();
      } else {
        renderCompareBar();
      }
    });
    $("cbSideA").addEventListener("click", function () {
      openPicker(state.sideA || state.lastRoot || "", "a");
    });
    $("cbSideB").addEventListener("click", function () {
      openPicker(state.sideB || state.lastRoot || "", "b");
    });
    $("cbClear").addEventListener("click", function () {
      state.sideA = state.sideB = "";
      renderCompareBar();
      state.offset = 0;
      loadGroups();
    });
    $("pickList").addEventListener("click", function (ev) {
      var li = ev.target.closest("li");
      if (li) openPicker(li.dataset.path);
    });

    $("btnSettings").addEventListener("click", openSettings);
    $("btnSaveSettings").addEventListener("click", saveSettings);
    $("btnClearDb").addEventListener("click", clearDatabase);
    $("btnFolders").addEventListener("click", openFolders);
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

    // ---------------------------------------------------------------- folders
  function openFolders() {
    var depth = ($("fFolderDepth") || {}).value || "0";
    api("/api/folders?scan_id=" + (state.scanId || "") + "&depth=" + depth)
      .then(function (data) {
      var body = $("folderBody");
      body.innerHTML = "";
      if (!data.pairs || !data.pairs.length) {
        body.innerHTML = '<tr><td class="muted" colspan="4">No folder shares anything with another folder in this scan.</td></tr>';
      }
      (data.pairs || []).forEach(function (p) {
        var tr = document.createElement("tr");
        tr.className = "folder-pair" + (p.related ? " kin" : "");
        tr.dataset.a = p.a;
        tr.innerHTML =
          '<td class="fpath">' +
            (p.related ? '<span class="tag kin" title="' + esc(p.why) + '">\u21c4 same thing, twice</span> ' : "") +
            '<div class="pairline">' + esc(p.a) + '<span class="muted"> \u00b7 ' + fmtNum(p.a_files) + ' files</span></div>' +
            '<div class="pairline">' + esc(p.b) + '<span class="muted"> \u00b7 ' + fmtNum(p.b_files) + ' files</span></div>' +
            (p.related ? '<div class="reason">' + esc(p.why) + "</div>" : "") +
          "</td>" +
          '<td class="num">' + fmtNum(p.shared_files) + "</td>" +
          '<td class="num">' + p.overlap + "%</td>" +
          '<td class="num"><strong>' + fmtBytes(p.shared_bytes) + "</strong></td>";
        body.appendChild(tr);
      });
      $("modalFolders").classList.remove("hidden");
    }).catch(fail);
  }

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
        near_threshold: Number($("optThreshold").value) || 70,
        quick_rescan: $("optQuick").checked
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
  // The chosen paths, shown in full. A half-filled comparison is visibly half
  // filled rather than silently inactive.
  function renderCompareBar() {
    $("cbPathA").textContent = state.sideA || "choose a folder…";
    $("cbPathB").textContent = state.sideB || "choose a folder…";
    $("cbSideA").classList.toggle("set", !!state.sideA);
    $("cbSideB").classList.toggle("set", !!state.sideB);
    $("btnCompare").classList.toggle("active",
      !!(state.sideA && state.sideB));
  }

  function openPicker(path, purpose) {
    if (purpose) state.pickFor = purpose;
    var comparing = state.pickFor !== "scan";
    $("pickTitle").textContent = comparing
      ? "Choose folder " + state.pickFor.toUpperCase() + " to compare"
      : "Choose a folder to scan";
    $("pickScanOptions").classList.toggle("hidden", comparing);
    $("btnStartScan").textContent = comparing ? "Use this folder" : "Scan this folder";
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
      $("setKeepPrefers").value = c.keep_prefers || "oldest";
      $("setResumeOnStart").checked = !!c.resume_on_start;
      $("setProtect").checked = !!c.protect_last_copy;
      $("setImages").checked = !!c.image_similarity;
      $("setQuick").checked = !!c.quick_rescan;
      $("setHelpers").checked = !!c.helpers_enabled;
      $("setFuzzyMiB").value = Math.round((c.fuzzy_max_bytes || 0) / 1048576);
      $("setCtphMinMB").value = Math.round((c.ctph_min_size || 0) / 1048576);
      $("setSkipExts").value = (c.fuzzy_skip_exts || []).join("\n");
      $("setExcludes").value = (c.exclude_dirs || []).join("\n");
      $("setRoots").value = (c.roots_allowlist || []).join("\n");
      $("settingsNote").textContent = settingsNote();
      $("modalSettings").classList.remove("hidden");
      loadDbUsage();
    }).catch(fail);
  }

  // What the two optional pieces can do here. Worth two sentences rather
  // than a tick: a video pass that silently does nothing looks exactly like
  // a library with no re-encodes in it, and the difference matters.
  function settingsNote() {
    var lines = [];
    lines.push(window.__pillow
      ? "Pillow detected — perceptual image matching is available."
      : "Pillow is not installed, so image similarity is limited to fuzzy hashing.");
    var v = window.__video;
    if (v && v.ok) {
      lines.push("ffmpeg and ffprobe found — re-encoded video is matched by frame.");
    } else if (v) {
      lines.push("Video matching is off: " + (v.missing || []).join(", ")
        + " missing. Films re-encoded or at another bitrate will not be found.");
    }
    return lines.join(" ");
  }

  // ------------------------------------------------------- database
  function loadDbUsage() {
    var el = $("dbUsage");
    el.textContent = "Reading…";
    return api("/api/database/usage").then(function (d) {
      var c = d.counts || {};
      state.dbUsage = d;
      el.textContent = fmtBytes(d.bytes_on_disk) + " on disk — "
        + fmtNum(d.scans) + " scan(s), "
        + fmtNum(c.files) + " file rows, "
        + fmtNum((c.hash_cache || 0) + (c.verify_cache || 0)) + " cache entries, "
        + fmtNum(c.actions) + " log entries";
    }).catch(function () { el.textContent = "Could not read the database size."; });
  }

  function clearDatabase() {
    var keep = $("setKeepCache").checked;
    var c = (state.dbUsage && state.dbUsage.counts) || {};
    var lines = [
      "Empty the database?",
      "",
      "This removes " + fmtNum(state.dbUsage && state.dbUsage.scans) + " scan(s) with their"
        + " results and " + fmtNum(c.actions) + " action log entries.",
      keep ? "The hash cache is kept, so the next scan stays fast."
           : "The hash cache goes too, including the paths it records.",
      "",
      "No files are deleted. Anything already in a .dupfinder-trash folder stays"
        + " on disk, but its Restore button goes with the log — you would have to"
        + " move those back by hand.",
      "",
      "This cannot be undone."
    ];
    if (!window.confirm(lines.join("\n"))) return;

    var btn = $("btnClearDb");
    btn.disabled = true;
    btn.textContent = "Emptying…";
    api("/api/database/clear", { method: "POST", body: { keep_cache: keep } })
      .then(function (r) {
        toast("Database emptied — " + fmtNum(r.rows) + " rows removed, "
              + fmtBytes(r.bytes_before) + " → " + fmtBytes(r.bytes_after));
        // Everything on screen refers to rows that no longer exist.
        state.scanId = null;
        state.groups = [];
        state.total = 0;
        state.detail = {};
        state.selected = {};
        // loadGroups rather than renderGrid: the summary tiles above the table
        // are filled from its response, and rendering alone left them showing
        // the counts of groups that no longer exist.
        loadGroups();
        loadDbUsage();
        loadScans();
        pollStatus();
      })
      .catch(fail)
      .then(function () {
        btn.disabled = false;
        btn.textContent = "Empty the database";
      });
  }

  function saveSettings() {
    var body = {
      ai_model: $("setModel").value.trim(),
      ai_effort: $("setEffort").value,
      ai_max_groups: Number($("setMaxGroups").value) || 300,
      delete_mode: $("setDelMode").value,
      keep_prefers: $("setKeepPrefers").value,
      resume_on_start: $("setResumeOnStart").checked,
      protect_last_copy: $("setProtect").checked,
      image_similarity: $("setImages").checked,
      quick_rescan: $("setQuick").checked,
      helpers_enabled: $("setHelpers").checked,
      fuzzy_max_bytes: Math.max(0, Number($("setFuzzyMiB").value) || 0) * 1048576,
      ctph_min_size: Math.max(0, Number($("setCtphMinMB").value) || 0) * 1048576,
      fuzzy_skip_exts: $("setSkipExts").value.split("\n")
        .map(function (s) { return s.trim().toLowerCase(); })
        .filter(Boolean)
        .map(function (s) { return s.charAt(0) === "." ? s : "." + s; }),
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
        // A simulated entry has a destination recorded but nothing ever moved
        // there, so it must never offer a Restore button.
        var simulated = String(a.action).indexOf("simulate:") === 0;
        var restorable = a.ok && a.dst_path && a.action !== "restore" && !simulated;
        if (simulated) tr.className = "simulated";
        tr.innerHTML =
          "<td>" + esc(fmtDate(a.created_at)) + "</td>" +
          "<td>" + (simulated
            ? '<span class="tag sim">simulated</span> ' + esc(String(a.action).slice(9))
            : esc(a.action)) + "</td>" +
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
    var pair = ev.target.closest && ev.target.closest("tr.folder-pair");
    if (pair) {
      // Jump straight to the groups behind this pair rather than leaving the
      // reader to copy a path by hand.
      $("fSearch").value = pair.dataset.a;
      closeModals();
      state.offset = 0;
      loadGroups();
      return;
    }
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
    api("/api/config").then(function (c) {
      $("delMode").value = c.delete_mode;
      state.dryRun = !!c.dry_run;
      $("optDryRun").checked = state.dryRun;
      renderDeleteButton();
    }).catch(function () {});
    loadScans().then(loadGroups);
    pollStatus();
    setInterval(pollStatus, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
