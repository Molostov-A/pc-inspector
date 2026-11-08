import { api } from "../api.js";
import { button, el, link, message, number } from "../ui.js";

export function modal(title, content, label, submit) {
  const error = message("", "error");
  error.setAttribute("role", "alert");
  const form = el("form", "", el("h2", "", title), ...content, error);
  const dialog = el("dialog", "", form);
  dialog.setAttribute("aria-label", title);
  const cancel = button("Закрыть", "secondary", () => dialog.close());
  const confirm = el("button", "primary", label);
  confirm.type = "submit";
  form.append(el("div", "actions", cancel, confirm));
  dialog.addEventListener("close", () => dialog.remove());
  dialog.addEventListener("cancel", event => { if (confirm.disabled) event.preventDefault(); });
  form.addEventListener("submit", async event => {
    event.preventDefault();
    error.textContent = "";
    confirm.disabled = cancel.disabled = true;
    confirm.textContent = "Выполняем…";
    try {
      await submit();
      dialog.close();
    } catch (problem) {
      error.textContent = problem.message;
    } finally {
      confirm.disabled = cancel.disabled = false;
      confirm.textContent = label;
    }
  });
  document.body.append(dialog);
  dialog.showModal();
}

export async function addToPlan(items, feedback) {
  try {
    const plans = await api("plans", {}, { activity: "Читаем список планов…" });
    const select = el("select");
    const fresh = el("option", "", "Создать новый план");
    fresh.value = "";
    select.append(fresh);
    for (const plan of plans.filter(plan => plan.status === "draft")) {
      const option = el("option", "", plan.title);
      option.value = plan.id;
      select.append(option);
    }
    const title = el("input");
    title.maxLength = 160;
    title.value = "План уборки";
    title.required = true;
    const field = el("label", "field", "Название нового плана", title);
    select.addEventListener("change", () => {
      field.hidden = Boolean(select.value);
      title.required = !select.value;
    });
    modal("Добавить в план", [
      message(`Выбрано путей · ${number(items.length)}`, "muted"),
      el("label", "field", "План", select), field,
    ], "Добавить", async () => {
      if (!select.value) {
        const plan = await api("plans", {}, { method: "POST", body: { title: title.value } });
        const option = el("option", "", plan.title);
        option.value = plan.id;
        select.append(option);
        select.value = plan.id;
        select.dispatchEvent(new Event("change"));
      }
      const id = select.value;
      const result = await api(`plans/${id}/items`, {}, {
        method: "POST", body: { items }, activity: "Проверяем пути и добавляем пункты…",
      });
      feedback.replaceChildren(
        message(`Добавлено · ${number(result.accepted.length)}`, "note"),
        ...result.rejected.map(item => message(`${item.path} · ${item.reason}`, "error")),
        link("Открыть план →", `/plans/${id}`, {}, "text-link"),
      );
      feedback.hidden = false;
    });
  } catch (error) {
    feedback.replaceChildren(message(error.message, "error"));
    feedback.hidden = false;
  }
}
