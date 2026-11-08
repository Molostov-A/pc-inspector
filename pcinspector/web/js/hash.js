import { api } from "./api.js";
import { hashActivity } from "./activity.js";
import { button, el, number } from "./ui.js";

const statusBox = document.querySelector("#hash-status");
const storageKey = "pci-hash-job";
const states = {
  queued: "Поиск дубликатов в очереди", running: "Ищем дубликаты…",
  completed: "Поиск дубликатов завершён", cancelled: "Поиск дубликатов отменён", failed: "Ошибка поиска дубликатов",
};
const steps = { candidates: "Отбираем файлы одинакового размера", partial: "Считаем частичные хэши", full: "Сверяем полные хэши" };
let job = sessionStorage.getItem(storageKey);
let submitting = false;

export async function startHash(root, minSize) {
  if (job || submitting) throw new Error("Поиск уже выполняется");
  submitting = true;
  try {
    const result = await api("hash", {}, {
      method: "POST", body: { root, min_size: minSize }, activity: "Запускаем поиск дубликатов…",
    });
    job = result.job_id;
    sessionStorage.setItem(storageKey, job);
    showStatus({ state: "queued", errors: [] });
    setTimeout(poll, 150);
  } finally {
    submitting = false;
  }
}

function showStatus(status) {
  statusBox.hidden = false;
  const active = status.state === "queued" || status.state === "running";
  const info = status.hash_progress;
  const bar = active ? el("progress", "scan-bar") : null;
  if (bar) {
    bar.setAttribute("aria-label", "Ход поиска дубликатов");
    if (info?.total > 0) {
      bar.max = info.total;
      bar.value = info.processed;
    }
  }
  hashActivity(status.state === "running" && info
    ? `${steps[info.phase]} · ${number(info.processed)}/${number(info.total)} · из кэша ${number(info.cached)}` : "");
  const action = active ? button("Отменить поиск", "secondary", async () => {
    action.disabled = true;
    try {
      await api(`jobs/${job}/cancel`, {}, { method: "POST", activity: "Запрашиваем отмену поиска…" });
    } catch (error) {
      statusBox.append(el("p", "error", error.message));
      action.disabled = false;
    }
  }) : button("Скрыть", "secondary", () => { statusBox.hidden = true; });
  const result = status.hash_result;
  const summary = result ? el("p", "muted",
    `Кандидатов ${number(result.candidates)} · чтений ${number(result.partial_read + result.full_read)} · из кэша ${number(result.cached)} · жёстких ссылок пропущено ${number(result.hardlinks)} · ошибок ${number(result.errors)}`,
  ) : null;
  statusBox.replaceChildren(...[
    el("div", "section-head", el("strong", "", states[status.state]), action),
    bar, summary, ...status.errors.map(error => el("p", "error", error)),
  ].filter(Boolean));
  return active;
}

async function poll() {
  if (!job) return;
  try {
    if (showStatus(await api(`jobs/${job}`))) {
      setTimeout(poll, 700);
      return;
    }
  } catch (error) {
    hashActivity("");
    statusBox.hidden = false;
    statusBox.replaceChildren(el("p", "error", error.message));
  }
  job = null;
  sessionStorage.removeItem(storageKey);
  window.dispatchEvent(new Event("pci-hash-finished"));
}

if (job) poll();
