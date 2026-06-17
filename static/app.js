/* Virtual AIS Node - frontend. */
const VAISN = (() => {
  const $ = (id) => document.getElementById(id);
  const fmtTime = (epoch) => epoch ? new Date(epoch * 1000).toLocaleTimeString() : "--";

  async function api(path, opts = {}) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  // ---------- shared header (status pill + optional Start/Stop) ----------
  // The Start/Stop buttons now only live on the Configuration page, so guard
  // every lookup - other pages just have the status pill in the header.
  function bindHeader() {
    const start = $("startBtn");
    const stop  = $("stopBtn");
    if (start) start.addEventListener("click", async () => { await api("/api/start", {method:"POST"}); refreshStatus(); });
    if (stop)  stop .addEventListener("click", async () => { await api("/api/stop",  {method:"POST"}); refreshStatus(); });
  }

  let lastStatus = null;
  async function refreshStatus() {
    try {
      const s = await api("/api/status");
      lastStatus = s;
      const pill = $("statusPill");
      pill.textContent = s.running ? "Running" : "Stopped";
      pill.className = "badge " + (s.running ? "bg-running" : "bg-stopped");
      // dashboard widgets if present
      if ($("stat-vessels")) {
        $("stat-vessels").textContent = s.vessels;
        $("stat-sent").textContent    = s.sentences_sent;
        const af = s.polls.aisfriends, ah = s.polls.aishub, kp = s.polls.kpler;
        $("stat-af").innerHTML = renderPoll(af);
        $("stat-ah").innerHTML = renderPoll(ah);
        if ($("stat-kp")) $("stat-kp").innerHTML = renderPoll(kp);
        const tbody = $("fwd-tbody");
        if (!s.forwarders.length) {
          tbody.innerHTML = '<tr><td colspan="5" class="text-muted">No outputs configured.</td></tr>';
        } else {
          tbody.innerHTML = s.forwarders.map(f => `
            <tr>
              <td><code>${f.host}:${f.port}</code></td>
              <td>${f.protocol.toUpperCase()}</td>
              <td><span class="badge fwd-pill ${f.connected ? "bg-success" : "bg-secondary"}">${f.connected ? "Connected" : "Idle"}</span></td>
              <td>${f.sent_count}</td>
              <td class="text-danger small">${f.last_error || ""}</td>
            </tr>`).join("");
        }
      }
    } catch (e) {
      console.warn("status failed", e);
    }
  }

  function renderPoll(p) {
    if (!p) return "--";
    const ok  = p.last_ok ? `last ok ${fmtTime(p.last_ok)}` : "no successful poll yet";
    const err = p.last_err ? `<div class="text-danger">${escapeHtml(p.last_err)}</div>` : "";
    const next = p.next ? `next in ${Math.max(0, Math.round(p.next - Date.now()/1000))}s` : "";
    return `<div>${p.vessels} vessels</div><div class="text-muted">${ok} · ${next}</div>${err}`;
  }

  const escapeHtml = (s) => (s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

  // ---------- AIS vessel-type palette (ITU-1371 shiptype codes) ----------
  // Each entry has a label, a CSS colour, and the predicate that maps an
  // ais_type code (0-99) onto the category. Order matters - first match wins.
  const VESSEL_TYPES = [
    {label:"Passenger",         color:"#1976d2", match:(t)=> t>=60 && t<=69 },
    {label:"Cargo",             color:"#5dad42", match:(t)=> t>=70 && t<=79 },
    {label:"Tanker",            color:"#dc3545", match:(t)=> t>=80 && t<=89 },
    {label:"Fishing",           color:"#c84db7", match:(t)=> t===30 },
    {label:"Tug / Towing",      color:"#17a2b8", match:(t)=> t===31 || t===32 || t===52 },
    {label:"Dredger / Underwater", color:"#a98c2c", match:(t)=> t===33 || t===34 },
    {label:"Military / Police", color:"#4d6b1a", match:(t)=> t===35 || t===55 },
    {label:"Sailing / Pleasure",color:"#7b5cd9", match:(t)=> t===36 || t===37 },
    {label:"High-Speed Craft",  color:"#ffc107", match:(t)=> t>=40 && t<=49 },
    {label:"Pilot / Port",      color:"#0dcaf0", match:(t)=> t===50 || t===53 || t===54 },
    {label:"SAR / Medical",     color:"#28a745", match:(t)=> t===51 || t===58 },
    {label:"Other / Unknown",   color:"#9aa0aa", match:(t)=> true /* fallback */ },
  ];
  function colorFor(aisType) {
    const t = (typeof aisType === "number") ? aisType : -1;
    return (VESSEL_TYPES.find(v => v.match(t)) || VESSEL_TYPES[VESSEL_TYPES.length-1]).color;
  }
  function labelFor(aisType) {
    const t = (typeof aisType === "number") ? aisType : -1;
    return (VESSEL_TYPES.find(v => v.match(t)) || VESSEL_TYPES[VESSEL_TYPES.length-1]).label;
  }

  // ---------- dashboard ----------
  let dashMap, bboxLayer, vesselsLayer;
  let lastLogId = 0;
  let lastBboxKey = "";

  function initDashboard() {
    bindHeader();
    dashMap = L.map("map");
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {maxZoom:18, attribution:"© OpenStreetMap"}).addTo(dashMap);
    vesselsLayer = L.layerGroup().addTo(dashMap);

    // The map element is initially hidden behind a layout transition; force
    // Leaflet to recalculate its size once the DOM has settled, then again on
    // window resize. Without this the right-hand half of the map renders as
    // grey tiles until the user first interacts.
    setTimeout(() => dashMap.invalidateSize(), 100);
    window.addEventListener("resize", () => dashMap.invalidateSize());

    addLegendControl(dashMap);

    refreshBbox(true);   setInterval(() => refreshBbox(false), 5000);
    refreshStatus();     setInterval(refreshStatus,            2000);
    refreshVessels();    setInterval(refreshVessels,           5000);
    pumpLog();           setInterval(pumpLog,                  1500);
  }

  // Build a Leaflet control docked in the bottom-right of the map listing
  // every vessel category with its colour swatch. Click the header to
  // collapse it down to just the title bar.
  function addLegendControl(map) {
    const Legend = L.Control.extend({
      options: { position: "bottomright" },
      onAdd: function() {
        const div = L.DomUtil.create("div", "vessel-legend");
        const rows = VESSEL_TYPES.map(v => `
          <div class="legend-row">
            <span class="legend-swatch">${arrowSvg(v.color, 14, 0)}</span>
            <span class="legend-label">${v.label}</span>
          </div>`).join("");
        div.innerHTML = `
          <div class="legend-header">
            <span>Vessel types</span>
            <span class="chev">▾</span>
          </div>
          <div class="legend-rows">${rows}</div>`;
        // Stop map drag/zoom when interacting with the legend.
        L.DomEvent.disableClickPropagation(div);
        L.DomEvent.disableScrollPropagation(div);
        div.querySelector(".legend-header").addEventListener("click", () => {
          div.classList.toggle("collapsed");
        });
        return div;
      }
    });
    new Legend().addTo(map);
  }

  // Inline SVG arrow used by both the markers and the legend swatches.
  // Pointing north by default; CSS rotates it to the actual heading/COG.
  function arrowSvg(color, size, rotateDeg) {
    return `<svg viewBox="0 0 20 20" width="${size}" height="${size}"
                 style="transform: rotate(${rotateDeg}deg);">
              <polygon points="10,1 17,18 10,14 3,18"
                       fill="${color}"
                       stroke="#ffffff" stroke-width="1.2"
                       stroke-linejoin="round"/>
            </svg>`;
  }


  // Re-fetch the saved bbox and redraw the rectangle if it changed. Only
  // re-fits the map view on first load (or when the box has moved a lot),
  // so we don't keep stealing the user's pan/zoom.
  async function refreshBbox(forceFit) {
    try {
      const cfg = await api("/api/config");
      const b = cfg.bbox;
      const key = `${b.latmin},${b.latmax},${b.lonmin},${b.lonmax}`;
      if (key === lastBboxKey && bboxLayer) return;
      lastBboxKey = key;
      const bounds = [[b.latmin, b.lonmin], [b.latmax, b.lonmax]];
      if (bboxLayer) {
        bboxLayer.setBounds(bounds);
      } else {
        bboxLayer = L.rectangle(bounds, {color:"#137dc5", weight:1.5, fillOpacity:.08}).addTo(dashMap);
      }
      if (forceFit) dashMap.fitBounds(bboxLayer.getBounds(), {padding:[20,20]});
    } catch (e) { /* ignore */ }
  }

  // Render each vessel as a coloured arrow pointing in the direction of
  // travel. Colour comes from the AIS shiptype; direction prefers true
  // heading and falls back to course-over-ground; if neither is known we
  // draw a coloured dot instead.
  async function refreshVessels() {
    try {
      const list = await api("/api/vessels");
      vesselsLayer.clearLayers();
      list.forEach(v => {
        const colour = colorFor(v.ais_type);
        const dir = pickDirection(v);
        const tooltip = vesselTooltip(v);

        let icon;
        if (dir === null) {
          icon = L.divIcon({
            className: "vessel-icon",
            html:      `<div class="vessel-dot" style="background:${colour}"></div>`,
            iconSize:  [12, 12], iconAnchor: [6, 6],
          });
        } else {
          icon = L.divIcon({
            className: "vessel-icon",
            html:      arrowSvg(colour, 20, dir),
            iconSize:  [20, 20], iconAnchor: [10, 10],
          });
        }
        L.marker([v.lat, v.lon], { icon })
          .bindTooltip(tooltip)
          .addTo(vesselsLayer);
      });
    } catch (e) {}
  }

  // Prefer true heading (0-359). Fall back to COG. Return null when neither
  // is a usable bearing (AIS heading 511 = "not available", same for null).
  function pickDirection(v) {
    if (typeof v.heading === "number" && v.heading >= 0 && v.heading <= 359) return v.heading;
    if (typeof v.cog     === "number" && v.cog     >= 0 && v.cog     <= 359) return v.cog;
    return null;
  }

  function vesselTooltip(v) {
    const bits = [];
    bits.push(`<strong>${escapeHtml(v.name || ("MMSI " + v.mmsi))}</strong>`);
    bits.push(`MMSI ${v.mmsi}`);
    bits.push(labelFor(v.ais_type));
    if (typeof v.heading === "number" && v.heading >= 0 && v.heading <= 359) {
      bits.push(`HDG ${Math.round(v.heading)}°`);
    } else if (typeof v.cog === "number" && v.cog >= 0 && v.cog <= 359) {
      bits.push(`COG ${Math.round(v.cog)}°`);
    }
    if (typeof v.sog === "number") bits.push(`${v.sog.toFixed(1)} kn`);
    bits.push(`<span class="text-muted">src: ${v.src}</span>`);
    return bits.join("<br>");
  }



  async function pumpLog() {
    try {
      const items = await api(`/api/log?after=${lastLogId}&limit=200`);
      if (!items.length) return;
      lastLogId = items[items.length-1].id;
      const con = $("console");
      items.forEach(it => {
        const cls = it.src === "aisfriends" ? "src-af"
                  : it.src === "kpler"      ? "src-kp"
                  : "src-ah";
        con.insertAdjacentHTML("beforeend",
          `<span class="ts">${fmtTime(it.ts)}</span> <span class="${cls}">[${it.src}]</span> ${escapeHtml(it.txt)}\n`);
      });
      // trim
      while (con.childNodes.length > 400) con.removeChild(con.firstChild);
      con.scrollTop = con.scrollHeight;
      $("console-count").textContent = `${lastLogId} sentences`;
    } catch (e) {}
  }

  // ---------- config page ----------
  let cfgMap, cfgRect;
  function initConfig(outputs) {
    bindHeader();
    cfgMap = L.map("map-config");
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {maxZoom:18, attribution:"© OpenStreetMap"}).addTo(cfgMap);

    // Same trick as the dashboard map: force Leaflet to recompute its size
    // once the DOM has finished laying out, and again whenever the window is
    // resized. Without this the right half of a full-width map renders as
    // grey tiles until the user first interacts with it.
    setTimeout(() => cfgMap.invalidateSize(), 100);
    window.addEventListener("resize", () => cfgMap.invalidateSize());

    // Read the four inputs, sanitised into a proper [SW, NE] bounds pair.
    const readInputs = () => {
      let la1 = parseFloat($("latmin").value);
      let la2 = parseFloat($("latmax").value);
      let lo1 = parseFloat($("lonmin").value);
      let lo2 = parseFloat($("lonmax").value);
      if (!isFinite(la1) || !isFinite(la2) || !isFinite(lo1) || !isFinite(lo2)) return null;
      if (la1 > la2) [la1, la2] = [la2, la1];
      if (lo1 > lo2) [lo1, lo2] = [lo2, lo1];
      return [[la1, lo1], [la2, lo2]];
    };

    // Write a Leaflet LatLngBounds back into the four inputs (rounded to 4dp).
    let suppressInputEvents = false;
    const writeInputs = (bounds) => {
      const sw = bounds.getSouthWest(), ne = bounds.getNorthEast();
      suppressInputEvents = true;
      $("latmin").value = sw.lat.toFixed(4);
      $("latmax").value = ne.lat.toFixed(4);
      $("lonmin").value = sw.lng.toFixed(4);
      $("lonmax").value = ne.lng.toFixed(4);
      suppressInputEvents = false;
    };

    // Initial draw - the rectangle is created ONCE; from here on we only
    // mutate it in place with setBounds() so we don't have to detach/reattach
    // the draggable handles.
    const initial = readInputs() || [[49.0, -5.0], [51.5, 2.5]];
    cfgRect = L.rectangle(initial, {color:"#0d6efd", weight:1.5}).addTo(cfgMap);
    cfgMap.fitBounds(cfgRect.getBounds(), {padding:[20,20]});

    // --- DIY draggable handles (no plugin needed) ---------------------------
    // 4 corner markers + 1 centre move marker, all standard Leaflet.
    const cornerIcon = (cls) => L.divIcon({
      className: "bbox-handle " + cls,
      iconSize: [12, 12], iconAnchor: [6, 6],
    });
    const moveIcon = L.divIcon({
      className: "bbox-move",
      iconSize: [18, 18], iconAnchor: [9, 9],
    });

    const cornerOf = (b, which) => {
      switch (which) {
        case "nw": return [b.getNorth(), b.getWest()];
        case "ne": return [b.getNorth(), b.getEast()];
        case "se": return [b.getSouth(), b.getEast()];
        case "sw": return [b.getSouth(), b.getWest()];
      }
    };
    const centreOf = (b) => {
      const c = b.getCenter();
      return [c.lat, c.lng];
    };

    const handles = {};
    ["nw","ne","se","sw"].forEach(k => {
      handles[k] = L.marker(cornerOf(cfgRect.getBounds(), k),
        { icon: cornerIcon(k), draggable: true, autoPan: true })
        .addTo(cfgMap);
    });
    handles.mid = L.marker(centreOf(cfgRect.getBounds()),
      { icon: moveIcon, draggable: true, autoPan: true })
      .addTo(cfgMap);

    // Refresh all handle positions to match the rectangle.
    const repositionHandles = () => {
      const b = cfgRect.getBounds();
      ["nw","ne","se","sw"].forEach(k => handles[k].setLatLng(cornerOf(b, k)));
      handles.mid.setLatLng(centreOf(b));
    };

    // When a corner is dragged, rebuild bounds from that corner + its opposite.
    const opposite = { nw:"se", ne:"sw", se:"nw", sw:"ne" };
    ["nw","ne","se","sw"].forEach(k => {
      handles[k].on("drag", () => {
        const moving = handles[k].getLatLng();
        const fixed  = handles[opposite[k]].getLatLng();
        const la1 = Math.min(moving.lat, fixed.lat);
        const la2 = Math.max(moving.lat, fixed.lat);
        const lo1 = Math.min(moving.lng, fixed.lng);
        const lo2 = Math.max(moving.lng, fixed.lng);
        cfgRect.setBounds([[la1, lo1], [la2, lo2]]);
        writeInputs(cfgRect.getBounds());
        repositionHandles();
      });
    });

    // When the centre is dragged, translate the whole bounds by the delta.
    let lastMid = handles.mid.getLatLng();
    handles.mid.on("dragstart", () => { lastMid = handles.mid.getLatLng(); });
    handles.mid.on("drag", () => {
      const now = handles.mid.getLatLng();
      const dLat = now.lat - lastMid.lat;
      const dLng = now.lng - lastMid.lng;
      lastMid = now;
      const b = cfgRect.getBounds();
      const sw = b.getSouthWest(), ne = b.getNorthEast();
      cfgRect.setBounds([[sw.lat + dLat, sw.lng + dLng],
                         [ne.lat + dLat, ne.lng + dLng]]);
      writeInputs(cfgRect.getBounds());
      repositionHandles();
    });

    // Typing in any of the four inputs reshapes the rectangle in place.
    const applyInputs = () => {
      if (suppressInputEvents) return;
      const b = readInputs();
      if (!b) return;
      cfgRect.setBounds(b);
      repositionHandles();
    };
    ["latmin","latmax","lonmin","lonmax"].forEach(id => {
      $(id).addEventListener("change", applyInputs);
      $(id).addEventListener("input",  applyInputs);
    });

    // "Re-centre map" link - re-fits the view to the current bbox.
    const fitBtn = $("bbox-fit");
    if (fitBtn) fitBtn.addEventListener("click", () => {
      cfgMap.fitBounds(cfgRect.getBounds(), {padding:[20,20]});
    });

    // outputs
    const obody = $("outputs-body");
    function renderOutputs(list) {
      obody.innerHTML = list.map((o, i) => `
        <div class="output-row" data-i="${i}">
          <div class="row g-2">
            <div class="col-1 d-flex align-items-center"><input class="form-check-input mt-0" type="checkbox" data-k="enabled" ${o.enabled ? "checked":""}></div>
            <div class="col-2"><select class="form-select" data-k="protocol">
              <option value="tcp" ${o.protocol==="tcp"?"selected":""}>TCP</option>
              <option value="udp" ${o.protocol==="udp"?"selected":""}>UDP</option>
            </select></div>
            <div class="col-5"><input class="form-control" data-k="host" value="${o.host}" placeholder="host or IP"></div>
            <div class="col-3"><input class="form-control" data-k="port" type="number" value="${o.port}"></div>
            <div class="col-1"><button class="btn btn-outline-danger btn-sm" data-act="del">×</button></div>
          </div>
        </div>`).join("");
    }
    renderOutputs(outputs);
    $("add-fwd").addEventListener("click", () => {
      const cur = collectOutputs();
      cur.push({enabled:true, protocol:"tcp", host:"127.0.0.1", port:10110});
      renderOutputs(cur);
    });
    obody.addEventListener("click", (e) => {
      if (e.target.dataset.act === "del") {
        const cur = collectOutputs();
        const i = parseInt(e.target.closest(".output-row").dataset.i);
        cur.splice(i, 1);
        renderOutputs(cur);
      }
    });

    function collectOutputs() {
      return [...obody.querySelectorAll(".output-row")].map(row => ({
        enabled:  row.querySelector('[data-k=enabled]').checked,
        protocol: row.querySelector('[data-k=protocol]').value,
        host:     row.querySelector('[data-k=host]').value.trim(),
        port:     parseInt(row.querySelector('[data-k=port]').value) || 10110,
      }));
    }

    $("save-config").addEventListener("click", async () => {
      const payload = {
        bbox: {
          latmin: parseFloat($("latmin").value),
          latmax: parseFloat($("latmax").value),
          lonmin: parseFloat($("lonmin").value),
          lonmax: parseFloat($("lonmax").value),
        },
        poll: {
          interval_seconds: parseInt($("poll-interval").value),
          stagger_seconds:  parseInt($("poll-stagger").value),
        },
        sources: {
          aisfriends: {enabled: $("src-af").checked},
          aishub:     {enabled: $("src-ah").checked},
          kpler:      {enabled: $("src-kp") ? $("src-kp").checked : false},
        },
        outputs: collectOutputs(),
      };
      const status = $("save-status");
      status.textContent = "saving…"; status.className = "ms-2 small text-muted";
      try { await api("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
        status.textContent = "saved ✓"; status.className = "ms-2 small text-success"; }
      catch (e) { status.textContent = "save failed: "+e.message; status.className = "ms-2 small text-danger"; }
    });

    refreshStatus(); setInterval(refreshStatus, 3000);
  }

  // ---------- credentials page ----------
  function initCredentials() {
    bindHeader();

    // Show/hide the FlareSolverr URL row depending on backend choice.
    const backendSel = $("af-backend");
    const fsRow      = $("af-fs-row");
    const updateFsRow = () => {
      if (backendSel && fsRow) {
        fsRow.style.display = (backendSel.value === "flaresolverr") ? "" : "none";
      }
    };
    if (backendSel) backendSel.addEventListener("change", updateFsRow);
    updateFsRow();

    $("save-creds").addEventListener("click", async () => {
      const payload = {
        aisfriends_token:            $("af-token").value,
        aisfriends_backend:          backendSel ? backendSel.value : "flaresolverr",
        aisfriends_flaresolverr_url: $("af-fs-url") ? $("af-fs-url").value : "http://localhost:8191",
        aishub_username:             $("ah-user").value,
        // Kpler - all optional, omitted if the card isn't on this page.
        kpler_credential: $("kp-cred")     ? $("kp-cred").value     : "",
        kpler_flavour:    $("kp-flavour")  ? $("kp-flavour").value  : "graphql",
        kpler_api_url:    $("kp-api-url")  ? $("kp-api-url").value  : "",
        kpler_token_url:  $("kp-token-url")? $("kp-token-url").value: "",
        kpler_audience:   $("kp-audience") ? $("kp-audience").value : "",
      };
      const st = $("save-creds-status");
      st.textContent = "saving…"; st.className = "ms-2 small text-muted";
      try { await api("/api/credentials", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
        st.textContent = "saved ✓"; st.className = "ms-2 small text-success"; }
      catch (e) { st.textContent = e.message; st.className = "ms-2 small text-danger"; }
    });

    async function runTest(source, btn, statusEl) {
      statusEl.textContent = "testing…"; statusEl.className = "ms-2 small text-muted";
      try {
        const r = await api(`/api/test-source/${source}`, {method:"POST"});
        if (r.ok) {
          const backendNote = r.backend ? ` (via ${r.backend})` : "";
          statusEl.textContent = `OK · ${r.vessels} vessels${backendNote}`;
          statusEl.className = "ms-2 small text-success";
        } else {
          statusEl.textContent = `Error: ${r.error || "unknown"}`;
          statusEl.className = "ms-2 small text-danger";
        }
      } catch (e) {
        statusEl.textContent = e.message;
        statusEl.className = "ms-2 small text-danger";
      }
    }
    $("test-af").addEventListener("click", () => runTest("aisfriends", $("test-af"), $("test-af-status")));
    $("test-ah").addEventListener("click", () => runTest("aishub",     $("test-ah"), $("test-ah-status")));
    if ($("test-kp")) {
      $("test-kp").addEventListener("click", () => runTest("kpler",    $("test-kp"), $("test-kp-status")));
    }
    refreshStatus(); setInterval(refreshStatus, 3000);
  }


  // ---------- logs page ----------
  function initLogs() {
    bindHeader();
    let last = 0;
    const con = $("logs");
    async function tick() {
      try {
        const items = await api(`/api/log?after=${last}&limit=200`);
        if (items.length) {
          last = items[items.length-1].id;
          items.forEach(it => {
            const cls = it.src === "aisfriends" ? "src-af"
                      : it.src === "kpler"      ? "src-kp"
                      : "src-ah";
            con.insertAdjacentHTML("beforeend",
              `<span class="ts">${new Date(it.ts*1000).toISOString()}</span> <span class="${cls}">[${it.src}]</span> ${escapeHtml(it.txt)}\n`);
          });
          while (con.childNodes.length > 1500) con.removeChild(con.firstChild);
          con.scrollTop = con.scrollHeight;
          $("logs-count").textContent = `${last} sentences`;
        }
      } catch (e) {}
    }
    tick(); setInterval(tick, 1500);
    refreshStatus(); setInterval(refreshStatus, 3000);
  }

  // ---------- Wi-Fi page ----------
  // Talks to /api/wifi/{status,scan,saved,connect,forget}. NetworkManager
  // backed. The page degrades gracefully if nmcli isn't installed.

  let connectModal = null;

  function signalBars(pct) {
    // 0..3 filled bars. 100% = 4 bars, 75% = 3, 50% = 2, 25% = 1.
    const v = Math.max(0, Math.min(100, pct|0));
    const filled = v >= 80 ? 4 : v >= 55 ? 3 : v >= 30 ? 2 : v >= 10 ? 1 : 0;
    let html = '<span class="sig-bars" title="' + v + '%">';
    for (let i = 1; i <= 4; i++) {
      html += `<i class="bar b${i} ${i<=filled?"on":""}"></i>`;
    }
    html += '</span>';
    return html;
  }

  async function refreshIfaces() {
    try {
      const s = await api("/api/wifi/status");
      $("wifi-hostname").textContent = s.hostname ? "host: " + s.hostname : "";
      const tbody = $("iface-tbody");
      if (!s.nmcli_available) {
        tbody.innerHTML = `<tr><td colspan="4" class="text-danger px-3 py-3">
          NetworkManager (<code>nmcli</code>) is not available on this host.
          On Debian/Ubuntu/Raspberry Pi OS, install it with
          <code>sudo apt install network-manager</code> and re-run install.sh.
          ${s.error ? "<br><small>"+escapeHtml(s.error)+"</small>" : ""}
        </td></tr>`;
        ["wifi-rescan","wifi-hidden","wifi-refresh-saved"].forEach(id => {
          if ($(id)) $(id).disabled = true;
        });
        return;
      }
      if (!s.interfaces || !s.interfaces.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="text-muted px-3 py-3">No interfaces.</td></tr>';
        return;
      }
      tbody.innerHTML = s.interfaces.map(i => {
        const dflt = i.is_default
          ? `<span class="badge bg-success">default route${i.metric!=null ? " · metric "+i.metric : ""}</span>`
          : "";
        let extra = "";
        if (i.type === "wifi" || i.type === "802-11-wireless") {
          if (i.ssid) extra = `Wi-Fi: <strong>${escapeHtml(i.ssid)}</strong>`
                            + (i.signal != null ? ` · signal ${i.signal}%` : "")
                            + (i.security ? ` · ${escapeHtml(i.security)}` : "");
        } else if (i.connection) {
          extra = `<span class="text-muted">${escapeHtml(i.connection)}</span>`;
        }
        return `<tr>
          <td class="ps-3"><code>${escapeHtml(i.device)}</code> <span class="text-muted small">${escapeHtml(i.type||"")}</span></td>
          <td>${escapeHtml(i.state||"")}</td>
          <td>${i.ipv4 ? `<code>${escapeHtml(i.ipv4)}</code>` : '<span class="text-muted">—</span>'}</td>
          <td class="pe-3">${dflt} ${extra}</td>
        </tr>`;
      }).join("");
    } catch (e) { console.warn("wifi status failed", e); }
  }

  async function refreshSaved() {
    try {
      const r = await api("/api/wifi/saved");
      const tbody = $("saved-tbody");
      if (!r.ok && r.error) {
        tbody.innerHTML = `<tr><td colspan="3" class="px-3 py-3 text-danger">${escapeHtml(r.error)}</td></tr>`;
        return;
      }
      if (!r.profiles || !r.profiles.length) {
        tbody.innerHTML = '<tr><td colspan="3" class="px-3 py-3 text-muted">No saved networks.</td></tr>';
        return;
      }
      tbody.innerHTML = r.profiles.map(p => `
        <tr>
          <td class="ps-3"><strong>${escapeHtml(p.ssid||p.name)}</strong></td>
          <td>${p.autoconnect ? '<span class="badge bg-secondary">yes</span>' : '<span class="text-muted">no</span>'}</td>
          <td class="pe-3 text-end">
            <button class="btn btn-sm btn-outline-danger" data-act="forget" data-name="${escapeHtml(p.name)}">Forget</button>
          </td>
        </tr>`).join("");
    } catch (e) { console.warn("wifi saved failed", e); }
  }

  async function refreshScan(rescan) {
    const status = $("scan-status");
    status.textContent = rescan ? "scanning…" : "loading cached scan…";
    try {
      const url = "/api/wifi/scan" + (rescan ? "" : "?rescan=0");
      const r = await api(url);
      const tbody = $("scan-tbody");
      if (!r.ok && r.error) {
        tbody.innerHTML = `<tr><td colspan="4" class="px-3 py-3 text-danger">${escapeHtml(r.error)}</td></tr>`;
        status.textContent = "";
        return;
      }
      if (!r.networks.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="px-3 py-3 text-muted">No networks visible.</td></tr>';
      } else {
        tbody.innerHTML = r.networks.map(n => `
          <tr>
            <td class="ps-3">${signalBars(n.signal)}</td>
            <td>
              ${n.in_use ? '<span class="badge bg-success me-2">in use</span>' : ''}
              <strong>${escapeHtml(n.ssid)}</strong>
              <span class="text-muted small">· ${n.signal}%</span>
            </td>
            <td><span class="badge bg-secondary">${escapeHtml(n.security||"Open")}</span></td>
            <td class="pe-3 text-end">
              <button class="btn btn-sm btn-primary"
                      data-act="connect"
                      data-ssid="${escapeHtml(n.ssid)}"
                      data-sec="${escapeHtml(n.security||"")}">Connect</button>
            </td>
          </tr>`).join("");
      }
      status.textContent = rescan ? "scan complete" : "";
      setTimeout(() => { if (status.textContent === "scan complete") status.textContent = ""; }, 1500);
    } catch (e) {
      status.textContent = "scan failed: " + e.message;
    }
  }

  function openConnectModal(ssid, securityHint, hidden) {
    $("connect-ssid-display").textContent = hidden ? "hidden network" : (ssid || "network");
    $("connect-hidden-row").style.display = hidden ? "" : "none";
    if (hidden) $("connect-ssid").value = "";
    $("connect-password").value = "";
    $("connect-result").textContent = "";
    $("connect-result").className = "small";
    // Open networks don't need a password
    const isOpen = (securityHint || "").trim() === "" || /open/i.test(securityHint || "");
    if (isOpen && !hidden) {
      $("connect-pw-row").querySelector("label").textContent = "Password (open network - leave blank)";
    } else {
      $("connect-pw-row").querySelector("label").textContent = "Password";
    }
    if (!connectModal) connectModal = new bootstrap.Modal($("connectModal"));
    connectModal._currentSsid   = ssid || "";
    connectModal._currentHidden = !!hidden;
    connectModal.show();
  }

  async function doConnect() {
    const hidden = connectModal._currentHidden;
    const ssid   = hidden ? ($("connect-ssid").value || "").trim() : connectModal._currentSsid;
    const password = $("connect-password").value;
    if (!ssid) {
      $("connect-result").textContent = "SSID is required."; $("connect-result").className = "small text-danger";
      return;
    }
    const result = $("connect-result");
    result.textContent = "connecting (may take 10-30 s)…"; result.className = "small text-muted";
    $("connect-go").disabled = true;
    try {
      const r = await api("/api/wifi/connect", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ssid, password, hidden})
      });
      if (r.ok) {
        result.textContent = `Connected to ${ssid}. Refreshing status…`;
        result.className = "small text-success";
        setTimeout(() => {
          connectModal.hide();
          refreshIfaces(); refreshSaved(); refreshScan(false);
        }, 1500);
      } else {
        result.textContent = "Failed: " + (r.error || "unknown error");
        result.className = "small text-danger";
      }
    } catch (e) {
      result.textContent = e.message;
      result.className = "small text-danger";
    } finally {
      $("connect-go").disabled = false;
    }
  }

  async function doForget(name) {
    if (!confirm(`Forget saved network "${name}"?`)) return;
    try {
      const r = await api("/api/wifi/forget", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({name})
      });
      if (!r.ok) alert("Forget failed: " + (r.error || "unknown"));
    } catch (e) {
      alert(e.message);
    }
    refreshSaved(); refreshIfaces();
  }

  function initWifi() {
    bindHeader();
    refreshStatus(); setInterval(refreshStatus, 3000);

    refreshIfaces(); setInterval(refreshIfaces, 3000);
    refreshSaved();
    refreshScan(true);

    $("wifi-rescan").addEventListener("click", () => refreshScan(true));
    $("wifi-refresh-saved").addEventListener("click", () => refreshSaved());
    $("wifi-hidden").addEventListener("click", () => openConnectModal("", "", true));

    // Delegated handlers for the dynamic rows
    $("scan-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act=connect]");
      if (!btn) return;
      openConnectModal(btn.dataset.ssid, btn.dataset.sec, false);
    });
    $("saved-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-act=forget]");
      if (!btn) return;
      doForget(btn.dataset.name);
    });
    $("connect-go").addEventListener("click", doConnect);
    // Enter in the password field submits.
    $("connect-password").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); doConnect(); }
    });
  }

  return { initDashboard, initConfig, initCredentials, initLogs, initWifi };
})();
