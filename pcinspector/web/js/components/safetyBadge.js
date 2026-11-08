import { el } from "../ui.js";

export const safetyLabels = { safe: "Безопасно убрать", review: "Нужна проверка", keep: "Сохранить", info: "Сведения" };

export function safetyBadge(value) {
  const known = Object.hasOwn(safetyLabels, value) ? value : "info";
  return el("span", `badge ${known}`, safetyLabels[known]);
}
