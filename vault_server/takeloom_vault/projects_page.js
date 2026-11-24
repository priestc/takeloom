// The Setlists page (web.py's projects_page): a recording node's setlists
// (a "project" in the code is a setlist file), edited through the vault (POST /nodes/<id>/rpc, which runs
// takeloom's node_rpc.py there). The setlist is the node's: every change
// is saved straight back to it (save_setlist), as the Record tab does.
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var node = null, project = "", setlist = null, previews = [], busy = false;

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === "text") e.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return e;
  }
  function status(text, bad) { var s = $("pj-status"); s.textContent = text || ""; s.style.color = bad ? "#d33" : ""; }
  function rpc(op, args) {
    if (!node) return Promise.reject(new Error("No recording node is connected to the vault."));
    return fetch("/nodes/" + node + "/rpc", {method: "POST", headers: {"Content-Type": "application/json"},
                                              body: JSON.stringify({op: op, args: args || {}})})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; }); });
  }
  function fmt(s) { s = Math.round(s || 0); return s ? Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0") : ""; }
  function songName(t) { return (t.artist || "Unknown") + " - " + (t.title || "Unknown") + (t.year ? " (" + t.year + ")" : ""); }
  function avgDuration(songs) {
    var d = songs.map(function (s) { return +s.duration || 0; }).filter(function (x) { return x > 0; });
    return d.length ? d.reduce(function (a, b) { return a + b; }, 0) / d.length : 0;
  }

  // --- the node ---

  function loadNodes() {
    return fetch("/cameras/data").then(function (r) { return r.json(); }).then(function (j) {
      var sel = $("pj-node"), keep = node;
      sel.innerHTML = "";
      j.nodes.forEach(function (n) { sel.appendChild(el("option", {value: n.id, text: n.label + (n.active ? " (in charge)" : "")})); });
      var pick = j.nodes.find(function (n) { return n.id === keep; }) || j.nodes.find(function (n) { return n.active; }) || j.nodes[0];
      node = pick ? pick.id : null;
      if (pick) sel.value = pick.id;
      $("pj-none").style.display = node ? "none" : "";
      $("pj-main").style.display = node ? "" : "none";
      return node !== keep;
    });
  }

  // --- projects ---

  function loadProjects() {
    return rpc("list_projects").then(function (j) {
      var sel = $("pj-project"), keep = project || localStorage.getItem("tl-project") || "";
      sel.innerHTML = "";
      j.projects.forEach(function (p) { sel.appendChild(el("option", {value: p, text: p})); });
      project = j.projects.indexOf(keep) >= 0 ? keep : (j.projects[0] || "");
      sel.value = project;
      return loadSetlist();
    }).catch(function (e) { status("Couldn't list the setlists: " + e.message, true); });
  }
  function loadSetlist() {
    if (!project) { setlist = null; render(); return Promise.resolve(); }
    try { localStorage.setItem("tl-project", project); } catch (e) { /* private mode */ }
    status("Loading…");
    return rpc("get_setlist", {project_name: project}).then(function (j) {
      setlist = j.setlist; previews = [];
      render(); status("");
      rpc("get_filter_slot_previews", {project_name: project}).then(function (p) { previews = p.previews || []; render(); }, function () {});
    }).catch(function (e) { status("Couldn't load the setlist: " + e.message, true); });
  }
  function save(refreshPreviews) {
    busy = true; status("Saving…");
    return rpc("is_recording").then(function (r) {
      if (r.recording) throw new Error("the node is recording; change the setlist once the session's over");
      return rpc("save_setlist", {project_name: project, setlist: setlist});
    }).then(function () {
      busy = false; status("Saved."); setTimeout(function () { if (!busy) status(""); }, 2000);
      if (refreshPreviews) rpc("get_filter_slot_previews", {project_name: project}).then(function (p) { previews = p.previews || []; render(); }, function () {});
      return true;
    }).catch(function (e) { busy = false; status("Not saved: " + e.message, true); loadSetlist(); return false; });
  }

  // --- the setlist ---

  function render() {
    var body = $("pj-tracks");
    body.innerHTML = "";
    if (titled.length) renderTitled();
    if (!setlist) return;
    var tracks = setlist.tracks || [];
    $("pj-count").textContent = tracks.length + " track" + (tracks.length === 1 ? "" : "s");
    var setSel = $("ss-target");
    setSel.innerHTML = "";
    setSel.appendChild(el("option", {value: "", text: "a new song set"}));
    tracks.forEach(function (t, i) {
      var isSet = t.is_inspiration_filter;
      if (isSet) setSel.appendChild(el("option", {value: String(i), text: "“" + t.name + "”"}));
      var source = isSet ? "song set · " + (t.song_set || []).length + " songs" :
        t.inspiration_track_id ? "inspiration" : (t.source || "upload");
      var takes = Object.keys(t.preferred_takes || {});
      var preview = previews[i];
      var next = preview && preview.next_up ? Object.keys(preview.next_up).map(function (k) {
        return k + ": " + (preview.next_up[k] || "—"); }).join(" · ") : "";
      var nameCell = el("td", {}, [el("b", {text: t.name}),
        takes.length ? el("span", {"class": "muted small", text: "  takes: " + takes.join(", ")}) : null,
        next ? el("div", {"class": "muted small", text: "next up " + next}) : null]);
      var actions = el("td", {style: "white-space:nowrap"}, [
        el("button", {text: "↑", title: "Move up", onclick: function () { move(i, -1); }}),
        el("button", {text: "↓", title: "Move down", onclick: function () { move(i, 1); }}),
        isSet ? el("button", {text: "Songs", onclick: function () { toggleSongs(i); }}) :
          el("button", {text: "Rename", onclick: function () { rename(i); }}),
        el("button", {text: "Delete", onclick: function () { remove(i); }})]);
      var depth = isSet ? el("input", {type: "number", min: "1", max: String(Math.max(1, (t.song_set || []).length)),
                                       value: String(t.depth || 1), style: "width:3.5em", title: "Songs from this set played back to back",
                                       onchange: function (ev) { t.depth = Math.max(1, +ev.target.value || 1); save(); }}) : null;
      body.appendChild(el("tr", {}, [el("td", {"class": "muted", text: String(i + 1)}), nameCell,
        el("td", {"class": "muted small"}, [source, depth ? el("span", {}, [" · depth ", depth]) : null]),
        el("td", {"class": "muted small", text: fmt(t.duration_seconds)}), actions]));
      if (isSet && openSongs[i]) {
        body.appendChild(el("tr", {}, [el("td"), el("td", {colspan: "4"}, (t.song_set || []).map(function (s, k) {
          return el("div", {"class": "small"}, [el("button", {text: "×", title: "Remove from the set", onclick: function () {
            if (t.song_set.length <= 1) { status("A song set needs at least one song: delete the set instead.", true); return; }
            t.song_set.splice(k, 1); t.duration_seconds = avgDuration(t.song_set);
            t.depth = Math.min(t.depth || 1, t.song_set.length); save(true); render(); }}), " " + songName(s) + (s.duration ? "  " + fmt(s.duration) : "")]);
        }))]));
      }
    });
  }
  var openSongs = {};
  function toggleSongs(i) { openSongs[i] = !openSongs[i]; render(); }
  function move(i, d) {
    var t = setlist.tracks, j = i + d;
    if (j < 0 || j >= t.length) return;
    var x = t.splice(i, 1)[0]; t.splice(j, 0, x);
    if (previews.length === t.length) { var p = previews.splice(i, 1)[0]; previews.splice(j, 0, p); }
    openSongs = {}; render(); save();
  }
  function rename(i) {
    var t = setlist.tracks[i], name = prompt("Track name:", t.name);
    name = (name || "").trim();
    if (!name || name === t.name) return;
    t.name = name; render(); save();
  }
  function remove(i) {
    if (!confirm("Remove “" + setlist.tracks[i].name + "” from the setlist?")) return;
    setlist.tracks.splice(i, 1); previews.splice(i, 1); openSongs = {}; render(); save();
  }

  // --- adding ---

  function added(what) { return function () { status("Added " + what + "."); loadSetlist(); }; }
  function failed(e) { status("Couldn't add it: " + e.message, true); }

  function addFile() {
    var f = $("add-file").files[0];
    if (!f) { status("Pick a file first.", true); return; }
    status("Sending " + f.name + " to the node…");
    fetch("/nodes/" + node + "/upload?name=" + encodeURIComponent(f.name), {method: "POST", body: f})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; }); })
      .then(function (j) {
        status("Adding " + f.name + "…");
        return rpc("add_local_backing_track", {project_name: project, upload: j.upload,
                                               track_name: $("add-file-name").value.trim() || null});
      }).then(added(f.name), failed);
  }
  function addYoutube() {
    var url = $("add-yt").value.trim();
    if (!url) { status("Paste a YouTube URL first.", true); return; }
    status("Downloading from YouTube on the node (this can take a few minutes)…");
    rpc("add_youtube_backing_track", {project_name: project, url: url}).then(added("the YouTube video"), failed);
  }

  // Inspiration: Artist and Title autocomplete, then add the exact track.
  var picked = null, timers = {};
  function debounce(key, fn) { clearTimeout(timers[key]); timers[key] = setTimeout(fn, 250); }
  function suggest(list, items, onPick) {
    list.innerHTML = "";
    items.slice(0, 12).forEach(function (it) {
      list.appendChild(el("div", {"class": "sugg", text: it.text, onmousedown: function (ev) { ev.preventDefault(); onPick(it); list.innerHTML = ""; }}));
    });
  }
  function onArtist() {
    var v = $("insp-artist").value.trim();
    debounce("a", function () {
      if (!v) { $("insp-artist-list").innerHTML = ""; return; }
      rpc("search_inspiration_artists", {partial: v}).then(function (j) {
        suggest($("insp-artist-list"), (j.suggestions || []).map(function (a) { return {text: a}; }),
                function (it) { $("insp-artist").value = it.text; });
      }, function () {});
    });
  }
  function onTitle() {
    picked = null;
    var v = $("insp-title").value.trim();
    debounce("t", function () {
      if (!v) { $("insp-title-list").innerHTML = ""; return; }
      rpc("search_inspiration_titles", {partial: v, artist: $("insp-artist").value.trim()}).then(function (j) {
        suggest($("insp-title-list"), (j.suggestions || []).map(function (t) { return {text: songName(t), track: t}; }),
                function (it) { picked = it.track; $("insp-title").value = it.track.title; $("insp-artist").value = it.track.artist || ""; });
      }, function () {});
    });
  }
  function addInspiration() {
    var go = picked ? Promise.resolve({track: picked}) :
      rpc("find_inspiration_track", {artist: $("insp-artist").value.trim(), title: $("insp-title").value.trim()});
    status("Adding from the inspiration server…");
    go.then(function (j) { return rpc("add_inspiration_track_by_id", {project_name: project, track_info: j.track}); })
      .then(added("the song"), failed);
  }

  // Song sets: tracks matching a filter, then a new set or into an existing one.
  var found = [];
  function searchSet() {
    var c = {};
    ["artist", "genre", "year_min", "year_max", "duration_min", "duration_max"].forEach(function (k) {
      var v = $("ss-" + k).value.trim();
      if (v) c[k] = /min|max/.test(k) ? +v : v;
    });
    status("Searching the inspiration server…");
    rpc("search_inspiration_by_filter", {filter_criteria: c, all_matches: $("ss-all").checked}).then(function (j) {
      found = j.tracks || [];
      var box = $("ss-results");
      box.innerHTML = "";
      found.forEach(function (t, i) {
        box.appendChild(el("label", {"class": "small", style: "display:block"},
          [el("input", {type: "checkbox", checked: "checked", "data-i": String(i)}), " " + songName(t) + (t.duration ? "  " + fmt(t.duration) : "")]));
      });
      status(found.length + " match" + (found.length === 1 ? "" : "es") + ".");
    }, function (e) { status("Search failed: " + e.message, true); });
  }
  function addSet() {
    var songs = Array.prototype.slice.call($("ss-results").querySelectorAll("input:checked")).map(function (x) { return found[+x.getAttribute("data-i")]; });
    if (!songs.length) { status("Tick at least one song.", true); return; }
    var target = $("ss-target").value;
    if (target !== "") {
      var t = setlist.tracks[+target], have = {};
      (t.song_set || []).forEach(function (s) { have[s.id] = 1; });
      songs.forEach(function (s) { if (!have[s.id]) t.song_set.push(s); });
      t.duration_seconds = avgDuration(t.song_set);
      render(); save(true); return;
    }
    var label = $("ss-label").value.trim();
    if (!label) { status("Give the new song set a name.", true); return; }
    status("Adding the song set…");
    rpc("add_song_set_slot", {project_name: project, label: label, songs: songs}).then(added("the song set"), failed);
  }

  // By working title: a song that already has completed takes and a
  // working title (GET /completed/titled), added as the same setlist entry
  // it is elsewhere, backing track and takes included.
  var titled = [];
  function loadTitled() {
    fetch("/completed/titled").then(function (r) { return r.json(); }).then(function (j) {
      titled = j.songs || [];
      renderTitled();
    }).catch(function (e) { $("wt-list").textContent = "Couldn't load them: " + e.message; });
  }
  function renderTitled() {
    var box = $("wt-list"), f = $("wt-filter").value.trim().toLowerCase();
    var have = {};
    ((setlist && setlist.tracks) || []).forEach(function (t) { have[t.name] = 1; });
    box.innerHTML = "";
    var shown = titled.filter(function (s) {
      return !f || s.working_title.toLowerCase().indexOf(f) >= 0 || s.name.toLowerCase().indexOf(f) >= 0;
    });
    if (!titled.length) { box.innerHTML = "<p class='muted small'>No song has both completed takes and a working title yet.</p>"; return; }
    shown.forEach(function (s) {
      var already = !!have[s.name];
      var label = el("label", {"class": "small", style: "display:block" + (already ? ";opacity:.5" : "")}, [
        el("input", {type: "checkbox", "data-name": s.name}), " ", el("b", {text: s.working_title}),
        el("span", {"class": "muted", text: "  " + (s.name !== s.working_title ? s.name + " · " : "") +
          s.instruments.join(", ") + (already ? " · already in this setlist" : "")})]);
      if (already) label.querySelector("input").disabled = true;
      box.appendChild(label);
    });
  }
  function addTitled() {
    var names = Array.prototype.slice.call($("wt-list").querySelectorAll("input:checked")).map(function (x) { return x.getAttribute("data-name"); });
    if (!names.length) { status("Tick at least one song.", true); return; }
    if (!setlist) return;
    var have = {};
    setlist.tracks.forEach(function (t) { have[t.name] = 1; });
    var added = 0;
    names.forEach(function (n) {
      var s = titled.find(function (x) { return x.name === n; });
      if (s && !have[n]) { setlist.tracks.push(JSON.parse(JSON.stringify(s.entry))); have[n] = 1; added++; }
    });
    render(); renderTitled();
    save().then(function (ok) { if (ok && added) status("Added " + added + " song" + (added === 1 ? "" : "s") + "."); });
  }

  function newProject() {
    var name = (prompt("New setlist's name:") || "").trim();
    if (!name) return;
    rpc("create_project", {name: name}).then(function (j) { project = j.name || name; return loadProjects(); }, function (e) { status("Couldn't create it: " + e.message, true); });
  }

  $("pj-node").addEventListener("change", function () { node = $("pj-node").value; project = ""; loadProjects(); });
  $("pj-project").addEventListener("change", function () { project = $("pj-project").value; openSongs = {}; loadSetlist(); });
  $("pj-new").addEventListener("click", newProject);
  $("pj-reload").addEventListener("click", function () { loadNodes().then(loadProjects); });
  $("add-file-go").addEventListener("click", addFile);
  $("add-yt-go").addEventListener("click", addYoutube);
  $("insp-artist").addEventListener("input", onArtist);
  $("insp-title").addEventListener("input", onTitle);
  ["insp-artist", "insp-title"].forEach(function (id) { $(id).addEventListener("blur", function () { setTimeout(function () { $(id + "-list").innerHTML = ""; }, 150); }); });
  $("insp-go").addEventListener("click", addInspiration);
  $("ss-search").addEventListener("click", searchSet);
  $("ss-go").addEventListener("click", addSet);
  $("wt-go").addEventListener("click", addTitled);
  $("wt-filter").addEventListener("input", renderTitled);
  Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(document.querySelectorAll(".tabs button"), function (x) { x.classList.toggle("on", x === b); });
      Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) { t.style.display = t.id === b.getAttribute("data-tab") ? "" : "none"; });
      if (b.getAttribute("data-tab") === "t-wt") loadTitled();
    });
  });
  loadNodes().then(loadProjects);
  setInterval(function () { loadNodes().then(function (changed) { if (changed && node) loadProjects(); }); }, 5000);
})();
