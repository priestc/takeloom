// The Completed Takes page (web.py's completed_page): the song list and a
// mixer that plays every take on a song together in the browser (Web
// Audio), from GET /completed/data. What takeloom's Completed Takes tab
// was (ui/completed_takes.py, ui/song_mixer.py, ui/backing_trim_editor.py).
(function () {
  "use strict";
  var COLORS = {
    "acoustic-guitar": "#2E7D32", "electric-guitar": "#43A047", "electric-bass": "#1565C0",
    "electric-bass-fretless": "#0D47A1", "drums": "#E65100", "midi-keyboard": "#C62828",
    "piano": "#C62828", "organ": "#AD1457"
  };
  var BACKING = "backing track", MAX_GAIN = 2.0;
  var SOURCES = {inspiration: "Inspiration", youtube: "YouTube", upload: "Direct upload"};
  var $ = function (id) { return document.getElementById(id); };
  var data = {songs: [], projects: {}, pending: 0};
  var expanded = {};
  var ctx = null;
  var mixer = null; // {song, strips: [{key, label, filename|backing, buffer, gain, muted, solo, node}], ...}

  function el(tag, attrs, children) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) {
      if (k === "text") e.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), attrs[k]);
      else e.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }
  function badge(label) {
    var b = el("span", {"class": "badge", text: label});
    b.style.background = label === BACKING ? "#444" : (COLORS[label] || "#616161");
    return b;
  }
  function media(rel) { return "/media/" + rel.split("/").map(encodeURIComponent).join("/"); }
  function fmt(s) {
    s = Math.max(0, s || 0);
    var m = Math.floor(s / 60), r = Math.floor(s % 60);
    return m + ":" + (r < 10 ? "0" : "") + r;
  }
  function ago(t) {
    if (!t) return "—";
    var d = Date.now() / 1000 - t;
    if (d < 60) return "just now";
    var units = [[60, "m"], [24, "h"], [30, "d"], [12.17, "mo"]], n = d / 60;
    for (var i = 0; i < units.length; i++) {
      if (n < units[i][0]) return Math.floor(n) + units[i][1] + " ago";
      n /= units[i][0];
    }
    return Math.floor(n) + "y ago";
  }
  function post(url, body) {
    return fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; }); });
  }
  function status(text) { if ($("mx-status")) $("mx-status").textContent = text || ""; }

  // --- the song list ---

  function load(then) {
    $("ct-loaded").textContent = "Loading…";
    fetch("/completed/data").then(function (r) { return r.json(); }).then(function (j) {
      data = j;
      $("ct-loaded").textContent = "Loaded " + new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
      $("ct-pending").textContent = j.pending ? j.pending + " change" + (j.pending === 1 ? "" : "s") +
        " waiting for takeloom to pick up (on its next sync, or when a session opens)." : "";
      var sel = $("ct-project"), keep = sel.value;
      sel.innerHTML = "";
      sel.appendChild(el("option", {value: "", text: "All projects"}));
      Object.keys(j.projects).sort().forEach(function (p) { sel.appendChild(el("option", {value: p, text: p})); });
      sel.value = keep in j.projects ? keep : "";
      renderList();
      if (then) then();
    }).catch(function (e) { $("ct-loaded").textContent = "Couldn't load: " + e.message; });
  }

  function renderList() {
    var title = $("ct-title").value.trim().toLowerCase(), project = $("ct-project").value;
    var inProject = project ? new Set(data.projects[project] || []) : null;
    var body = $("ct-songs");
    body.innerHTML = "";
    var shown = 0;
    data.songs.forEach(function (song) {
      if (title && song.name.toLowerCase().indexOf(title) < 0) return;
      if (inProject && !inProject.has(song.name)) return;
      shown++;
      var open = !!expanded[song.name];
      var toggle = el("span", {"class": "tri", text: open ? "▾" : "▸", onclick: function (ev) {
        ev.stopPropagation(); expanded[song.name] = !open; renderList();
      }});
      var badges = el("td", {}, song.takes.map(function (t) { return badge(t.instrument); }));
      var row = el("tr", {"class": "song" + (mixer && mixer.song.name === song.name ? " on" : ""),
                          onclick: function () { loadSong(song.name, true); }}, [
        el("td", {}, [toggle, el("b", {text: song.name})]),
        el("td", {"class": "muted small", text: SOURCES[song.backing_source] || "—"}),
        badges
      ]);
      body.appendChild(row);
      if (open) {
        song.takes.forEach(function (t) {
          body.appendChild(el("tr", {"class": "take"}, [
            el("td", {colspan: "3"}, [badge(t.instrument),
              el("span", {text: " take " + t.take_number + " · " + ago(t.recorded_at) + (t.has_video ? " · video" : "") +
                (t.available ? "" : " · audio not in the vault")})])
          ]));
        });
      }
    });
    $("ct-count").textContent = shown + (shown === data.songs.length ? "" : " of " + data.songs.length);
  }

  // --- the mixer ---

  function songNamed(name) { return data.songs.find(function (s) { return s.name === name; }); }

  function loadSong(name, autoplay) {
    var song = songNamed(name);
    stop();
    if (!song) { mixer = null; renderMixer(); return; }
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    var mix = song.mix || {volumes: {}, muted: []};
    var labels = song.takes.map(function (t) { return t.instrument; });
    var strips = song.takes.filter(function (t) { return t.available; }).map(function (t, i) {
      var key = labels.filter(function (l) { return l === t.instrument; }).length === 1 ? t.instrument : t.instrument + " #" + (i + 1);
      return {key: key, label: t.instrument, take: t, url: media("completed_takes/" + t.filename)};
    });
    var skipped = song.takes.filter(function (t) { return !t.available; }).map(function (t) { return t.instrument + ": not in the vault"; });
    if (song.backing_track) strips.push({key: BACKING, label: BACKING, backing: true,
      url: "/completed/backing/" + encodeURIComponent(song.backing_track)});
    strips.forEach(function (s) {
      s.gain = Math.min(MAX_GAIN, Math.max(0, mix.volumes[s.key] != null ? mix.volumes[s.key] : 1));
      s.muted = mix.muted.indexOf(s.key) >= 0; s.solo = false;
    });
    var token = {};
    mixer = {song: song, strips: strips, token: token, playing: false, offset: song.trim_start_seconds, full: 0,
             trimStart: song.trim_start_seconds, trimEnd: song.trim_end_seconds, saved: null, skipped: skipped,
             base: song.mix ? "Saved mix applied" + (song.mix.saved_at ? " (" + new Date(song.mix.saved_at * 1000).toLocaleString() + ")" : "") : "No saved mix yet"};
    mixer.saved = stateKey();
    renderMixer();
    renderList();
    status("Loading takes…");
    Promise.all(strips.map(function (s) {
      return fetch(s.url).then(function (r) {
        if (!r.ok) throw new Error(r.status + " " + r.statusText);
        return r.arrayBuffer();
      }).then(function (buf) { return ctx.decodeAudioData(buf); })
        .then(function (b) { s.buffer = b; }, function (e) { s.error = e.message || String(e); });
    })).then(function () {
      if (mixer.token !== token) return;
      strips.forEach(function (s) { if (s.error) skipped.push(s.label + ": " + s.error); });
      mixer.strips = strips.filter(function (s) { return s.buffer; });
      mixer.full = Math.max.apply(null, [0].concat(mixer.strips.map(function (s) { return s.buffer.duration; })));
      renderMixer();
      if (!mixer.strips.length) { status("None of this song's takes could be played" + (skipped.length ? ": " + skipped.join("; ") : ".")); return; }
      showBase();
      if (autoplay) play();
    });
  }

  function effectiveGains() {
    var anySolo = mixer.strips.some(function (s) { return s.solo; });
    return mixer.strips.map(function (s) { return s.muted || (anySolo && !s.solo) ? 0 : s.gain; });
  }
  function stateKey() {
    return JSON.stringify(mixer.strips.map(function (s) { return [s.key, Math.round(s.gain * 1000), s.muted]; }).sort());
  }
  function showBase() {
    var dirty = stateKey() !== mixer.saved;
    status(dirty ? "Unsaved changes" : mixer.base + (mixer.skipped.length ? " — skipped " + mixer.skipped.join("; ") : ""));
  }
  function windowEnd() { return Math.max(mixer.trimStart, mixer.full - mixer.trimEnd); }
  function position() {
    if (!mixer) return 0;
    return mixer.playing ? mixer.offset + (ctx.currentTime - mixer.startedAt) : mixer.offset;
  }
  function play(from) {
    if (!mixer || !mixer.strips.length) return;
    if (ctx.state === "suspended") ctx.resume();
    stopSources();
    var at = from != null ? from : position();
    if (at < mixer.trimStart || at >= windowEnd() - 0.05) at = mixer.trimStart;
    var gains = effectiveGains(), when = ctx.currentTime + 0.05;
    mixer.sources = mixer.strips.map(function (s, i) {
      var src = ctx.createBufferSource(), g = ctx.createGain();
      src.buffer = s.buffer; g.gain.value = gains[i];
      src.connect(g); g.connect(ctx.destination);
      s.node = g;
      if (at < s.buffer.duration) src.start(when, at, windowEnd() - at);
      return src;
    });
    mixer.offset = at; mixer.startedAt = when; mixer.playing = true;
    tick();
  }
  function stopSources() {
    if (mixer && mixer.sources) mixer.sources.forEach(function (s) { try { s.stop(); } catch (e) { /* never started */ } });
    if (mixer) mixer.sources = null;
  }
  function pause() { if (!mixer) return; mixer.offset = position(); mixer.playing = false; stopSources(); tick(); }
  function stop() { if (mixer) { stopSources(); mixer.playing = false; } }
  function applyGains() {
    if (!mixer) return;
    var gains = effectiveGains();
    mixer.strips.forEach(function (s, i) { if (s.node) s.node.gain.setTargetAtTime(gains[i], ctx.currentTime, 0.01); });
    mixer.strips.forEach(function (s) { if (s.pct) s.pct.textContent = Math.round(s.gain * 100) + "%"; });
    showBase();
  }
  function tick() {
    if (!mixer) return;
    var pos = position();
    if (mixer.playing && pos >= windowEnd()) { mixer.playing = false; mixer.offset = mixer.trimStart; stopSources(); pos = mixer.offset; }
    var seek = $("mx-seek");
    if (seek && !seek.dragging) {
      seek.min = mixer.trimStart; seek.max = windowEnd() || 1; seek.value = pos;
    }
    if ($("mx-time")) $("mx-time").textContent = fmt(pos - mixer.trimStart) + " / " + fmt(windowEnd() - mixer.trimStart);
    if ($("mx-play")) $("mx-play").textContent = mixer.playing ? "❚❚ Pause" : "▶ Play";
    if (mixer.playing) requestAnimationFrame(tick);
  }

  function takeChooser(label, takes, current) {
    var sel = el("select", {"class": "takesel"});
    takes.forEach(function (t) { sel.appendChild(el("option", {value: t.filename, text: "take " + t.take_number})); });
    sel.appendChild(el("option", {value: "", text: "none"}));
    sel.value = current || "";
    sel.addEventListener("change", function () {
      var anyTake = mixer.song.takes[0].filename, wasPlaying = mixer.playing, name = mixer.song.name;
      status("Switching " + label + " take…");
      post("/completed/preferred-take", {take_filename: anyTake, instrument: label, new_filename: sel.value || null})
        .then(function () { load(function () { loadSong(name, wasPlaying); }); })
        .catch(function (e) { status("Could not change take: " + e.message); sel.value = current || ""; });
    });
    return sel;
  }

  function renderMixer() {
    var box = $("mixer");
    box.innerHTML = "";
    if (!mixer) {
      box.appendChild(el("p", {"class": "muted", text: "Click a song to load its takes here."}));
      return;
    }
    var song = mixer.song;
    box.appendChild(el("h2", {text: song.name}));
    var seek = el("input", {type: "range", id: "mx-seek", step: "0.01", min: "0", max: "1", value: "0"});
    seek.addEventListener("input", function () { seek.dragging = true; $("mx-time").textContent = fmt(seek.value - mixer.trimStart); });
    seek.addEventListener("change", function () {
      seek.dragging = false;
      var v = parseFloat(seek.value);
      if (mixer.playing) play(v); else { mixer.offset = v; tick(); }
    });
    box.appendChild(el("div", {"class": "transport"}, [
      el("button", {id: "mx-play", "class": "ok", text: "▶ Play", onclick: function () { mixer.playing ? pause() : play(); }}),
      seek, el("span", {id: "mx-time", "class": "small muted"})
    ]));
    var strips = el("div", {"class": "strips"});
    var ordered = mixer.strips.filter(function (s) { return s.backing; }).concat(mixer.strips.filter(function (s) { return !s.backing; }));
    ordered.forEach(function (s) {
      var pct = el("div", {"class": "pct", text: Math.round(s.gain * 100) + "%"});
      s.pct = pct;
      var fader = el("input", {type: "range", "class": "fader", min: "0", max: String(MAX_GAIN), step: "0.01", value: String(s.gain)});
      fader.addEventListener("input", function () { s.gain = parseFloat(fader.value); applyGains(); });
      fader.addEventListener("dblclick", function () { s.gain = 1; fader.value = "1"; applyGains(); });
      var mute = el("input", {type: "checkbox"}); mute.checked = s.muted;
      mute.addEventListener("change", function () { s.muted = mute.checked; applyGains(); });
      var solo = el("input", {type: "checkbox"});
      solo.addEventListener("change", function () { s.solo = solo.checked; applyGains(); });
      strips.appendChild(el("div", {"class": "strip"}, [
        pct, fader,
        el("label", {}, [mute, document.createTextNode(" Mute")]),
        el("label", {}, [solo, document.createTextNode(" Solo")]),
        badge(s.label),
        s.backing ? el("div", {"class": "muted small", text: "track"}) : takeChooser(s.label, s.take.alternates, s.take.filename)
      ]));
    });
    // Labels with takes on file but none preferred: just the chooser, to pick one again.
    Object.keys(song.unpreferred || {}).forEach(function (label) {
      strips.appendChild(el("div", {"class": "strip empty"}, [badge(label), takeChooser(label, song.unpreferred[label], null)]));
    });
    box.appendChild(strips);
    var exportLink = el("span", {id: "mx-export", "class": "small"});
    box.appendChild(el("div", {"class": "buttons"}, [
      el("button", {text: "Save mix", onclick: saveMix}),
      el("button", {text: "Reset to 100%", onclick: function () {
        mixer.strips.forEach(function (s) { s.gain = 1; s.muted = false; s.solo = false; });
        renderMixer(); applyGains();
      }}),
      el("button", {id: "mx-export-btn", text: "Export mix…", onclick: exportMix}),
      exportLink
    ]));
    box.appendChild(el("p", {id: "mx-status", "class": "muted small"}));
    box.appendChild(trimEditor());
    showBase();
    tick();
  }

  function saveMix() {
    var volumes = {}, muted = [];
    mixer.strips.forEach(function (s) { volumes[s.key] = Math.round(s.gain * 1000) / 1000; if (s.muted) muted.push(s.key); });
    var m = mixer, state = stateKey();
    status("Saving…");
    post("/completed/mix", {track_name: m.song.name, volumes: volumes, muted: muted}).then(function (saved) {
      m.song.mix = saved;
      if (mixer !== m) return;
      m.saved = state;
      m.base = "Mix saved " + new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
      showBase();
    }).catch(function (e) { status("Could not save: " + e.message); });
  }

  function exportMix() {
    var gains = effectiveGains(), m = mixer;
    var tracks = m.strips.map(function (s, i) {
      return s.backing ? {backing: true, backing_track: m.song.backing_track, gain: gains[i]}
                       : {filename: s.take.filename, label: s.label, gain: gains[i]};
    });
    $("mx-export-btn").disabled = true;
    $("mx-export").textContent = " Exporting from the original recordings…";
    post("/completed/export", {track_name: m.song.name, tracks: tracks, trim_start_seconds: m.trimStart, trim_end_seconds: m.trimEnd})
      .then(function () { pollExport(m); })
      .catch(function (e) { $("mx-export").textContent = " Could not export: " + e.message; $("mx-export-btn").disabled = false; });
  }
  function pollExport(m) {
    fetch("/completed/export-status?song=" + encodeURIComponent(m.song.name)).then(function (r) { return r.json(); }).then(function (st) {
      if (mixer !== m) return;
      if (st.state === "exporting") { setTimeout(function () { pollExport(m); }, 1500); return; }
      $("mx-export-btn").disabled = false;
      var out = $("mx-export");
      out.innerHTML = "";
      if (st.state === "done") {
        out.appendChild(document.createTextNode(" Exported: "));
        out.appendChild(el("a", {href: media(st.url), download: "", text: st.url.split("/").pop()}));
        out.appendChild(document.createTextNode(" (kept in the vault's final_mixes/)" + (st.notes && st.notes.length ? ". Note: " + st.notes.join("; ") : "")));
      } else {
        out.textContent = " Could not export: " + (st.error || st.state);
      }
    });
  }

  function trimEditor() {
    var details = el("details", {"class": "trim"});
    var start = el("input", {type: "number", min: "0", step: "0.1", value: String(mixer.trimStart)});
    var end = el("input", {type: "number", min: "0", step: "0.1", value: String(mixer.trimEnd)});
    var note = el("span", {"class": "small muted"});
    function live() {
      var s = Math.max(0, parseFloat(start.value) || 0), e = Math.max(0, parseFloat(end.value) || 0);
      if (mixer.full && s + e >= mixer.full) { note.textContent = "That leaves nothing of the song."; return null; }
      mixer.trimStart = s; mixer.trimEnd = e;
      note.textContent = mixer.full ? "Plays " + fmt(mixer.full - s - e) + " of " + fmt(mixer.full) + "." : "";
      tick();
      return [s, e];
    }
    start.addEventListener("input", live); end.addEventListener("input", live);
    details.appendChild(el("summary", {text: "Trim song"}));
    details.appendChild(el("p", {"class": "small muted", text: "Crops the backing track's intro/outro, for every take of this song. " +
      "Non-destructive: the files are untouched."}));
    details.appendChild(el("div", {"class": "trimrow"}, [
      el("label", {}, [document.createTextNode("Cut from start (s) "), start]),
      el("button", {text: "▶ Preview start", onclick: function () { if (live()) play(mixer.trimStart); }}),
      el("label", {}, [document.createTextNode("Cut from end (s) "), end]),
      el("button", {text: "▶ Preview end", onclick: function () { if (live()) play(Math.max(mixer.trimStart, windowEnd() - 5)); }}),
      el("button", {"class": "ok", text: "Save trim", onclick: function () {
        var v = live();
        if (!v) return;
        var name = mixer.song.name;
        note.textContent = "Saving…";
        post("/completed/trim", {take_filename: mixer.song.takes[0].filename, trim_start_seconds: v[0], trim_end_seconds: v[1]})
          .then(function () {
            mixer.song.trim_start_seconds = v[0]; mixer.song.trim_end_seconds = v[1];
            note.textContent = "Saved — takeloom picks it up on its next sync.";
            load();
          })
          .catch(function (e) { note.textContent = "Could not save trim: " + e.message; });
      }}),
      note
    ]));
    return details;
  }

  document.addEventListener("keydown", function (e) {
    if (e.code === "Space" && mixer && !/INPUT|SELECT|TEXTAREA|BUTTON/.test(document.activeElement.tagName)) {
      e.preventDefault(); mixer.playing ? pause() : play();
    }
  });
  $("ct-title").addEventListener("input", renderList);
  $("ct-project").addEventListener("change", renderList);
  $("ct-reload").addEventListener("click", function () {
    var name = mixer && mixer.song.name;
    load(function () { if (name && !(mixer && mixer.playing)) loadSong(name, false); });
  });
  renderMixer();
  load();
})();
