"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

const model = {
  state: null,
  listing: [],
  demos: [],
  demo: "hola",
  dirty: false,
  numberMode: "hex",
  previousRegisters: null,
  running: false,
  runToken: 0,
  memoryStart: 0,
  memoryCount: 64,
};

const registerRoles = [
  "ZERO", "ARG / RET", "ARG", "ARG", "TEMP", "TEMP", "TEMP", "TEMP",
  "SAVED", "SAVED", "SAVED", "SAVED", "SAVED", "SAVED", "GENERAL", "DEPTH",
];

const integerFormatter = new Intl.NumberFormat("es-MX");

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.blob();
  if (!response.ok || (payload && payload.ok === false)) {
    throw new Error(payload?.error || `HTTP ${response.status}`);
  }
  setConnection(true);
  return payload;
}

function jsonOptions(payload) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

async function action(payload) {
  return api("/api/action", jsonOptions({
    ...payload,
    memory_start: model.memoryStart,
    memory_count: model.memoryCount,
  }));
}

function setConnection(online, detail = "VM32 local conectada") {
  const dot = $("#connectionDot");
  dot.classList.toggle("online", online);
  dot.classList.toggle("offline", !online);
  $("#connectionText").textContent = detail;
}

function toast(message, type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type === "error" ? "error" : ""}`;
  item.textContent = message;
  $("#toastRegion").append(item);
  window.setTimeout(() => item.remove(), type === "error" ? 5200 : 3000);
}

function setCompileStatus(message, type = "pending") {
  const status = $("#compileStatus");
  status.className = `compile-status ${type}`;
  $("span", status).textContent = message;
}

function parseInteger(value) {
  const text = String(value).trim().toLowerCase();
  if (!text) return 0;
  if (/^[+-]?0x[0-9a-f]+$/.test(text)) {
    const sign = text.startsWith("-") ? -1 : 1;
    return sign * Number.parseInt(text.replace(/^[+-]?0x/, ""), 16);
  }
  if (/^[+-]?0b[01]+$/.test(text)) {
    const sign = text.startsWith("-") ? -1 : 1;
    return sign * Number.parseInt(text.replace(/^[+-]?0b/, ""), 2);
  }
  const parsed = Number.parseInt(text, 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function hex(value, width = 8) {
  return `0x${(Number(value) >>> 0).toString(16).toUpperCase().padStart(width, "0")}`;
}

function numberValue(value) {
  if (model.numberMode === "hex") return hex(value);
  if (model.numberMode === "dec") return String(Number(value) >>> 0);
  return String(Number(value));
}

function compactNumber(value) {
  const number = Number(value);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(number >= 10_000_000 ? 0 : 1)}M`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(number >= 100_000 ? 0 : 1)}K`;
  return String(number);
}

async function bootstrap() {
  try {
    const payload = await api("/api/bootstrap");
    model.demos = payload.demos;
    model.demo = payload.demo;
    model.listing = payload.listing || [];
    populateDemos();
    $("#sourceEditor").value = payload.source;
    updateEditorStats();
    setCompileStatus("Programa listo · ensamblador VM32", "success");
    renderState(payload.state);
  } catch (error) {
    setConnection(false, "No se pudo conectar con el servidor local");
    setCompileStatus(error.message, "error");
    toast(error.message, "error");
  }
}

function populateDemos() {
  const select = $("#demoSelect");
  select.replaceChildren();
  for (const demo of model.demos) {
    const option = document.createElement("option");
    option.value = demo.id;
    option.textContent = demo.label;
    option.selected = demo.id === model.demo;
    select.append(option);
  }
}

async function compileLoad({ quiet = false } = {}) {
  stopRun(false);
  setCompileStatus("Ensamblando…", "pending");
  $("#loadProgram").disabled = true;
  try {
    const payload = await api("/api/load", jsonOptions({
      source: $("#sourceEditor").value,
      demo: model.demo,
      inputs: $("#inputValues").value,
      config: {
        memory_words: $("#settingMemory").value,
        gas_limit: $("#settingGas").value,
        stack_limit: $("#settingStack").value,
        trace_size: $("#settingTrace").value,
        output_limit: $("#settingOutput").value,
        protect_code: $("#settingProtect").checked,
      },
    }));
    model.listing = payload.listing || [];
    model.dirty = false;
    model.memoryStart = 0;
    $("#memoryStart").value = "0x0";
    setCompileStatus(`${model.listing.length} elementos ensamblados · sin errores`, "success");
    renderState(payload.state);
    if (!quiet) toast(payload.message);
    return true;
  } catch (error) {
    setCompileStatus(error.message, "error");
    focusAssemblyError(error.message);
    toast(error.message, "error");
    return false;
  } finally {
    $("#loadProgram").disabled = false;
  }
}

function focusAssemblyError(message) {
  const match = /Línea\s+(\d+)/i.exec(message);
  if (!match) return;
  const targetLine = Number(match[1]);
  const editor = $("#sourceEditor");
  const lines = editor.value.split("\n");
  let start = 0;
  for (let index = 0; index < targetLine - 1; index += 1) start += lines[index].length + 1;
  editor.focus();
  editor.setSelectionRange(start, start + (lines[targetLine - 1]?.length || 0));
}

async function singleStep() {
  if (model.running) return;
  try {
    const payload = await action({ action: "step" });
    renderState(payload.state);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function startRun() {
  if (model.running) {
    stopRun();
    return;
  }
  if (!model.state || ["HALTED", "FAULTED", "WAITING"].includes(model.state.state)) return;
  model.running = true;
  const token = ++model.runToken;
  updateTransport();

  try {
    while (model.running && token === model.runToken) {
      const payload = await action({
        action: "run",
        max_instructions: 2000,
        breakpoints: $("#breakpointValues").value,
      });
      renderState(payload.state);
      const state = payload.state.state;
      const reason = payload.state.reason || "";
      if (["HALTED", "FAULTED", "WAITING"].includes(state)) break;
      if (state === "PAUSED" && !reason.startsWith("Límite local")) break;
      await new Promise((resolve) => window.setTimeout(resolve, 0));
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (token === model.runToken) {
      model.running = false;
      updateTransport();
    }
  }
}

function stopRun(notify = true) {
  if (!model.running) return;
  model.running = false;
  model.runToken += 1;
  updateTransport();
  if (notify) toast("Ejecución por bloques detenida");
}

async function resetVM() {
  stopRun(false);
  try {
    const payload = await action({ action: "reset", inputs: $("#inputValues").value });
    model.previousRegisters = null;
    renderState(payload.state);
    toast(payload.message);
  } catch (error) {
    toast(error.message, "error");
  }
}

function updateTransport() {
  const final = !model.state || ["HALTED", "FAULTED"].includes(model.state.state);
  $("#stepButton").disabled = model.running || final || model.state?.state === "WAITING";
  $("#runButton").disabled = final || model.state?.state === "WAITING";
  $("#runButton").innerHTML = model.running ? "<span>■</span> Detener" : "<span>▶</span> Ejecutar";
  $("#stopButton").disabled = !model.running;
  $("#loadProgram").disabled = model.running;
}

function renderState(state) {
  if (!state) return;
  const priorRegisters = model.state?.registers || model.previousRegisters;
  model.state = state;

  const pill = $("#statePill");
  pill.dataset.state = state.state;
  pill.innerHTML = `<i></i> ${state.state}`;
  $("#pcValue").textContent = hex(state.pc);
  $("#instructionValue").textContent = compactNumber(state.resources.instructions);
  $("#cycleValue").textContent = compactNumber(state.resources.cycles);

  $$("#lifecycleRail li").forEach((item) => item.classList.toggle("active", item.dataset.state === state.state));
  renderRegisters(state.registers, priorRegisters);
  renderFlags(state.flags);
  renderReason(state);
  renderOutput(state);
  renderResources(state);
  renderStack(state.stack);
  renderMemory(state.memory, state.program);
  renderTrace(state.trace, state.pc);
  renderProgram(state.program);
  renderInterrupts(state.resources);
  renderGutter();
  updateTransport();

  const instruction = state.last_instruction;
  $("#lastInstruction").textContent = instruction
    ? `${hex(instruction.pc)} · ${instruction.opcode} · ${instruction.detail}`
    : "Sin instrucción";
  const transition = state.last_transition;
  $("#lastTransition").textContent = transition
    ? `${transition.source} → ${transition.destination}`
    : "Sin transición";
  model.previousRegisters = [...state.registers];
}

function renderRegisters(registers, previous) {
  const grid = $("#registerGrid");
  grid.replaceChildren();
  registers.forEach((value, index) => {
    const card = document.createElement("article");
    card.className = "register";
    if (previous && previous[index] !== value) card.classList.add("changed");
    const header = document.createElement("header");
    const name = document.createElement("b");
    name.textContent = `R${index}`;
    const role = document.createElement("small");
    role.textContent = registerRoles[index];
    const code = document.createElement("code");
    code.textContent = numberValue(value);
    code.title = `Hex ${hex(value)} · Con signo ${value} · Sin signo ${value >>> 0}`;
    header.append(name, role);
    card.append(header, code);
    grid.append(card);
  });
}

function renderFlags(flags) {
  const grid = $("#flagGrid");
  grid.replaceChildren();
  for (const name of ["Z", "N", "C", "O"]) {
    const flag = document.createElement("span");
    flag.className = `flag ${flags[name] ? "on" : ""}`;
    const label = document.createElement("b");
    label.textContent = name;
    const value = document.createElement("span");
    value.textContent = flags[name] ? "1" : "0";
    flag.append(label, value);
    grid.append(flag);
  }
}

function renderReason(state) {
  const bar = $("#reasonBar");
  if (!state.reason && state.exit_code === null) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.classList.toggle("error", state.state === "FAULTED");
  bar.textContent = state.reason || `Programa finalizado · exit code ${state.exit_code}`;
}

function renderOutput(state) {
  const terminal = $("#outputTerminal");
  terminal.textContent = state.output_text || "Sin salida";
  terminal.classList.toggle("muted", !state.output_text);
  terminal.scrollTop = terminal.scrollHeight;
  $("#outputUnits").textContent = `${integerFormatter.format(state.resources.output_units)} unidades`;
}

function setProgress(id, used, total, labelId, label) {
  const percent = total > 0 ? Math.min(100, Math.max(0, (used / total) * 100)) : 0;
  $(id).value = percent;
  $(labelId).textContent = label;
}

function renderResources(state) {
  const r = state.resources;
  const gasUsed = r.gas_limit - r.gas_remaining;
  const stackUsed = state.stack.length;
  const memoryUsed = r.memory_allocated_words;
  setProgress("#gasProgress", gasUsed, r.gas_limit, "#gasLabel", `${compactNumber(r.gas_remaining)} / ${compactNumber(r.gas_limit)}`);
  setProgress("#stackProgress", stackUsed, r.stack_limit, "#stackLabel", `${stackUsed} / ${compactNumber(r.stack_limit)}`);
  setProgress("#memoryProgress", memoryUsed, r.memory_words, "#memoryLabel", `${compactNumber(memoryUsed)} físicas / ${compactNumber(r.memory_words)} lógicas`);
  setProgress("#outputProgress", r.output_units, r.output_limit, "#outputLabel", `${compactNumber(r.output_units)} / ${compactNumber(r.output_limit)}`);
  $("#heapValue").textContent = hex(r.heap_pointer);
  $("#inputDepth").textContent = r.input_depth;
  $("#irqDepth").textContent = r.interrupt_depth;
  $("#traceDepth").textContent = state.trace.length;
  $("#protectionBadge").textContent = r.protect_code ? "Código protegido" : "Código editable";
}

function renderStack(stack) {
  $("#stackDepth").textContent = `${stack.length} valores`;
  const view = $("#stackView");
  view.replaceChildren();
  if (!stack.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Pila vacía";
    view.append(empty);
    return;
  }
  stack.forEach((value, index) => {
    const row = document.createElement("div");
    row.className = "stack-value";
    const offset = document.createElement("span");
    offset.className = "index";
    offset.textContent = index === 0 ? "TOP" : `-${index}`;
    const valueHex = document.createElement("code");
    valueHex.textContent = hex(value);
    const valueDec = document.createElement("code");
    valueDec.textContent = String(value);
    row.append(offset, valueHex, valueDec);
    view.append(row);
  });
}

function renderMemory(memory, program) {
  model.memoryStart = memory.start;
  model.memoryCount = memory.count;
  const body = $("#memoryBody");
  body.replaceChildren();
  for (let offset = 0; offset < memory.values.length; offset += 4) {
    const row = document.createElement("tr");
    const addressCell = document.createElement("td");
    addressCell.textContent = hex(memory.start + offset);
    row.append(addressCell);
    for (let column = 0; column < 4; column += 1) {
      const address = memory.start + offset + column;
      const cell = document.createElement("td");
      if (offset + column < memory.values.length) {
        cell.textContent = numberValue(memory.values[offset + column]);
        cell.title = `Memoria[${hex(address)}] = ${memory.values[offset + column]}`;
        if (program && address < program.code_size) cell.className = "code-word";
        else if (program && address < program.words) cell.className = "data-word";
      }
      row.append(cell);
    }
    body.append(row);
  }
}

function renderTrace(trace, pc) {
  $("#traceCount").textContent = `${trace.length} eventos`;
  const body = $("#traceBody");
  body.replaceChildren();
  [...trace].reverse().forEach((entry, index) => {
    const row = document.createElement("tr");
    if (index === 0 && entry.pc === pc) row.classList.add("current");
    if (entry.detail.startsWith("FAULT")) row.classList.add("fault");
    const values = [
      entry.sequence,
      hex(entry.pc),
      entry.opcode,
      entry.operands.map((value) => String(value)).join(", "),
      entry.detail,
      entry.cycles,
    ];
    for (const value of values) {
      const cell = document.createElement("td");
      cell.textContent = value;
      cell.title = String(value);
      row.append(cell);
    }
    body.append(row);
  });
}

function renderProgram(program) {
  const list = $("#symbolList");
  list.replaceChildren();
  if (!program) {
    $("#programSize").textContent = "Sin programa";
    return;
  }
  $("#programSize").textContent = `${program.code_size} código · ${program.data_size} datos`;
  Object.entries(program.symbols)
    .sort(([, left], [, right]) => left - right)
    .forEach(([name, address]) => {
      const item = document.createElement("div");
      item.className = "symbol";
      const label = document.createElement("b");
      label.textContent = name;
      const code = document.createElement("code");
      code.textContent = hex(address);
      item.append(label, code);
      list.append(item);
    });
  if (!list.children.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "Sin símbolos";
    list.append(empty);
  }
}

function renderInterrupts(resources) {
  const vectors = Object.entries(resources.interrupt_vectors)
    .map(([vector, target]) => `${vector}→${hex(target)}`)
    .join(" · ");
  const pending = resources.pending_interrupts.length
    ? `Pendientes: ${resources.pending_interrupts.join(", ")}`
    : "Sin IRQ pendientes";
  $("#interruptStatus").textContent = `${resources.interrupts_enabled ? "IRQ habilitadas" : "IRQ enmascaradas"} · ${pending}${vectors ? ` · Vectores: ${vectors}` : ""}`;
}

function updateEditorStats() {
  const source = $("#sourceEditor").value;
  const lines = source.split("\n");
  $("#lineCount").textContent = `${lines.length} líneas`;
  $("#wordCount").textContent = `${source.trim() ? source.trim().split(/\s+/).length : 0} palabras`;
  renderGutter();
}

function renderGutter() {
  const lines = $("#sourceEditor").value.split("\n");
  const addresses = new Map();
  if (!model.dirty) {
    for (const item of model.listing) addresses.set(item.line, item.address);
  }
  const activeLine = !model.dirty && model.state
    ? model.listing.find((item) => item.address === model.state.pc)?.line
    : null;
  const gutter = $("#editorGutter");
  gutter.replaceChildren();
  lines.forEach((_, index) => {
    const lineNumber = index + 1;
    const line = document.createElement("div");
    line.className = `gutter-line ${lineNumber === activeLine ? "active" : ""}`;
    const address = document.createElement("span");
    address.className = "address";
    address.textContent = addresses.has(lineNumber)
      ? (addresses.get(lineNumber) >>> 0).toString(16).toUpperCase().padStart(8, "0")
      : "";
    const number = document.createElement("span");
    number.textContent = String(lineNumber).padStart(3, " ");
    line.append(address, document.createTextNode(" "), number);
    gutter.append(line);
  });
  gutter.scrollTop = $("#sourceEditor").scrollTop;
}

async function refreshMemory() {
  try {
    model.memoryStart = parseInteger($("#memoryStart").value);
    model.memoryCount = Number($("#memoryCount").value);
    const payload = await api(`/api/state?memory_start=${model.memoryStart}&memory_count=${model.memoryCount}`);
    renderState(payload.state);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function submitInput(event) {
  event.preventDefault();
  try {
    const payload = await action({ action: "input", values: $("#inputValues").value });
    renderState(payload.state);
    toast(payload.message);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function setVector(event) {
  event.preventDefault();
  try {
    const payload = await action({
      action: "set_vector",
      vector: $("#vectorNumber").value,
      target: $("#vectorTarget").value,
    });
    renderState(payload.state);
    toast(payload.message);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function requestInterrupt(event) {
  event.preventDefault();
  try {
    const payload = await action({ action: "interrupt", vector: $("#interruptNumber").value });
    renderState(payload.state);
    toast(payload.message);
  } catch (error) {
    toast(error.message, "error");
  }
}

function download(path, filename) {
  const anchor = document.createElement("a");
  anchor.href = path;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

async function restoreSnapshot(file) {
  if (!file) return;
  stopRun(false);
  try {
    const response = await fetch("/api/restore", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: await file.arrayBuffer(),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    model.listing = [];
    model.dirty = true;
    renderState(payload.state);
    setCompileStatus("Snapshot restaurado · direcciones fuente no disponibles", "success");
    toast(payload.message);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("#snapshotFile").value = "";
  }
}

function activateTab(name) {
  $$(".inspect-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
  $$(".inspect-tab").forEach((tab) => tab.classList.toggle("active", tab.id === `${name}Tab`));
}

function bindEvents() {
  $("#loadProgram").addEventListener("click", () => compileLoad());
  $("#stepButton").addEventListener("click", singleStep);
  $("#runButton").addEventListener("click", startRun);
  $("#stopButton").addEventListener("click", stopRun);
  $("#resetButton").addEventListener("click", resetVM);
  $("#inputForm").addEventListener("submit", submitInput);
  $("#memoryForm").addEventListener("submit", (event) => { event.preventDefault(); refreshMemory(); });
  $("#vectorForm").addEventListener("submit", setVector);
  $("#interruptForm").addEventListener("submit", requestInterrupt);

  $("#sourceEditor").addEventListener("input", () => {
    model.dirty = true;
    setCompileStatus("Cambios sin ensamblar", "pending");
    updateEditorStats();
  });
  $("#sourceEditor").addEventListener("scroll", (event) => {
    $("#editorGutter").scrollTop = event.currentTarget.scrollTop;
  });
  $("#sourceEditor").addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const editor = event.currentTarget;
      const start = editor.selectionStart;
      editor.setRangeText("    ", start, editor.selectionEnd, "end");
      editor.dispatchEvent(new Event("input"));
    }
  });

  $("#demoSelect").addEventListener("change", async (event) => {
    const demo = model.demos.find((item) => item.id === event.target.value);
    if (!demo) return;
    model.demo = demo.id;
    $("#sourceEditor").value = demo.source;
    model.dirty = true;
    updateEditorStats();
    await compileLoad({ quiet: true });
  });

  $$("[data-number-mode]").forEach((button) => button.addEventListener("click", () => {
    model.numberMode = button.dataset.numberMode;
    $$("[data-number-mode]").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
    if (model.state) renderState(model.state);
  }));

  $$(".inspect-tabs button").forEach((button) => button.addEventListener("click", () => activateTab(button.dataset.tab)));

  $("#downloadBytecode").addEventListener("click", () => download("/api/bytecode", "programa.tvm"));
  $("#saveSnapshot").addEventListener("click", () => download("/api/snapshot", "tramoya-vm32.tvms"));
  $("#openSnapshot").addEventListener("click", () => $("#snapshotFile").click());
  $("#snapshotFile").addEventListener("change", (event) => restoreSnapshot(event.target.files[0]));

  $("#openSettings").addEventListener("click", () => $("#settingsDialog").showModal());
  $("#openHelp").addEventListener("click", () => $("#helpDialog").showModal());
  $("#applySettings").addEventListener("click", async (event) => {
    event.preventDefault();
    if (await compileLoad()) $("#settingsDialog").close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.key === "Enter") {
      event.preventDefault();
      compileLoad();
    } else if (event.key === "F5") {
      event.preventDefault();
      startRun();
    } else if (event.key === "F10") {
      event.preventDefault();
      singleStep();
    } else if (event.key === "Escape" && model.running) {
      stopRun();
    }
  });
}

bindEvents();
bootstrap();
