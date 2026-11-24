// The Cameras page (web.py's cameras_page). The phones and their previews
// are this vault's own (GET /cameras/data); their names, lenses, battery
// estimates and the program camera are the recording node's, asked for and
// changed through the vault (POST /nodes/<id>/rpc: takeloom's node_rpc.py).
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var PREFIX = "takeloom-camera:";
  var node = null, labels = {}, program = "", batteries = {}, framings = {}, nodeCameras = [];

  function esc(t) { return String(t == null ? "" : t).replace(/&/g, "&amp;").replace(/</g, "&lt;"); }
  function status(text, bad) { var s = $("cm-status"); s.textContent = text || ""; s.style.color = bad ? "#d33" : ""; }
  function rpc(op, args) {
    if (!node) return Promise.reject(new Error("No recording node is connected."));
    return fetch("/nodes/" + node + "/rpc", {method: "POST", headers: {"Content-Type": "application/json"},
                                              body: JSON.stringify({op: op, args: args || {}})})
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.statusText); return j; }); });
  }

  // --- what the node says about its cameras ---

  function loadNodeState() {
    if (!node) { labels = {}; program = ""; batteries = {}; nodeCameras = []; renderProgram(); return; }
    rpc("session_cameras").then(function (j) {
      nodeCameras = j.cameras || [];
      labels = {};
      nodeCameras.forEach(function (c) { labels[c[0]] = c[1]; });
      return rpc("get_program_camera");
    }).then(function (j) { program = j.camera || ""; renderProgram(); }, function (e) { status(e.message, true); });
    rpc("camera_batteries").then(function (j) { batteries = j.batteries || {}; }, function () {});
    Object.keys(framings).forEach(loadFraming);  // a lens may have been picked on the phone itself
  }
  function renderProgram() {
    var p = $("cm-program");
    p.innerHTML = "";
    if (!node || !nodeCameras.length) return;
    p.appendChild(document.createTextNode("The live stream shows "));
    nodeCameras.forEach(function (c) {
      var phone = c[0].indexOf(PREFIX) === 0;
      var b = document.createElement("button");
      b.className = "air" + (c[0] === program ? " on" : "");
      b.textContent = (c[0] === program ? "● " : "") + c[1];
      if (!phone) b.title = "A camera on the node itself: the vault's stream can't show it";
      b.onclick = function () {
        rpc("set_program_camera", {camera: c[0]}).then(function () { program = c[0]; renderProgram(); }, function (e) { status(e.message, true); });
      };
      p.appendChild(b);
      p.appendChild(document.createTextNode(" "));
    });
  }
  var asked = {};
  function loadFraming(device) {
    if (asked[device]) return;
    asked[device] = true;
    rpc("get_camera_framing", {camera: device}).then(function (f) { framings[device] = f; }, function () {})
      .then(function () { asked[device] = false; });
  }
  function lensRow(device) {
    var f = framings[device];
    var row = document.createElement("div");
    row.className = "small";
    if (!f) { if (node) loadFraming(device); return row; }
    if (!f.supported) { row.textContent = "Lens: this phone's app can't be zoomed."; return row; }
    row.appendChild(document.createTextNode("Lens "));
    (f.lenses || [1]).forEach(function (z) {
      var b = document.createElement("button");
      b.className = "lens" + (Math.abs(z - f.zoom) < 0.01 ? " on" : "");
      b.textContent = (Math.round(z * 10) / 10) + "×";
      b.onclick = function () {
        rpc("set_camera_framing", {camera: device, zoom: z, x: 0.5, y: 0.5}).then(function (r) { framings[device] = r; }, function (e) { status(e.message, true); });
      };
      row.appendChild(b);
      row.appendChild(document.createTextNode(" "));
    });
    return row;
  }
  function batteryText(device, s) {
    var b = batteries[device];
    if (b && b.level != null) {
      return " · battery " + Math.round(100 * b.level) + "%" + (b.charging ? " (charging" + (b.full_hours ? ", full in " + b.full_hours + " h" : "") + ")" :
             b.minutes_left ? " (about " + Math.round(b.minutes_left) + " min left)" : "");
    }
    return s.battery != null ? " · battery " + Math.round(100 * s.battery) + "%" + (s.charging ? " (charging)" : "") : "";
  }

  // --- the phones, from this vault ---

  function load() {
    fetch("/cameras/data").then(function (r) { return r.json(); }).then(function (j) {
      var sel = $("cm-node"), keep = node;
      sel.innerHTML = "";
      j.nodes.forEach(function (n) { var o = document.createElement("option"); o.value = n.id; o.textContent = n.label + (n.active ? " (in charge)" : ""); sel.appendChild(o); });
      var pick = j.nodes.find(function (n) { return n.id === keep; }) || j.nodes.find(function (n) { return n.active; }) || j.nodes[0];
      node = pick ? pick.id : null;
      if (pick) sel.value = pick.id;
      $("cm-none").style.display = node ? "none" : "";
      if (node !== keep) { framings = {}; loadNodeState(); }

      var rec = {};
      j.recording.forEach(function (r) { rec[r.device_id] = r.session; });
      var el = $("cams");
      if (!j.cameras.length) el.innerHTML = "<p class='muted'>No phones connected.</p>";
      else {
        var empty = el.querySelector("p.muted");
        if (empty) empty.remove();
      }
      j.cameras.forEach(function (c) {
        var device = PREFIX + c.device_id, id = "cam-" + c.device_id.replace(/[^\w-]/g, "_"), box = $(id);
        if (!box) {
          box = document.createElement("div");
          box.id = id;
          box.innerHTML = "<img style='width:100%;aspect-ratio:16/9;object-fit:contain;background:#000;border-radius:6px'><p class='small'></p><div class='lensbox'></div>";
          el.appendChild(box);
        }
        var s = c.status || {};
        box.querySelector("img").src = "/cameras/" + encodeURIComponent(c.device_id) + "/preview.jpg?" + Date.now();
        box.querySelector("p").innerHTML = "<b>" + esc(labels[device] || c.client_name) + "</b>" + (device === program ? " <span class='pill'>● on the stream</span>" : "") +
          " <span class='muted'>" + esc(c.client_name) + " · " + esc(c.ip) + " · app " + esc(c.app_version) + "</span><br>" +
          (s.width ? esc(s.width + "×" + s.height + " " + (s.fps || "") + " fps") : "") + batteryText(device, s) +
          (s.live_width ? " · live " + esc(s.live_width + "×" + s.live_height) + " " + ((s.live_bitrate || 0) / 1e6).toFixed(1) + " Mbps" : "") +
          (rec[c.device_id] ? "<br><b>● recording</b> " + esc(rec[c.device_id]) : "");
        var lb = box.querySelector(".lensbox");
        lb.innerHTML = "";
        lb.appendChild(lensRow(device));
      });
      Array.prototype.slice.call(el.children).forEach(function (b) {
        if (b.id && b.id.indexOf("cam-") === 0 && !j.cameras.some(function (c) { return "cam-" + c.device_id.replace(/[^\w-]/g, "_") === b.id; })) el.removeChild(b);
      });
      $("nodes").textContent = (j.nodes.length ? j.nodes.map(function (n) { return n.label + " (" + n.ip + ")" + (n.active ? " — in charge of the phones" : ""); }).join(", ") : "None connected.") +
        (j.streams && j.streams.length ? "  ·  Streaming live: " + j.streams.map(function (s) { return s.session + " (showing " + s.program + ")"; }).join(", ") : "");
    }).catch(function () {}).then(function () { setTimeout(load, 1500); });
  }

  $("cm-node").addEventListener("change", function () { node = $("cm-node").value; framings = {}; loadNodeState(); });
  load();
  setInterval(loadNodeState, 8000);
})();
