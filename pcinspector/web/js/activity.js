const line = document.querySelector("#activity");
const tasks = new Map();
let scan = "";

function update() {
  const text = [...tasks.values()].at(-1) || scan || "Готово";
  if (line.textContent !== text) line.textContent = text;
  line.title = text;
}

export function beginActivity(text) {
  const task = Symbol();
  tasks.set(task, text);
  update();
  return () => {
    tasks.delete(task);
    update();
  };
}

export function scanActivity(text) {
  scan = text;
  update();
}
