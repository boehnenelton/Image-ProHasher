/* group_viewer front-end - v106 | RELATIONAL_ID: 6e2b90d4-17a5-4c3f-8d61-f4a7c53e0b92 | Elton Boehnen · boehnenelton2024@gmail.com · boehnenelton2024.pages.dev · github.com/boehnenelton
 * Plain ES6, no dependencies, no inline script (CSP: script-src 'self'). Untrusted strings
 * (file names / paths / EXIF) only ever reach the DOM through textContent. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const UI_KEY = "gv_ui", DENSITY_KEY = "gv_density";
  const TILE_PX = { s: "110px", m: "160px", l: "250px" };
  const S = {
    session: document.body.dataset.session || null,
    view: "groups", groupId: null, tiles: [], groupMeta: { resolved: false, basis: "" },
    selected: new Set(), verdicts: {}, pending: new Map(),
    filters: { q: "", state: "open", basis: "", sort: "reclaimable" },
    list: { items: [], total: 0, offset: 0, basisCounts: {}, reclaimable: 0, loaded: false },
    density: "m", expand: false, pendingTarget: null, browserPath: null,
    lb: { items: [], index: 0, mode: "single" },
  };

  // ------------------------------------------------------------------ helpers
  function h(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const clean = (nodes) => nodes.flat().filter((n) => n !== null && n !== undefined && n !== false);   // replaceChildren(null) would render the text "null"
  const btn = (label, onclick, cls, attrs) => h("button", Object.assign({ class: cls || "", onclick, type: "button" }, attrs || {}), label);
  const q = (o) => new URLSearchParams(o).toString();
  const fmtBytes = (n) => {
    if (n === null || n === undefined) return "-";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Math.abs(n);
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(i > 1 ? 1 : i ? 1 : 0) + " " + u[i];
  };
  const shortDate = (r) => ((r.exif_datetime || r.file_modified_time || "").slice(0, 10)).replace(/:/g, "-");
  const parentName = (p) => { const parts = (p || "").split("/"); return parts.length > 1 ? parts[parts.length - 2] : ""; };
  const baseName = (p) => (p || "").split("/").pop();
  const pixels = (r) => (r.width || 0) * (r.height || 0);

  let toastTimer = null;
  function toast(msg, actionLabel, actionFn, ms) {
    $("toastText").textContent = msg;
    const a = $("toastAction");
    a.hidden = !actionLabel;
    a.textContent = actionLabel || "";
    a.onclick = actionFn ? () => { $("toast").hidden = true; actionFn(); } : null;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, ms || (actionLabel ? 9000 : 5000));
  }

  async function api(url, body) {
    try {
      const r = await fetch(url, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.status === 401) { location.href = "/login"; return { error: "unauthorized" }; }
      const data = await r.json().catch(() => ({}));
      if (!r.ok && !data.error) data.error = `HTTP ${r.status}`;
      if (data.error) toast(data.error);
      return data;
    } catch (e) {
      toast("Can't reach group_viewer - is it still running?");
      return { error: "network" };
    }
  }

  // ------------------------------------------------------------------ dialogs
  let dialogCloser = null, dialogReturnFocus = null;
  function openDialog(title, bodyNode, actions) {
    dialogReturnFocus = document.activeElement;
    $("dialogTitle").textContent = title;
    $("dialogBody").replaceChildren(...clean(Array.isArray(bodyNode) ? bodyNode : [bodyNode]));
    $("dialogActions").replaceChildren(...actions.map((a) => btn(a.label, async () => { if (await a.onclick() !== false) closeDialog(); }, "gv-btn" + (a.primary ? " gv-btn--primary" : ""))));
    $("dialogBackdrop").classList.add("is-open");
    const first = $("dialogActions").querySelector(".gv-btn--primary") || $("dialogActions").querySelector("button");
    if (first) first.focus();
  }
  function closeDialog() {
    $("dialogBackdrop").classList.remove("is-open");
    if (dialogCloser) { dialogCloser(); dialogCloser = null; }
    if (dialogReturnFocus && document.contains(dialogReturnFocus)) dialogReturnFocus.focus();
    dialogReturnFocus = null;
  }
  $("dialogBackdrop").addEventListener("keydown", (e) => {           // keep Tab inside the dialog while it is open
    if (e.key !== "Tab") return;
    const f = [...$("dialogBackdrop").querySelectorAll("button, input, [tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  function confirmDialog(title, message, confirmLabel) {
    return new Promise((resolve) => {
      let answered = false;
      dialogCloser = () => { if (!answered) resolve(false); };
      openDialog(title, h("p", { text: message }), [
        { label: "Cancel", onclick: () => { answered = true; resolve(false); } },
        { label: confirmLabel || "Continue", primary: true, onclick: () => { answered = true; resolve(true); } },
      ]);
    });
  }
  $("dialogBackdrop").addEventListener("click", (e) => { if (e.target === $("dialogBackdrop")) closeDialog(); });

  // ------------------------------------------------------------------ UI state persistence
  function saveUi() {
    try { localStorage.setItem(UI_KEY, JSON.stringify({ session: S.session, groupId: S.groupId, filters: S.filters, view: S.view })); } catch (e) { /* private mode */ }
  }
  function restoreUi() {
    try {
      const o = JSON.parse(localStorage.getItem(UI_KEY) || "null");
      const d = localStorage.getItem(DENSITY_KEY); if (d && TILE_PX[d]) S.density = d;
      if (!o) return null;
      if (o.session && [...$("sessionSelect").options].some((x) => x.value === o.session)) { S.session = o.session; $("sessionSelect").value = o.session; }
      if (o.filters) Object.assign(S.filters, o.filters);
      return o;
    } catch (e) { return null; }
  }

  // ------------------------------------------------------------------ navigation
  function setView(view) {
    S.view = view;
    for (const id of ["Groups", "Trash", "Notes", "About"]) $("view" + id).hidden = id.toLowerCase() !== view;
    document.querySelectorAll(".gv-nav__item[data-view]").forEach((b) => b.classList.toggle("is-active", b.dataset.view === view));
    $("btnBack").hidden = !(view === "groups" && S.groupId);
    closeDrawer();
    if (view === "trash") loadTrash();
    if (view === "notes") loadNotes();
    renderToolbar(); renderSelBar(); saveUi();
  }
  $("btnSignOut").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); location.href = "/login"; });
  document.querySelectorAll(".gv-nav__item[data-view]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.view === "groups" && S.groupId) goList(); else if (b.dataset.view === "groups" && S.view === "groups") loadGroups(true); else setView(b.dataset.view);
  }));
  function closeDrawer() { $("sidebar").classList.remove("is-open"); $("sidebarOverlay").classList.remove("is-open"); $("hamburgerBtn").setAttribute("aria-expanded", "false"); }
  $("hamburgerBtn").addEventListener("click", () => {
    const open = $("sidebar").classList.toggle("is-open"); $("sidebarOverlay").classList.toggle("is-open", open);
    $("hamburgerBtn").setAttribute("aria-expanded", String(open));
  });
  $("sidebarOverlay").addEventListener("click", closeDrawer);

  $("sessionSelect").addEventListener("change", (e) => {
    S.session = e.target.value; S.groupId = null; S.selected.clear(); S.list.loaded = false;
    showList(); loadCart(); loadBanner();
  });
  let filterTimer = null;
  $("filterInput").addEventListener("input", (e) => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => { S.filters.q = e.target.value.trim(); saveUi(); loadGroups(true); }, 250);
  });
  $("btnBack").addEventListener("click", goList);
  function goList() { if (history.state && history.state.g) history.back(); else showList(); }
  window.addEventListener("popstate", (e) => { if (e.state && e.state.g) openGroup(e.state.g, "none"); else showList(true); });

  // ------------------------------------------------------------------ toolbar (group-level actions only)
  function seg(label, key, value) {
    return btn(label, () => { S.filters[key] = value; saveUi(); loadGroups(true); renderToolbar(); }, S.filters[key] === value ? "is-active" : "", { "aria-pressed": String(S.filters[key] === value) });
  }
  function select(options, current, onchange, label) {
    const s = h("select", { onchange: (e) => onchange(e.target.value), "aria-label": label });
    for (const [val, text] of options) { const o = h("option", { value: val }, text); if (val === current) o.selected = true; s.append(o); }
    return s;
  }
  const sep = () => h("span", { class: "gv-sep" });
  const STRATEGIES = [["Highest resolution", "highest_res"], ["Largest file", "largest_size"], ["Newest", "newest"], ["Oldest", "oldest"], ["Shortest path", "shortest_path"]];

  function renderToolbar() {
    const bar = $("toolbar");
    bar.replaceChildren();
    if (S.view === "groups" && !S.groupId) {
      bar.append(seg("Open", "state", "open"), seg("Resolved", "state", "resolved"), seg("All", "state", "all"), sep());
      bar.append(select([["reclaimable", "Most space to free"], ["members", "Most files"], ["size", "Largest total"]], S.filters.sort,
        (v) => { S.filters.sort = v; saveUi(); loadGroups(true); }, "Sort groups"));
      const bases = Object.keys(S.list.basisCounts || {});
      if (bases.length > 1) bar.append(select([["", "Any reason"], ...bases.map((b) => [b, `${b} (${S.list.basisCounts[b]})`])], S.filters.basis,
        (v) => { S.filters.basis = v; saveUi(); loadGroups(true); }, "Filter by reason"));
      bar.append(sep(), btn("Resolve exact copies \u25be", (e) => popMenu(e.currentTarget, STRATEGIES.map(([l, k]) => ["Keep " + l.toLowerCase(), () => bulkKeep(k, l)]))),
        btn("Refresh", refreshAll));
    } else if (S.view === "groups" && S.groupId) {
      bar.append(
        btn("Keep \u25be", (e) => popMenu(e.currentTarget, STRATEGIES.map(([l, k]) => [l, () => autoKeep(k, l)]))),
        btn(S.groupMeta.resolved ? "Reopen" : "Resolve \u2192", toggleResolved, S.groupMeta.resolved ? "" : "gv-btn--primary"),
        btn("Verify", verifyGroup),
        btn(S.selected.size === S.tiles.length && S.tiles.length ? "None" : "All", () => (S.selected.size === S.tiles.length ? clearSel() : selectAll())),
        btn("\u22ef", (e) => popMenu(e.currentTarget, [["Ungroup (not duplicates)", ungroupAll]]), "", { "aria-label": "More group actions" }));
    } else if (S.view === "trash") {
      bar.append(btn("Restore all", () => restoreTrash([])), btn("Empty trash", emptyTrash, "is-danger"), btn("Refresh", loadTrash));
    }
  }

  // ------------------------------------------------------------------ selection bar (bottom, thumb-reachable)
  function renderSelBar() {
    const bar = $("selBar"), n = S.selected.size;
    const show = S.view === "groups" && S.groupId && n > 0;
    bar.hidden = !show;
    document.body.classList.toggle("has-sel", show);
    if (!show) return;
    const hasTwins = !S.expand && S.tiles.some((t) => S.selected.has(t.representative.record_uuid) && t.copy_count > 1);
    bar.replaceChildren(...clean([
      btn("\u2715", clearSel, "", { "aria-label": "Clear selection" }),
      h("span", { class: "gv-selcount" }, String(n), h("span", { class: "gv-hide-narrow", text: " selected" })),
      btn("Trash", () => stageTrash(selectedUuids()), "is-danger"),
      btn("Move", () => pickFolder("move")), btn("Copy", () => pickFolder("copy")),
      n > 1 ? btn("Compare", compareSelected, "gv-hide-narrow") : null,
      btn("\u22ef", (e) => popMenu(e.currentTarget, [
        n > 1 ? ["Compare side by side", compareSelected] : null,
        hasTwins ? ["Trash identical twins only", trashTwins] : null,
        ["Not a duplicate", notDup], ["Invert selection", invert],
      ].filter(Boolean), true), "", { "aria-label": "More actions for selected images" })]));
  }

  function popMenu(anchor, items, upward) {
    const m = $("popMenu");
    m.replaceChildren(...items.map(([label, fn]) => btn(label, () => { m.classList.remove("is-open"); fn(); }, "", { role: "menuitem" })));
    m.classList.add("is-open");
    const r = anchor.getBoundingClientRect(), mh = m.offsetHeight, mw = m.offsetWidth;
    m.style.left = Math.max(8, Math.min(r.left, window.innerWidth - mw - 8)) + "px";
    m.style.top = (upward ? Math.max(8, r.top - mh - 6) : r.bottom + 4) + "px";
    const first = m.querySelector("button"); if (first) first.focus();
  }
  document.addEventListener("click", (e) => { if (!e.target.closest("#popMenu") && !e.target.closest(".gv-toolbar button") && !e.target.closest(".gv-selbar button")) $("popMenu").classList.remove("is-open"); });

  // ------------------------------------------------------------------ banner (scan / grouping status)
  async function loadBanner() {
    if (!S.session) { $("banner").hidden = true; return; }
    const d = await fetch("/api/scan_status?" + q({ session: S.session })).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    const b = $("banner");
    if (!d) { b.hidden = true; return; }
    let msg = "";
    if (d.scan.status === "running") msg = `A scan is running (${d.scan.last_checkpoint_processed || 0} images so far). New groups will appear after Quick Group.`;
    else if (d.scan.status === "interrupted") msg = "The last scan was interrupted. Resume it with Start Scan in the menu - nothing is lost.";
    else if (d.scan.status === "error") msg = "The last scan hit an error: " + (d.scan.last_error || "unknown");
    else if (d.group.status && String(d.group.status).startsWith("running")) msg = "Grouping is running...";
    else if (d.group.status === "error") msg = "The last grouping hit an error: " + (d.group.last_error || "unknown");
    b.textContent = msg; b.hidden = !msg;
  }
  setInterval(() => { if (!document.hidden && S.view === "groups" && !S.groupId) loadBanner(); }, 8000);

  // ------------------------------------------------------------------ group list
  async function loadGroups(reset) {
    if (!S.session) { renderCards(); return; }
    if (reset) { S.list.offset = 0; S.list.items = []; }
    $("listSummary").textContent = "Loading...";
    const data = await api("/api/groups?" + q({ session: S.session, q: S.filters.q, state: S.filters.state, basis: S.filters.basis, sort: S.filters.sort, limit: 100, offset: S.list.offset }));
    if (data.error) { $("listSummary").textContent = ""; return; }
    S.list.items = S.list.items.concat(data.items);
    Object.assign(S.list, { total: data.total, basisCounts: data.basis_counts || {}, reclaimable: data.reclaimable_total, offset: S.list.items.length, loaded: true });
    renderCards(); renderToolbar();
    $("statsPanel").textContent = `${data.total} group${data.total === 1 ? "" : "s"} shown\n${fmtBytes(data.reclaimable_total)} can be freed`;
  }

  function renderCards() {
    const box = $("groupCards"), empty = $("emptyState");
    box.replaceChildren();
    for (const g of S.list.items) {
      const extra = g.member_count - 1;
      box.append(h("button", { class: "gv-card", type: "button", onclick: () => openGroup(g.group_id, "push") },
        h("div", { class: "gv-thumbs" }, g.thumb_uuids.map((u) => h("img", { src: `/thumb/${S.session}/${u}`, loading: "lazy", alt: "" }))),
        h("div", { style: "min-width:0" },
          h("div", { class: "gv-card__name", text: g.sample_name + (extra > 0 ? `  +${extra} more` : "") }),
          h("div", { class: "gv-card__meta" },
            h("span", { class: "gv-chip", title: g.match_basis, text: g.reason }),
            S.filters.state !== "open" ? h("span", { class: "gv-chip" + (g.resolved ? " gv-chip--ok" : ""), text: g.resolved ? "resolved" : "open" }) : null,
            h("span", { text: `${g.member_count} files` }),
            h("span", { text: g.folders.join(" \u00b7 ") }))),
        h("div", { class: "gv-card__reclaim" }, fmtBytes(g.reclaimable_bytes), h("small", { text: " can be freed" }))));
    }
    $("btnMore").hidden = S.list.items.length >= S.list.total;
    const filtered = S.filters.q || S.filters.basis || S.filters.state !== "open";
    const totalAll = Object.values(S.list.basisCounts || {}).reduce((a, b) => a + b, 0);
    $("listSummary").textContent = S.list.items.length ? `Showing ${S.list.items.length} of ${S.list.total} group${S.list.total === 1 ? "" : "s"}` : "";
    if (S.list.items.length || !S.list.loaded && S.session) { empty.hidden = true; if (!S.session) renderEmpty(empty); return; }
    renderEmpty(empty, filtered, totalAll);
  }

  function renderEmpty(empty, filtered, totalAll) {
    empty.hidden = false; empty.replaceChildren();
    if (!S.session) {
      empty.append(h("h3", { text: "No sessions yet" }), h("p", { text: "Create one from the terminal menu: New Session, then Folder Scan, then Start Scan." }));
    } else if (filtered && totalAll > 0 && S.filters.state === "open" && !S.filters.q && !S.filters.basis) {
      empty.append(h("h3", { text: "All caught up" }), h("p", { text: "Every group is resolved." }), btn("Show resolved groups", () => { S.filters.state = "resolved"; saveUi(); loadGroups(true); }, "gv-btn"));
    } else if (filtered) {
      empty.append(h("h3", { text: "No groups match" }), h("p", { text: "Try a different search or filter." }),
        btn("Clear filters", () => { S.filters = { q: "", state: "open", basis: "", sort: S.filters.sort }; $("filterInput").value = ""; saveUi(); loadGroups(true); }, "gv-btn"));
    } else {
      empty.append(h("h3", { text: "No duplicate groups yet" }),
        h("p", { text: "Run Quick Group (menu option 8) to find candidates from file size and photo time, or Deep Group (9) to find look-alikes. Then refresh." }), btn("Refresh", refreshAll, "gv-btn"));
    }
  }
  $("btnMore").addEventListener("click", () => loadGroups(false));

  function showList(fromPop) {
    S.groupId = null; S.selected.clear(); S.verdicts = {};
    $("groupListView").hidden = false; $("groupDetailView").hidden = true;
    if (!fromPop && history.state && history.state.g) history.replaceState(null, "");
    setView("groups"); loadGroups(true); loadBanner();
  }

  // ------------------------------------------------------------------ group detail
  async function openGroup(gid, historyMode) {
    const data = await api(`/api/group/${gid}/members?` + q({ session: S.session, expand: S.expand ? 1 : 0 }));
    if (data.error) { if (S.groupId === gid || historyMode === "none") showList(true); return false; }
    S.groupId = gid; S.tiles = data.tiles; S.selected.clear(); S.verdicts = {};
    S.groupMeta = { resolved: data.resolved, basis: data.match_basis };
    if (historyMode === "push") history.pushState({ g: gid }, ""); else if (historyMode === "replace") history.replaceState({ g: gid }, "");
    $("groupDetailTitle").textContent = `${baseName((data.tiles[0] || {}).representative && data.tiles[0].representative.file_path) || "Group"} \u00b7 ${data.member_count} files`;
    $("groupBasis").textContent = `Grouped because: ${data.match_basis}`;
    $("groupListView").hidden = true; $("groupDetailView").hidden = false;
    applyDensity(); updatePager(); renderTiles(); updateExpandBtn(); setView("groups");
    window.scrollTo(0, 0);
    return true;
  }

  function listIndex() { return S.list.items.findIndex((g) => g.group_id === S.groupId); }
  function updatePager() {
    const i = listIndex();
    $("pagerText").textContent = i >= 0 ? `${i + 1} of ${S.list.total}` : "";
    $("btnPrev").disabled = i <= 0; $("btnNext").disabled = i < 0 || (i >= S.list.items.length - 1 && S.list.items.length >= S.list.total);
  }
  async function neighbor(delta, mode) {
    if (!S.list.loaded) await loadGroups(true);
    let i = listIndex();
    if (i < 0) return false;
    if (delta > 0 && i >= S.list.items.length - 1 && S.list.items.length < S.list.total) { await loadGroups(false); i = listIndex(); }
    const target = S.list.items[i + delta];
    if (!target) { toast(delta > 0 ? "That was the last group." : "This is the first group."); return false; }
    return openGroup(target.group_id, mode || "replace");
  }
  $("btnPrev").addEventListener("click", () => neighbor(-1));
  $("btnNext").addEventListener("click", () => neighbor(1));

  function applyDensity() {
    $("tileGrid").dataset.density = S.density;
    document.querySelectorAll("[data-density]").forEach((b) => { const on = b.dataset.density === S.density; b.classList.toggle("is-active", on); b.setAttribute("aria-pressed", String(on)); });
  }
  document.querySelectorAll("[data-density]").forEach((b) => b.addEventListener("click", () => { S.density = b.dataset.density; try { localStorage.setItem(DENSITY_KEY, S.density); } catch (e) { /* ignore */ } applyDensity(); }));

  function updateExpandBtn() {
    const b = $("btnExpand"), has = S.tiles.some((t) => t.twins > 0);
    b.hidden = !(has || S.expand);
    b.textContent = S.expand ? "Group copies" : "Each copy";
    b.title = S.expand ? "Merge byte-identical copies into one tile" : "Show every identical copy separately so you can choose which to keep";
    b.setAttribute("aria-pressed", String(S.expand));
  }
  $("btnExpand").addEventListener("click", () => { S.expand = !S.expand; openGroup(S.groupId, "none"); });

  function bestTileUuid() {
    if (S.tiles.length < 2) return null;
    const keyOf = (t) => pixels(t.representative) + ":" + (t.representative.file_size_bytes || 0);
    if (new Set(S.tiles.map(keyOf)).size < 2) return null;                  // all the same size/dimensions - no "best"
    return S.tiles.reduce((a, b) => ((pixels(b.representative) - pixels(a.representative)) || ((b.representative.file_size_bytes || 0) - (a.representative.file_size_bytes || 0))) > 0 ? b : a).representative.record_uuid;
  }

  function renderTiles() {
    const grid = $("tileGrid");
    grid.replaceChildren();
    const best = bestTileUuid();
    const groupHasTrash = S.tiles.some((t) => t.copies.some((c) => S.pending.get(c) === "trash"));
    for (const t of S.tiles) {
      const r = t.representative, id = r.record_uuid, v = S.verdicts[id], act = S.pending.get(id), sel = S.selected.has(id);
      // A collapsed tile stands for several identical files: the ribbon must reflect ALL of them, not just the representative.
      const queued = t.copies.filter((c) => S.pending.get(c) === "trash").length;
      let ribbon = null, queuedNote = "";
      if (t.copy_count > 1 && queued > 0 && queued < t.copy_count) {
        ribbon = act === "trash" ? h("span", { class: "gv-ribbon gv-ribbon--trash", text: `TRASH ${queued} OF ${t.copy_count}` })
          : h("span", { class: "gv-ribbon gv-ribbon--keep", text: `KEEP 1 \u00b7 TRASH ${queued}` });
        queuedNote = `, ${queued} of ${t.copy_count} identical copies queued to trash`;
      } else if (act) {
        ribbon = h("span", { class: "gv-ribbon" + (act === "trash" ? " gv-ribbon--trash" : ""), text: act.toUpperCase() + (t.copy_count > 1 && queued === t.copy_count ? ` \u00d7${queued}` : "") });
      } else if (groupHasTrash) {
        ribbon = h("span", { class: "gv-ribbon gv-ribbon--keep", text: "KEEP" });
      }
      const tile = h("div", { class: "gv-tile" + (sel ? " is-selected" : "") + (r.missing ? " is-missing" : ""), role: "checkbox", tabindex: "0", "aria-checked": String(sel),
        "aria-label": `${baseName(r.file_path)}, ${r.width} by ${r.height}, ${fmtBytes(r.file_size_bytes)}${act ? ", queued to " + act : ""}${queuedNote}`, "data-uuid": id },
        h("div", { class: "gv-tile__img" + (ribbon ? " has-ribbon" : "") },
          h("img", { src: `/thumb/${S.session}/${id}`, loading: "lazy", alt: "" }),
          h("div", { class: "gv-tick", "aria-hidden": "true", text: sel ? "\u2713" : "" }),
          h("button", { class: "gv-zoom", type: "button", "aria-label": "View full size", text: "\u2922", onclick: (e) => { e.stopPropagation(); openLightbox([r], 0, "single"); } }),
          ribbon,
          h("div", { class: "gv-flags" },
            id === best ? h("span", { class: "gv-flag gv-flag--best", text: "\u2605 Largest" }) : null,
            t.twins > 0 ? h("span", { class: "gv-flag gv-flag--same", text: S.expand ? `same as ${t.twins} other${t.twins === 1 ? "" : "s"}` : `+${t.twins} identical` }) : null,
            v ? h("span", { class: "gv-flag" + (v.diff ? " gv-flag--diff" : " gv-flag--same"), text: v.text }) : null)),
        h("div", { class: "gv-meta" }, h("strong", { text: baseName(r.file_path) }),
          h("span", { text: `${r.width}\u00d7${r.height} \u00b7 ${fmtBytes(r.file_size_bytes)}` }),
          h("span", { text: `${shortDate(r)} \u00b7 ${parentName(r.file_path)}` })));
      tile.addEventListener("click", () => toggle(id));
      tile.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(id); } });
      let timer = null;
      tile.addEventListener("touchstart", () => { timer = setTimeout(() => { timer = "fired"; openLightbox([r], 0, "single"); }, 600); }, { passive: true });
      tile.addEventListener("touchend", (e) => { if (timer === "fired") e.preventDefault(); clearTimeout(timer); });
      tile.addEventListener("touchmove", () => clearTimeout(timer), { passive: true });
      grid.append(tile);
    }
    renderToolbar(); renderSelBar();
  }

  function toggle(id) {
    S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
    const tile = $("tileGrid").querySelector(`[data-uuid="${id}"]`);
    if (tile) { const on = S.selected.has(id); tile.classList.toggle("is-selected", on); tile.setAttribute("aria-checked", String(on)); tile.querySelector(".gv-tick").textContent = on ? "\u2713" : ""; }
    renderToolbar(); renderSelBar();
  }
  const selectedUuids = () => [...S.selected];
  function selectAll() { S.tiles.forEach((t) => S.selected.add(t.representative.record_uuid)); renderTiles(); }
  function clearSel() { S.selected.clear(); renderTiles(); }
  function invert() { S.tiles.forEach((t) => { const id = t.representative.record_uuid; S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id); }); renderTiles(); }

  // ------------------------------------------------------------------ group actions
  async function verifyGroup() {
    toast("Checking files - hashing this group...", null, null, 15000);
    const res = await api(`/api/group/${S.groupId}/verify`, { session: S.session });
    if (res.error) return null;
    const byUuid = Object.fromEntries(res.members.map((m) => [m.record_uuid, m]));
    S.verdicts = {};
    for (const t of S.tiles) {
      const m = byUuid[t.representative.record_uuid];
      if (!m) continue;
      if (m.identical_to.length) S.verdicts[m.record_uuid] = { text: "identical \u00d7" + (m.identical_to.length + 1), diff: false };
      else if (m.phash_distance_to_anchor === 0) S.verdicts[m.record_uuid] = { text: "reference", diff: false };
      else if (m.similar) S.verdicts[m.record_uuid] = { text: `looks alike (${m.phash_distance_to_anchor})`, diff: false };
      else S.verdicts[m.record_uuid] = { text: m.phash_distance_to_anchor === null ? "no hash" : `different (${m.phash_distance_to_anchor})`, diff: true };
    }
    const words = { all_identical: "Verified: every file is byte-identical.", all_similar: "These look like the same picture, but the files differ (e.g. resized or re-saved).",
      mixed: "Mixed: some of these match and some do not.", unrelated: "These files do not look alike - probably not duplicates." };
    toast(words[res.verdict] || res.verdict, null, null, 7000);
    // re-open so exact twins collapse into one tile now that hashes exist
    const keep = S.verdicts; await openGroup(S.groupId, "none"); S.verdicts = keep; renderTiles();
    return res;
  }

  async function autoKeep(strategy, label) {
    if (S.tiles.some((t) => !t.representative.sha256_hash)) {                 // never pick a "winner" among unverified files blindly
      const res = await verifyGroup();
      if (!res) return;
      if (res.verdict !== "all_identical") {
        const ok = await confirmDialog("Files are not byte-identical", "These files are similar but not the same file (different size or content). Keep one by " + label.toLowerCase() + " and queue the others for trash anyway? Nothing is deleted until you commit.", "Queue the others");
        if (!ok) return;
      }
    }
    const res = await api(`/api/group/${S.groupId}/auto_keep`, { session: S.session, strategy });
    if (res.error) return;
    await loadCart();
    toast(`Keeping 1 (${label.toLowerCase()}); ${res.staged_for_trash} queued for trash.`, "Review", openReview);
  }

  async function bulkKeep(strategy, label) {
    const ok = await confirmDialog("Resolve exact copies in all open groups?",
      `For every open group whose files are proven byte-identical, keep one (${label.toLowerCase()}) and queue the rest for trash. Groups that are only similar are skipped. You review everything before anything is deleted.`, "Check & queue");
    if (!ok) return;
    toast("Checking every open group - this can take a while on big libraries...", null, null, 60000);
    const res = await api("/api/groups/auto_keep_all", { session: S.session, strategy });
    if (res.error) return;
    await loadCart(); loadGroups(true);
    toast(`${res.files_staged_for_trash} file(s) queued across ${res.groups_applied} group(s). ${res.groups_skipped_not_identical} not identical (skipped).` + (res.partial ? " Stopped early (time limit) - run again to continue." : ""), res.files_staged_for_trash ? "Review" : null, openReview, 12000);
  }

  async function toggleResolved() {
    const want = !S.groupMeta.resolved;
    const i = listIndex(), nextId = want && i >= 0 && S.filters.state === "open" ? (S.list.items[i + 1] || S.list.items[i - 1] || {}).group_id : null;
    const res = await api(`/api/group/${S.groupId}/resolve`, { session: S.session, resolved: want });
    if (res.error) return;
    S.groupMeta.resolved = want;
    if (want && S.filters.state === "open" && i >= 0) { S.list.items.splice(i, 1); S.list.total = Math.max(0, S.list.total - 1); S.list.offset = S.list.items.length; }
    if (want && nextId) { toast("Resolved. Next group."); openGroup(nextId, "replace"); }
    else if (want) { toast("Resolved. That was the last open group."); goList(); }
    else { renderToolbar(); toast("Reopened."); }
  }
  async function ungroupAll() {
    if (!await confirmDialog("Ungroup these files?", "This only clears the grouping (use it for false positives). Your files are not touched.", "Ungroup")) return;
    const res = await api(`/api/group/${S.groupId}/ungroup`, { session: S.session });
    if (!res.error) { S.list.loaded = false; goList(); }
  }
  async function notDup() {
    const res = await api(`/api/group/${S.groupId}/remove_member`, { session: S.session, record_uuids: selectedUuids() });
    if (res.error) return;
    toast("Removed from the group and remembered as not a duplicate.");
    S.list.loaded = false;
    res.group_exists ? openGroup(S.groupId, "none") : goList();
  }
  function compareSelected() { openLightbox(S.tiles.map((t) => t.representative).filter((r) => S.selected.has(r.record_uuid)), 0, "compare"); }
  function trashTwins() {
    const uuids = [];
    for (const t of S.tiles) if (S.selected.has(t.representative.record_uuid)) uuids.push(...t.copies.filter((c) => c !== t.representative.record_uuid));
    if (uuids.length) stage("trash", uuids);
  }

  // ------------------------------------------------------------------ staging + review/commit
  async function stageTrash(uuids) {
    const covered = S.tiles.every((t) => uuids.includes(t.representative.record_uuid));
    if (covered && !await confirmDialog("Trash every image in this group?", "That would queue every copy for trash, leaving none. Nothing is deleted until you commit, and trashed files can be restored. Continue?", "Queue all")) return;
    stage("trash", uuids);
  }
  async function stage(action, uuids, dest) {
    const res = await api("/api/stage", { session: S.session, action, record_uuids: uuids, dest_dir: dest || null });
    if (res.error) return;
    S.selected.clear();
    await loadCart();
    toast(`${res.staged} image${res.staged === 1 ? "" : "s"} queued to ${action}.` + (res.skipped && res.skipped.length ? ` ${res.skipped.length} skipped (${res.skipped[0].reason}).` : ""), "Review", openReview);
  }

  async function loadCart() {
    if (!S.session) return;
    const data = await api("/api/stage/summary?" + q({ session: S.session }));
    S.pending = new Map((data.pending || []).map((r) => [r.record_uuid, r.action]));
    const n = (data.pending || []).length, pill = $("pendingPill");
    pill.hidden = n === 0;
    pill.textContent = `Review ${n} pending`;
    pill.onclick = openReview;
    if (S.groupId && S.view === "groups") renderTiles();
  }

  async function openReview() {
    $("toast").hidden = true;
    const [sum, rev] = await Promise.all([api("/api/stage/summary?" + q({ session: S.session })), api("/api/stage/review?" + q({ session: S.session }))]);
    if (sum.error || rev.error) return;
    const rows = sum.pending || [];
    if (!rows.length) { toast("Nothing pending."); return; }
    const losing = Object.keys(rev.groups_losing_every_copy || {});
    const allow = h("input", { type: "checkbox", id: "allowAll" });
    const body = [
      h("p", {}, h("strong", { text: `Trash ${rev.counts.trash}` }), ` (${fmtBytes(rev.bytes.trash)})  \u00b7  Move ${rev.counts.move}  \u00b7  Copy ${rev.counts.copy}`),
      losing.length ? h("div", { class: "gv-warn" }, h("strong", { text: "\u26a0 Every copy would be trashed in " + losing.length + " group(s). " }),
        "To be safe, those items are held back unless you tick this box.", h("br"), h("label", {}, allow, " I understand - trash every copy in those groups")) : null,
      h("div", {}, rows.slice(0, 60).map((r) => h("div", { class: "gv-review-row" },
        h("img", { src: `/thumb/${S.session}/${r.record_uuid}`, alt: "", loading: "lazy" }),
        h("span", { text: `${r.action.toUpperCase()}  ${baseName(r.file_path)}${r.dest_dir ? "  \u2192  " + r.dest_dir : ""}` }),
        btn("\u2715", async () => { await api("/api/stage/remove", { session: S.session, stage_id: r.stage_id }); await loadCart(); openReview(); }, "", { "aria-label": "Remove from queue" })))),
      rows.length > 60 ? h("p", { class: "gv-muted", text: `...and ${rows.length - 60} more` }) : null,
    ];
    openDialog(`Review ${rows.length} pending action${rows.length === 1 ? "" : "s"}`, body, [
      { label: "Close", onclick: () => true },
      { label: "Discard all", onclick: async () => { await api("/api/stage/clear", { session: S.session }); await loadCart(); toast("Discarded the queue."); } },
      { label: "Commit", primary: true, onclick: () => commit(allow.checked) },
    ]);
  }

  async function commit(allowAll) {
    const res = await api("/api/stage/commit", { session: S.session, allow_delete_all_copies: allowAll });
    if (res.error) return;
    S.list.loaded = false;
    const parts = [];
    if (res.committed) parts.push(`Done: ${res.committed} action${res.committed === 1 ? "" : "s"}`);
    if (res.freed_bytes) parts.push(`${fmtBytes(res.freed_bytes)} freed`);
    if (res.blocked_groups && res.blocked_groups.length) parts.push(`${res.blocked_groups.length} group(s) held back - they would lose every copy`);
    if (res.failed && res.failed.length) parts.push(`${res.failed.length} problem(s): ${res.failed[0].file} - ${res.failed[0].error}`);
    const undo = res.trashed_record_uuids && res.trashed_record_uuids.length ? () => restoreTrash(res.trashed_record_uuids) : null;
    toast(parts.join(" \u00b7 ") || "Nothing to do.", undo ? "Undo" : null, undo, 12000);
    await loadCart();
    if (S.groupId) { if ((res.dissolved_groups || []).includes(S.groupId)) { if (!await neighbor(1, "replace")) goList(); } else openGroup(S.groupId, "none"); } else loadGroups(true);
  }

  // ------------------------------------------------------------------ lightbox (single + compare)
  function openLightbox(items, index, mode) { S.lb = { items, index, mode }; renderLightbox(); $("lightboxBackdrop").classList.add("is-open"); }
  function closeLightbox() { $("lightboxBackdrop").classList.remove("is-open"); }
  function renderLightbox() {
    const { items, index, mode } = S.lb;
    const shown = mode === "compare" ? items : [items[index]];
    const maxPx = Math.max(...shown.map(pixels)), maxSz = Math.max(...shown.map((r) => r.file_size_bytes || 0));
    $("lightboxRow").replaceChildren(...shown.map((r) => h("div", { class: "gv-lb-item" },
      h("img", { src: `/image/${S.session}/${r.record_uuid}`, alt: baseName(r.file_path), onclick: (e) => e.currentTarget.classList.toggle("is-zoomed") }),
      h("div", { class: "gv-lb-cap" },
        h("div", {}, h("strong", { text: baseName(r.file_path) })),
        h("div", {}, shown.length > 1 && pixels(r) === maxPx && new Set(shown.map(pixels)).size > 1 ? h("b", { text: `${r.width}\u00d7${r.height} \u2605 ` }) : `${r.width}\u00d7${r.height} `,
          "\u00b7 ", shown.length > 1 && (r.file_size_bytes || 0) === maxSz && new Set(shown.map((x) => x.file_size_bytes)).size > 1 ? h("b", { text: fmtBytes(r.file_size_bytes) + " \u2605" }) : fmtBytes(r.file_size_bytes), ` \u00b7 ${shortDate(r)}`),
        h("div", { text: r.camera_model || "no camera info" }), h("div", { class: "gv-muted", text: r.file_path })))));
    const bar = $("lightboxBar"); bar.replaceChildren();
    if (mode === "single" && items.length > 1) bar.append(btn("\u2039 Prev", () => lbStep(-1)), h("span", { class: "gv-muted", text: `${index + 1} / ${items.length}` }), btn("Next \u203a", () => lbStep(1)));
    if (mode === "single") bar.append(btn("Queue trash", () => { const r = items[index]; closeLightbox(); stage("trash", [r.record_uuid]); }));
    bar.append(btn("Close", closeLightbox));
  }
  function lbStep(d) { const n = S.lb.items.length; S.lb.index = (S.lb.index + d + n) % n; renderLightbox(); }
  $("lightboxBackdrop").addEventListener("click", (e) => { if (e.target === $("lightboxBackdrop")) closeLightbox(); });

  // ------------------------------------------------------------------ folder browser
  function pickFolder(action) { S.pendingTarget = { action, uuids: selectedUuids() }; $("browserTitle").textContent = (action === "move" ? "Move" : "Copy") + " to which folder?"; browse(null); }
  async function browse(path) {
    const data = await api("/api/browse?" + q(path ? { session: S.session, path } : { session: S.session }));
    if (data.error) return;
    S.browserPath = data.current_path;
    $("browserPath").textContent = data.current_path;
    const rows = [];
    if (data.parent_path) rows.push(btn("\u2191 Up", () => browse(data.parent_path), "gv-browser-row"));
    for (const e of data.entries) rows.push(btn("\ud83d\udcc1 " + e.name, () => browse(e.path), "gv-browser-row"));
    if (!data.entries.length) rows.push(h("p", { class: "gv-muted", style: "padding:8px 12px", text: "No sub-folders here." }));
    $("browserGrid").replaceChildren(...rows);
    $("browserBackdrop").classList.add("is-open");
  }
  $("btnCancelBrowse").addEventListener("click", () => { $("browserBackdrop").classList.remove("is-open"); S.pendingTarget = null; });
  $("btnUseFolder").addEventListener("click", async () => {
    $("browserBackdrop").classList.remove("is-open");
    const t = S.pendingTarget; S.pendingTarget = null;
    if (t) await stage(t.action, t.uuids, S.browserPath);
  });

  // ------------------------------------------------------------------ trash
  async function loadTrash() {
    if (!S.session) return;
    const data = await api("/api/trash?" + q({ session: S.session }));
    if (data.error) return;
    $("trashTotal").textContent = `${data.entries.length} file${data.entries.length === 1 ? "" : "s"} \u00b7 ${fmtBytes(data.total_bytes)}`;
    $("trashRows").replaceChildren(...(data.entries.length ? data.entries.map((e) => h("div", { class: "gv-trash-row" },
      h("span", { text: `${baseName(e.original_path)} \u2014 ${fmtBytes(e.size)}${e.exists ? "" : " (file gone)"}` }),
      btn("Restore", () => restoreTrash([e.record_uuid]), "gv-btn"))) : [h("p", { class: "gv-muted", text: "Trash is empty." })]));
  }
  async function restoreTrash(uuids) {
    const res = await api("/api/trash/restore", { session: S.session, record_uuids: uuids });
    if (res.error) return;
    toast(`Restored ${res.restored} file${res.restored === 1 ? "" : "s"}.` + (res.failed.length ? ` ${res.failed.length} failed.` : "") + (res.groups_restored ? ` ${res.groups_restored} group${res.groups_restored === 1 ? "" : "s"} put back.` : ""));
    S.list.loaded = false;
    if (S.view === "trash") loadTrash(); else loadGroups(true);
  }
  async function emptyTrash() {
    if (!await confirmDialog("Empty trash permanently?", "This deletes every file in this session's trash for good. It cannot be undone.", "Delete forever")) return;
    const res = await api("/api/trash/empty", { session: S.session });
    if (!res.error) { toast(`Permanently removed ${res.permanently_removed} file(s).`); loadTrash(); }
  }

  // ------------------------------------------------------------------ notes (autosave)
  let notesTimer = null, notesLoaded = false;
  async function loadNotes() {
    if (notesLoaded) return;
    const data = await api("/api/notes");
    if (!data.error) { $("notesBox").value = data.text || ""; notesLoaded = true; }
  }
  $("notesBox").addEventListener("input", () => {
    $("notesState").textContent = "typing...";
    clearTimeout(notesTimer);
    notesTimer = setTimeout(async () => { const res = await api("/api/notes", { text: $("notesBox").value }); $("notesState").textContent = res.ok ? "saved" : "NOT SAVED"; }, 700);
  });

  // ------------------------------------------------------------------ keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    const tag = (e.target.tagName || "").toLowerCase();
    if (e.key === "Escape") {
      if ($("dialogBackdrop").classList.contains("is-open")) return closeDialog();
      if ($("lightboxBackdrop").classList.contains("is-open")) return closeLightbox();
      if ($("browserBackdrop").classList.contains("is-open")) return $("btnCancelBrowse").click();
      if ($("popMenu").classList.contains("is-open")) return $("popMenu").classList.remove("is-open");
      if (S.selected.size) return clearSel();
      return;
    }
    if (tag === "input" || tag === "textarea" || tag === "select" || e.ctrlKey || e.metaKey || e.altKey) return;
    if ($("lightboxBackdrop").classList.contains("is-open")) {
      if (S.lb.mode === "single") { if (e.key === "ArrowRight") lbStep(1); if (e.key === "ArrowLeft") lbStep(-1); }
      return;
    }
    if ($("dialogBackdrop").classList.contains("is-open") || $("browserBackdrop").classList.contains("is-open")) return;
    if (e.key === "?") { setView("about"); return; }
    if (!(S.view === "groups" && S.groupId)) return;
    const k = e.key.toLowerCase();
    if (k === "j" || e.key === "ArrowRight") neighbor(1);
    else if (k === "k" || e.key === "ArrowLeft") neighbor(-1);
    else if (k === "a") { e.preventDefault(); selectAll(); }
    else if ((k === "t" || e.key === "Delete") && S.selected.size) stageTrash(selectedUuids());
    else if (k === "v") verifyGroup();
    else if (k === "r") toggleResolved();
  });

  // ------------------------------------------------------------------ boot
  function refreshAll() { S.list.loaded = false; loadGroups(true); loadCart(); loadBanner(); }
  (async function init() {
    const saved = restoreUi();
    $("filterInput").value = S.filters.q || "";
    applyDensity();
    setView(saved && ["groups", "trash", "notes", "about"].includes(saved.view) ? saved.view : "groups");
    $("groupListView").hidden = false;
    if (!S.session) { renderCards(); return; }
    loadCart(); loadBanner();
    await loadGroups(true);
    if (saved && saved.groupId && saved.session === S.session) { if (!(await openGroup(saved.groupId, "replace"))) { S.groupId = null; renderToolbar(); } }
  })();
})();
