import { api } from "./api.js";
import { button, el, number } from "./ui.js";

const dialog = document.querySelector("#scan-dialog");
const form = document.querySelector("#scan-form");
const paths = document.querySelector("#scan-paths");
const submit = document.querySelector("#scan-submit");
const errorBox = document.querySelector("#scan-error");
const statusBox = document.querySelector("#scan-status");
const activeStates = ["queued", "running"];
const states = {
  queued: "В очереди", running: "Сканирование…", completed: "Сканирование завершено",
  cancelled: "Сканирование отменено", failed: "Сканирование завершилось с ошибками",
};
let job = sessionStorage.getItem("pci-job");

export function scanDialog(roots = []) {
  if (job) {
    statusBox.scrollIntoView({ block: "start" });
    return;
  }
  errorBox.hidden = true;
  paths.value = roots.join("\n");
  dialog.showModal();
}

document.querySelector("#scan-close").addEventListener("click", () => dialog.close());
form.addEventListener("submit", async event => {
  event.preventDefault();
  submit.disabled = true;
  errorBox.hidden = true;
  try {
    const result = await api("scan", {}, {
      method: "POST", body: { paths: paths.value.split(/\r?\n/).map(path => path.trim()).filter(Boolean) },
    });
    job = result.job_id;
    sessionStorage.setItem("pci-job", job);
    dialog.close();
    showStatus({ state: "queued", results: [], errors: [] });
    setTimeout(poll, 200);
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  } finally {
    submit.disabled = false;
  }
});

function showStatus(status) {
  statusBox.hidden = false;
  const active = activeStates.includes(status.state);
  const bar = active ? el("progress", "scan-bar") : null;
  if (bar) bar.setAttribute("aria-label", "Сканирование выполняется");
  const cancel = button("Отменить", "secondary", async () => {
    cancel.disabled = true;
    try {
      await api(`jobs/${job}/cancel`, {}, { method: "POST" });
    } catch (error) {
      statusBox.append(el("p", "error", error.message));
      cancel.disabled = false;
    }
  });
  const close = button("Скрыть", "secondary", () => { statusBox.hidden = true; });
  const progress = status.progress;
  const count = progress && `${number(progress.files)} файлов · ${number(progress.dirs)} папок · ${number(progress.errors)} ошибок · ${number(Math.round(progress.files_per_second))} файлов/с`;
  statusBox.replaceChildren(...[
    el("div", "section-head", el("strong", "", states[status.state]), active ? cancel : close),
    bar,
    count ? el("p", "muted", count) : null,
    active && progress ? el("p", "path", progress.path) : null,
    ...status.results.map(result => el("p", "muted", `${result.root} · ${number(result.files)} файлов · ${number(result.errors)} ошибок`)),
    ...status.errors.map(error => el("p", "error", error)),
  ].filter(Boolean));
}

async function poll() {
  if (!job) return;
  try {
    const status = await api(`jobs/${job}`);
    showStatus(status);
    if (activeStates.includes(status.state)) {
      setTimeout(poll, 1000);
      return;
    }
  } catch (error) {
    statusBox.hidden = false;
    statusBox.replaceChildren(el("p", "error", error.message));
  }
  job = null;
  sessionStorage.removeItem("pci-job");
  window.dispatchEvent(new Event("pci-scan-finished"));
}

if (job) poll();
