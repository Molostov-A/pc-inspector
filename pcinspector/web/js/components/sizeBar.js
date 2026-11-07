import { el } from "../ui.js";

const percent = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1 });

export function sizeBar(value, total, label = "Доля от общего объёма") {
  const share = total > 0 ? Math.min(1, Math.max(0, value / total)) : 0;
  const meter = el("progress", "size-bar");
  meter.max = 1;
  meter.value = share;
  meter.setAttribute("aria-label", label);
  meter.title = `${label} · ${percent.format(share)}`;
  return meter;
}
