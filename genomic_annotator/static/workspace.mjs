import {
  arcPath, chromosomeSegments, formatNumber, pageLabel, polar,
  positionAngle, shortStatus, sourceLabel,
} from "./model.mjs";

const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="workspace-token"]').content;
const state = {
  datasets: [], activeId: null, rows: [], selected: null,
  chromosome: "", query: "", status: "", category: "",
  offset: 0, total: 0, limit: 30, view: "circular", zoom: 1,
  busy: false, maxUploadBytes: 32 * 1024 * 1024,
};
let chosenFile = null;
let rowsRequest = null;
let rowsGeneration = 0;
let refreshGeneration = 0;
let pollTimer = null;
let searchTimer = null;

function element(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svg(tag, attributes = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const node = svg("svg", { class: "icon", "aria-hidden": "true" });
  node.append(svg("use", { href: `#i-${name}` }));
  return node;
}

function activeDataset() {
  return state.datasets.find(dataset => dataset.id === state.activeId);
}

function notify(message, error = false) {
  $("toast-message").textContent = message;
  $("toast").setAttribute("role", error ? "alert" : "status");
  $("toast").hidden = false;
}

function reportError(error) {
  if (error.name !== "AbortError") notify(error.message, true);
}

function run(action) {
  return (...args) => Promise.resolve().then(() => action(...args)).catch(reportError);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("X-Workspace-Token", token);
  const response = await fetch(path, { ...options, headers, cache: "no-store", credentials: "omit" });
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("The local server returned an unexpected response. Reload the workspace.");
  }
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Local request failed (${response.status}).`);
  return body;
}

function postJSON(path, body) {
  return api(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

function openDialog(id) {
  const dataset = activeDataset();
  if (id === "online-dialog") {
    if (!dataset || dataset.synthetic) return;
    $("online-filename").textContent = dataset.name;
    $("online-consent").checked = false;
    $("online-error").hidden = true;
    $("online-form").dataset.datasetId = dataset.id;
  }
  if (id === "export-dialog") {
    if (!dataset) return;
    $("export-error").hidden = true;
    $("export-form").dataset.datasetId = dataset.id;
  }
  $(id).showModal();
}

function formError(id, message) {
  $(id).textContent = message;
  $(id).hidden = false;
}

function clearFilterState() {
  clearTimeout(searchTimer);
  state.chromosome = state.query = state.status = state.category = "";
  state.offset = 0;
  state.selected = null;
  for (const id of ["chromosome-filter", "variant-search", "status-filter", "category-filter"]) {
    $(id).value = "";
  }
}

async function switchDataset(id) {
  if (id === state.activeId) return;
  state.activeId = id;
  clearFilterState();
  state.zoom = 1;
  state.rows = [];
  state.total = 0;
  renderWorkspace();
  await loadRows();
}

function renderTabs() {
  const focused = document.activeElement?.id;
  $("file-tabs").replaceChildren();
  for (const dataset of state.datasets) {
    const selected = dataset.id === state.activeId;
    const tab = element("div", `file-tab${selected ? " active" : ""}`);
    const button = element("button");
    button.id = `tab-${dataset.id}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(selected));
    button.title = dataset.name;
    button.append(icon(dataset.synthetic ? "circle" : "file"), element("span", "tab-name", dataset.name));
    if (dataset.synthetic) button.append(element("span", "tab-demo", "DEMO"));
    button.addEventListener("click", run(() => switchDataset(dataset.id)));
    const close = element("button", "icon-button");
    close.id = `close-${dataset.id}`;
    close.setAttribute("aria-label", `Close ${dataset.name}`);
    close.title = `Close ${dataset.name}`;
    close.disabled = ["loading", "annotating"].includes(dataset.phase);
    close.append(icon("close"));
    close.addEventListener("click", run(async () => {
      await api(`/api/datasets/${dataset.id}`, { method: "DELETE" });
      await refreshWorkspace();
    }));
    tab.append(button, close);
    $("file-tabs").append(tab);
  }
  if (focused && $(focused)) $(focused).focus({ preventScroll: true });
}

function renderWorkspace() {
  const dataset = activeDataset();
  const hasRows = Boolean(dataset?.row_count);
  renderTabs();
  $("empty-workspace").hidden = Boolean(dataset);
  $("stat-variants").textContent = hasRows ? formatNumber(dataset.row_count) : "\u2014";
  $("stat-annotated").textContent = hasRows ? formatNumber(dataset.annotated_count) : "\u2014";
  $("stat-chromosomes").textContent = hasRows ? dataset.chromosomes.length : "\u2014";
  $("demo-label").hidden = !dataset?.synthetic;
  $("overview-subtitle").textContent = dataset?.synthetic
    ? "A synthetic genome. A real sense of what's possible."
    : "Your called variants, with a little more context.";
  const pending = state.datasets.find(item => ["loading", "annotating"].includes(item.phase));
  $("operation-banner").hidden = !pending;
  if (pending) {
    $("operation-text").textContent = pending.phase === "loading"
      ? `Opening ${pending.name} locally and checking the cache. No online requests.`
      : `Looking up uncached RSIDs for ${pending.name}. You can browse the previous results below.`;
  }
  $("file-error").hidden = !dataset?.error;
  $("file-error").textContent = dataset?.error || "";
  const busyFile = dataset && ["loading", "annotating"].includes(dataset.phase);
  $("export-button").disabled = !hasRows || busyFile;
  $("online-button").disabled = !hasRows || state.busy || dataset?.synthetic;
  $("online-button").title = dataset?.synthetic ? "Online lookup is disabled for synthetic identifiers." : "";
  $("mode-badge").querySelector("span").textContent = dataset?.offline === false ? "Online results" : "Offline mode";
  $("annotation-mode").textContent = dataset?.synthetic ? "Fictional demo annotations" :
    dataset?.offline === false ? "Cache + online annotations" : "Cache-only annotations";
  $("workspace-status").textContent = dataset?.synthetic ? "Demo data | no network" :
    state.busy ? "Local operation in progress" : "Memory-only genome session";
  const chromosomeFilter = $("chromosome-filter");
  chromosomeFilter.replaceChildren(new Option("All chromosomes", ""));
  for (const chromosome of dataset?.chromosomes || []) {
    chromosomeFilter.append(new Option(`Chromosome ${chromosome.name}`, chromosome.name));
  }
  chromosomeFilter.value = state.chromosome;
  const warnings = dataset?.warnings || [];
  $("warnings-box").hidden = warnings.length === 0;
  $("warnings-summary").textContent = dataset?.synthetic ? "About this synthetic data" :
    `${warnings.length} annotation notice${warnings.length === 1 ? "" : "s"}`;
  $("warnings-list").replaceChildren(...warnings.map(warning => element("li", "", warning)));
  renderMap();
  renderInspector();
  if (!hasRows) {
    state.rows = [];
    state.total = 0;
    renderRows();
  }
}

function selectChromosome(chromosome) {
  state.chromosome = chromosome;
  state.offset = 0;
  state.selected = null;
  $("chromosome-filter").value = chromosome;
  renderMap();
  renderInspector();
  return loadRows();
}

function activateSegment(node, chromosome) {
  node.addEventListener("click", run(() => selectChromosome(
    state.chromosome === chromosome ? "" : chromosome
  )));
  node.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      run(() => selectChromosome(state.chromosome === chromosome ? "" : chromosome))();
    }
  });
}

function renderMap() {
  const dataset = activeDataset();
  const map = $("genome-map");
  const list = $("chromosome-list");
  const segments = chromosomeSegments(dataset?.chromosomes || []);
  const circularVisible = state.view === "circular" && Boolean(dataset?.row_count);
  const focusedId = document.activeElement?.id;
  map.toggleAttribute("hidden", !circularVisible);
  list.hidden = state.view !== "linear" || !dataset?.row_count;
  $("zoom-controls").hidden = !circularVisible;
  $("circular-view").classList.toggle("active", state.view === "circular");
  $("linear-view").classList.toggle("active", state.view === "linear");
  $("circular-view").setAttribute("aria-pressed", String(state.view === "circular"));
  $("linear-view").setAttribute("aria-pressed", String(state.view === "linear"));
  list.replaceChildren();
  const largest = Math.max(1, ...segments.map(segment => segment.count));
  for (const segment of segments) {
    const row = element("button", `chromosome-row${state.chromosome === segment.name ? " active" : ""}`);
    row.id = `linear-chr-${segment.name}`;
    row.setAttribute("aria-pressed", String(state.chromosome === segment.name));
    row.append(element("span", "", `chr${segment.name}`));
    const track = svg("svg", { viewBox: "0 0 200 12", preserveAspectRatio: "none", "aria-hidden": "true" });
    track.append(
      svg("rect", { width: 200, height: 12, rx: 4, fill: "#ecf1ed" }),
      svg("rect", { width: Math.max(3, segment.count / largest * 200), height: 12, rx: 4, fill: segment.color }),
    );
    row.append(track, element("span", "", formatNumber(segment.count)));
    row.addEventListener("click", run(() => selectChromosome(
      state.chromosome === segment.name ? "" : segment.name
    )));
    list.append(row);
  }
  map.replaceChildren(svg("title", {}, "Observed chromosomes and variant density"));
  map.append(svg("desc", {}, "Equal-size chromosome segments, with positions normalized to each chromosome's maximum observed position. Not a reference genome scale. Click a chromosome to filter the variant browser."));
  if (!segments.length) return;
  const cx = 340, cy = 277;
  for (const radius of [151, 176, 233]) {
    map.append(svg("circle", {
      cx, cy, r: radius, fill: "none", stroke: "#e0e7e1", "stroke-width": .65,
      ...(radius === 151 ? { "stroke-dasharray": "1 5" } : {}),
    }));
  }
  for (const segment of segments) {
    const selected = state.chromosome === segment.name;
    const group = svg("g", {
      id: `map-chr-${segment.name}`,
      class: `chromosome-segment${selected ? " selected" : ""}${state.chromosome && !selected ? " dimmed" : ""}`,
      tabindex: 0, role: "button", "aria-pressed": String(selected),
      "aria-label": `Chromosome ${segment.name}, ${formatNumber(segment.count)} called variants`,
    });
    group.append(svg("title", {}, `Chromosome ${segment.name} | ${formatNumber(segment.count)} called variants`));
    group.append(svg("path", {
      d: arcPath(cx, cy, 150, 255, segment.start, segment.end),
      fill: "transparent", "pointer-events": "all",
    }));
    if (selected) {
      group.append(svg("path", {
        d: arcPath(cx, cy, 155, 233, segment.start, segment.end),
        fill: segment.color, opacity: .16,
      }));
    }
    group.append(svg("path", {
      class: "outer-arc", d: arcPath(cx, cy, 216, 230, segment.start, segment.end),
      fill: segment.color,
    }));
    const binStep = (segment.end - segment.start) / segment.bins.length;
    const maximum = Math.max(1, ...segment.bins);
    segment.bins.forEach((count, index) => {
      if (!count) return;
      const start = segment.start + index * binStep;
      group.append(svg("path", {
        d: arcPath(cx, cy, 181, 183 + Math.sqrt(count / maximum) * 24, start, start + binStep * .82),
        fill: segment.color, opacity: .78,
      }));
    });
    group.append(svg("path", {
      d: arcPath(cx, cy, 162, 166, segment.start, segment.end), fill: "#e8ede7",
    }));
    if (segment.annotated) {
      group.append(svg("path", {
        d: arcPath(cx, cy, 162, 166, segment.start,
          segment.start + (segment.end - segment.start) * segment.annotated / segment.count),
        fill: segment.color,
      }));
    }
    const [x, y] = polar(cx, cy, 246, (segment.start + segment.end) / 2);
    group.append(svg("text", { x, y, class: "map-chromosome-label" }, segment.name));
    activateSegment(group, segment.name);
    map.append(group);
  }
  const current = segments.find(segment => segment.name === state.chromosome);
  map.append(
    svg("circle", { cx, cy, r: 121, fill: "#f8faf7", stroke: "#eaf0e6", "stroke-width": .7 }),
    svg("text", { x: cx, y: cy - 41, class: "map-center-eyebrow" }, current ? `CHROMOSOME ${current.name}` : "YOUR GENOME"),
    svg("text", { x: cx, y: cy + 2, class: "map-center-count" }, formatNumber(current?.count ?? dataset.row_count)),
    svg("text", { x: cx, y: cy + 24, class: "map-center-label" }, "called variants"),
    svg("line", { x1: cx - 15, x2: cx + 15, y1: cy + 43, y2: cy + 43, stroke: "#c8d6c4", "stroke-width": 1 }),
    svg("text", { x: cx, y: cy + 63, class: "map-center-note" }, current ? `${formatNumber(current.annotated)} with annotations` : `${segments.length} chromosomes \u00b7 one local workspace`),
    svg("text", { x: cx, y: 548, class: "map-center-note" }, "Observed input positions \u00b7 not to scale"),
  );
  if (state.selected) {
    const segment = segments.find(item => item.name === state.selected.chromosome);
    if (segment) {
      const angle = positionAngle(segment, state.selected.position);
      const [x1, y1] = polar(cx, cy, 145, angle);
      const [x2, y2] = polar(cx, cy, 239, angle);
      const [tx, ty] = polar(cx, cy, 268, angle);
      const marker = svg("g", { class: "selection-marker", "aria-hidden": "true" });
      marker.append(
        svg("line", { x1, y1, x2, y2, stroke: "#008d89", "stroke-width": 1 }),
        svg("circle", { cx: x2, cy: y2, r: 3, fill: "#008d89", stroke: "#f7f9f8", "stroke-width": 1.5 }),
        svg("text", { x: tx, y: ty, "text-anchor": tx < cx - 20 ? "end" : tx > cx + 20 ? "start" : "middle" },
          state.selected.rsid.length > 18 ? `${state.selected.rsid.slice(0, 16)}...` : state.selected.rsid),
      );
      map.append(marker);
    }
  }
  applyZoom();
  if (focusedId?.startsWith("map-chr-") || focusedId?.startsWith("linear-chr-")) {
    $(focusedId)?.focus({ preventScroll: true });
  }
}

function applyZoom() {
  const width = 680 / state.zoom, height = 570 / state.zoom;
  $("genome-map").setAttribute("viewBox", `${340 - width / 2} ${285 - height / 2} ${width} ${height}`);
  $("zoom-value").textContent = `${Math.round(state.zoom * 100)}%`;
  $("zoom-in").disabled = state.zoom >= 1.8;
  $("zoom-out").disabled = state.zoom <= .8;
}

function renderInspector() {
  const row = state.selected;
  $("selected-rsid").textContent = row?.rsid || "Select a variant";
  $("selected-gene").hidden = !row?.gene;
  $("selected-gene").textContent = row?.gene || "";
  $("selected-category").className = `badge ${row?.category || "unavailable"}`;
  $("selected-category").textContent = row ? row.clinical_significance || "No classification" : "No selection";
  $("selected-category").title = row?.clinical_significance || "";
  $("selected-trait").textContent = row ? row.trait_summary || "No trait annotation is available for this RSID. Missing information does not imply a benign result." :
    "Choose a row in the variant browser to see its public annotations and local genotype.";
  $("selected-message").textContent = row?.annotation_message || "Annotations describe RSIDs, not your personal health.";
  $("selected-chromosome").textContent = row?.chromosome || "\u2014";
  $("selected-position").textContent = row ? `${formatNumber(row.position)} bp` : "\u2014";
  $("selected-genotype").textContent = row?.genotype || "\u2014";
  $("selected-source").textContent = row ? sourceLabel(row.annotation_source) : "\u2014";
}

function selectRow(row) {
  state.selected = row;
  for (const button of $("variant-list").querySelectorAll(".variant-row")) {
    const selected = Number(button.dataset.rowId) === row.row_id;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
  renderInspector();
  renderMap();
}

function renderRows() {
  const list = $("variant-list");
  list.replaceChildren();
  for (const row of state.rows) {
    const item = element("div");
    item.setAttribute("role", "listitem");
    const button = element("button", `variant-row${state.selected?.row_id === row.row_id ? " selected" : ""}`);
    button.dataset.rowId = row.row_id;
    button.setAttribute("aria-pressed", String(state.selected?.row_id === row.row_id));
    button.setAttribute("aria-label", `${row.rsid}, chromosome ${row.chromosome}, position ${row.position}, genotype ${row.genotype}, ${shortStatus(row)}`);
    const top = element("span", "variant-row-top");
    const identity = element("span", "variant-identity");
    identity.append(element("strong", "", row.rsid));
    if (row.gene) {
      const gene = element("span", "variant-gene", row.gene);
      gene.title = row.gene;
      identity.append(gene);
    }
    const call = element("span", "genotype-call");
    call.setAttribute("aria-hidden", "true");
    for (const base of row.genotype) call.append(element("span", `base base-${base.toLowerCase()}`, base));
    top.append(identity, call);
    const bottom = element("span", "variant-row-bottom");
    bottom.append(element("span", "variant-position", `chr${row.chromosome} : ${formatNumber(row.position)}`));
    const status = element("span", "variant-status");
    status.title = shortStatus(row);
    status.append(element("span", `category-dot ${row.category}`), element("span", "", shortStatus(row)));
    bottom.append(status);
    button.append(top, bottom);
    button.addEventListener("click", () => selectRow(row));
    item.append(button);
    list.append(item);
  }
  $("variant-total").textContent = formatNumber(state.total);
  const hasRows = Boolean(activeDataset()?.row_count);
  $("no-results").hidden = !hasRows || state.rows.length > 0;
  list.hidden = hasRows && state.rows.length === 0;
  $("page-label").textContent = hasRows ? pageLabel(state.offset, state.rows.length, state.total) : "No variants loaded";
  $("previous-page").disabled = state.offset === 0 || !hasRows;
  $("next-page").disabled = !hasRows || state.offset + state.rows.length >= state.total;
  $("clear-filters").hidden = ![state.query, state.chromosome, state.status, state.category].some(Boolean);
}

async function loadRows() {
  rowsRequest?.abort();
  const generation = ++rowsGeneration;
  const dataset = activeDataset();
  if (!dataset?.row_count) {
    state.rows = [];
    state.selected = null;
    state.total = 0;
    $("variant-list").setAttribute("aria-busy", "false");
    $("variant-list").inert = false;
    renderRows();
    renderInspector();
    return;
  }
  rowsRequest = new AbortController();
  const params = new URLSearchParams({
    chromosome: state.chromosome, query: state.query, status: state.status, category: state.category,
    offset: state.offset, limit: state.limit,
  });
  $("variant-list").setAttribute("aria-busy", "true");
  $("variant-list").inert = true;
  $("previous-page").disabled = true;
  $("next-page").disabled = true;
  try {
    const response = await api(`/api/datasets/${dataset.id}/variants?${params}`, { signal: rowsRequest.signal });
    // A slower search or a previous file must never replace the current view.
    if (generation !== rowsGeneration || dataset.id !== state.activeId) return;
    state.rows = response.rows;
    state.total = response.total;
    state.selected = state.rows.find(row => row.row_id === state.selected?.row_id) || state.rows[0] || null;
    renderRows();
    renderInspector();
    renderMap();
    $("variant-list").scrollTop = 0;
  } catch (error) {
    if (generation === rowsGeneration && error.name !== "AbortError") {
      state.rows = [];
      state.selected = null;
      state.total = 0;
      renderRows();
      renderInspector();
      renderMap();
      $("no-results").hidden = true;
      $("page-label").textContent = "Could not load these results. Change a filter or reload to retry.";
      $("previous-page").disabled = true;
      $("next-page").disabled = true;
      reportError(error);
    }
  } finally {
    if (generation === rowsGeneration) {
      $("variant-list").setAttribute("aria-busy", "false");
      $("variant-list").inert = false;
    }
  }
}

async function refreshWorkspace() {
  const generation = ++refreshGeneration;
  const response = await api("/api/workspace");
  if (generation !== refreshGeneration) return;
  const previous = activeDataset();
  const oldMetadata = JSON.stringify(state.datasets);
  const oldBusy = state.busy;
  state.datasets = response.datasets;
  state.busy = response.busy;
  state.maxUploadBytes = response.max_upload_bytes;
  if (!state.datasets.some(dataset => dataset.id === state.activeId)) {
    state.activeId = state.datasets[0]?.id || null;
    clearFilterState();
  }
  const current = activeDataset();
  if (oldMetadata !== JSON.stringify(state.datasets) || oldBusy !== state.busy || !current) renderWorkspace();
  if (previous?.id !== current?.id || previous?.revision !== current?.revision || !current) {
    await loadRows();
  }
  for (const [id, labels] of [["status-filter", response.status_labels], ["category-filter", response.category_labels]]) {
    if ($(id).options.length === 1) {
      for (const [value, label] of Object.entries(labels)) $(id).append(new Option(label, value));
    }
  }
  clearTimeout(pollTimer);
  if (state.busy) {
    pollTimer = setTimeout(() => refreshWorkspace().catch(error => {
      $("workspace-status").textContent = "Disconnected | reload to reconnect";
      reportError(error);
    }), 1200);
  }
}

async function openDemo() {
  const dataset = await api("/api/demo", { method: "POST" });
  state.activeId = dataset.id;
  clearFilterState();
  await refreshWorkspace();
}

function chooseFile(file) {
  chosenFile = file || null;
  $("chosen-file").textContent = chosenFile?.name || "Choose a file or drop it here";
  $("import-error").hidden = true;
}

for (const button of document.querySelectorAll("[data-dialog]")) {
  button.addEventListener("click", () => openDialog(button.dataset.dialog));
}
for (const button of document.querySelectorAll("[data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
}
$("dismiss-toast").addEventListener("click", () => { $("toast").hidden = true; });
$("export-button").addEventListener("click", () => openDialog("export-dialog"));
$("online-button").addEventListener("click", () => openDialog("online-dialog"));
$("load-demo").addEventListener("click", run(openDemo));
$("genome-file").addEventListener("change", event => chooseFile(event.target.files[0]));
$("drop-zone").addEventListener("dragover", event => {
  event.preventDefault();
  $("drop-zone").classList.add("dragging");
});
$("drop-zone").addEventListener("dragleave", () => $("drop-zone").classList.remove("dragging"));
$("drop-zone").addEventListener("drop", event => {
  event.preventDefault();
  $("drop-zone").classList.remove("dragging");
  if (event.dataTransfer.files.length !== 1) {
    formError("import-error", "Choose one genome file at a time.");
    return;
  }
  chooseFile(event.dataTransfer.files[0]);
});
$("import-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!chosenFile) return formError("import-error", "Choose a 23andMe raw file first.");
  if (chosenFile.size > state.maxUploadBytes) {
    return formError("import-error", "This file exceeds the 32 MiB browser limit. Use the CLI for larger files.");
  }
  $("import-submit").disabled = true;
  $("import-error").hidden = true;
  try {
    const dataset = await api("/api/datasets", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "X-Genome-Name": encodeURIComponent(chosenFile.name) },
      body: chosenFile,
    });
    state.activeId = dataset.id;
    clearFilterState();
    $("import-dialog").close();
    $("import-form").reset();
    chooseFile(null);
    await refreshWorkspace();
  } catch (error) {
    if ($("import-dialog").open) formError("import-error", error.message);
    else reportError(error);
  } finally {
    $("import-submit").disabled = false;
  }
});
$("online-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!$("online-consent").checked) return formError("online-error", "Consent is required to query RSIDs online.");
  $("online-submit").disabled = true;
  $("online-error").hidden = true;
  try {
    await postJSON(`/api/datasets/${event.currentTarget.dataset.datasetId}/annotate`, { allow_online: true });
    $("online-dialog").close();
    await refreshWorkspace();
  } catch (error) {
    if ($("online-dialog").open) formError("online-error", error.message);
    else reportError(error);
  } finally {
    $("online-submit").disabled = false;
  }
});
$("export-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("export-submit").disabled = true;
  $("export-error").hidden = true;
  try {
    const response = await postJSON(`/api/datasets/${event.currentTarget.dataset.datasetId}/export`, {
      path: $("export-path").value,
    });
    $("export-dialog").close();
    notify(`Private report saved to ${response.path} (${formatNumber(response.row_count)} rows).`);
  } catch (error) {
    if ($("export-dialog").open) formError("export-error", error.message);
    else reportError(error);
  } finally {
    $("export-submit").disabled = false;
  }
});

function toggleFilters() {
  $("filters").hidden = !$("filters").hidden;
  $("filter-toggle").setAttribute("aria-expanded", String(!$("filters").hidden));
}
$("filter-toggle").addEventListener("click", toggleFilters);
$("toolbar-filters").addEventListener("click", () => {
  if ($("filters").hidden) toggleFilters();
  $("status-filter").focus();
});
$("focus-search").addEventListener("click", () => $("variant-search").focus());
$("variant-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  state.query = $("variant-search").value.trim();
  state.offset = 0;
  state.selected = null;
  rowsRequest?.abort();
  searchTimer = setTimeout(run(loadRows), 200);
});
$("chromosome-filter").addEventListener("change", run(() => selectChromosome($("chromosome-filter").value)));
for (const [id, key] of [["status-filter", "status"], ["category-filter", "category"]]) {
  $(id).addEventListener("change", run(() => {
    state[key] = $(id).value;
    state.offset = 0;
    state.selected = null;
    return loadRows();
  }));
}
for (const id of ["clear-filters", "reset-filters"]) {
  $(id).addEventListener("click", run(() => {
    clearFilterState();
    return loadRows();
  }));
}
$("previous-page").addEventListener("click", run(() => {
  state.offset = Math.max(0, state.offset - state.limit);
  return loadRows();
}));
$("next-page").addEventListener("click", run(() => {
  if (state.offset + state.rows.length < state.total) state.offset += state.limit;
  return loadRows();
}));
$("reset-map").addEventListener("click", run(() => {
  state.zoom = 1;
  return selectChromosome("");
}));
for (const [id, view] of [["circular-view", "circular"], ["linear-view", "linear"]]) {
  $(id).addEventListener("click", () => { state.view = view; renderMap(); });
}
for (const [id, delta] of [["zoom-in", .1], ["zoom-out", -.1]]) {
  $(id).addEventListener("click", () => {
    state.zoom = Math.max(.8, Math.min(1.8, Math.round((state.zoom + delta) * 10) / 10));
    applyZoom();
  });
}
$("zoom-fit").addEventListener("click", () => { state.zoom = 1; applyZoom(); });
$("variant-list").addEventListener("keydown", event => {
  if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
  const current = event.target.closest(".variant-row");
  if (!current) return;
  const index = state.rows.findIndex(row => row.row_id === Number(current.dataset.rowId));
  const next = state.rows[index + (event.key === "ArrowDown" ? 1 : -1)];
  if (!next) return;
  event.preventDefault();
  selectRow(next);
  $("variant-list").querySelector(`[data-row-id="${next.row_id}"]`).focus();
});
document.addEventListener("keydown", event => {
  const typing = event.target instanceof HTMLElement &&
    (event.target.matches("input, textarea, select") || event.target.isContentEditable);
  if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    $("variant-search").focus();
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) run(refreshWorkspace)();
});

run(async () => {
  await refreshWorkspace();
  if (state.datasets.length === 0) await openDemo();
})();
