import { api } from "../api.js";
import { el, heading, message } from "../ui.js";

const days = {
  MON: "Понедельник", TUE: "Вторник", WED: "Среда", THU: "Четверг",
  FRI: "Пятница", SAT: "Суббота", SUN: "Воскресенье",
};

export async function render(container, _params, signal) {
  const settings = await api("settings", {}, { signal });
  const roots = el("textarea");
  const exclusions = el("textarea");
  roots.rows = exclusions.rows = 4;
  const projects = el("input");
  const stale = el("input");
  stale.type = "number";
  stale.min = "1";
  stale.max = "3650";
  stale.required = true;
  const schedule = el("input");
  schedule.type = "checkbox";
  const day = el("select");
  for (const [id, label] of Object.entries(days)) {
    const option = el("option", "", label);
    option.value = id;
    day.append(option);
  }
  const time = el("input");
  time.type = "time";
  time.required = true;
  const quarantine = el("input");
  const field = (label, input) => el("label", "field", label, input);
  function fill(value) {
    roots.value = value.scan_roots.join("\n");
    exclusions.value = value.exclude_paths.join("\n");
    projects.value = value.projects_root;
    stale.value = value.stale_days;
    schedule.checked = value.schedule_enabled;
    day.value = value.schedule_day;
    time.value = value.schedule_time;
    quarantine.value = value.quarantine_path;
  }
  fill(settings);
  const save = el("button", "primary", "Сохранить настройки");
  save.type = "submit";
  const feedback = el("div");
  feedback.setAttribute("role", "status");
  const form = el("form", "panel analyzer-form",
    el("h2", "", "Сканирование и отчёты"),
    el("div", "form-grid",
      field("Корни сканирования, по одному на строку", roots),
      field("Исключения, по одному на строку", exclusions),
      field("Основная папка проектов", projects), field("Порог давности, дней", stale)),
    message("Исключение может быть абсолютным или относительным к корню скана.", "muted"),
    el("h2", "", "Расписание"),
    el("label", "row-actions", schedule, "Еженедельное сканирование"),
    el("div", "form-grid", field("День недели", day), field("Время", time)),
    message("Здесь сохраняются параметры. Запуск по расписанию появится на этапе 8.", "note"),
    el("h2", "", "Карантин"),
    field("Папка карантина", quarantine),
    message("Пустое поле использует карантин на диске исходного файла. Своя папка должна быть на том же диске.", "muted"),
    el("div", "actions", save), feedback,
  );
  const lines = input => input.value.split("\n").map(line => line.trim()).filter(Boolean);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    save.disabled = true;
    feedback.replaceChildren();
    try {
      const value = await api("settings", {}, {
        method: "PUT", signal, activity: "Проверяем и сохраняем настройки…",
        body: {
          scan_roots: lines(roots), exclude_paths: lines(exclusions), projects_root: projects.value.trim(),
          stale_days: stale.valueAsNumber, schedule_enabled: schedule.checked, schedule_day: day.value,
          schedule_time: time.value, quarantine_path: quarantine.value.trim(),
        },
      });
      fill(value);
      feedback.replaceChildren(message("Настройки сохранены.", "note"));
    } catch (error) {
      if (!signal.aborted) feedback.replaceChildren(message(error.message, "error"));
    } finally {
      save.disabled = false;
    }
  });
  container.replaceChildren(heading("Локальные параметры", "Настройки", "Пути и параметры для следующих сканов и отчётов."), form);
}
