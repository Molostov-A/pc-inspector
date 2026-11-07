import { href, size } from "../ui.js";

const colors = ["#246a64", "#325a77", "#5b637d", "#776446", "#3b7760", "#3c7770"];

function chartData(nodes, parent = null, color = null) {
  return nodes.map((node, index) => {
    const navigationPath = node.path || parent;
    const fill = color || colors[index % colors.length];
    return {
      ...node,
      navigationPath,
      itemStyle: { color: fill },
      children: chartData(node.children, navigationPath, fill),
    };
  });
}

export function treemap(container, nodes) {
  const chart = globalThis.echarts.init(container);
  const resize = new ResizeObserver(() => chart.resize());
  resize.observe(container);
  chart.setOption({
    animation: false,
    tooltip: {
      renderMode: "richText",
      formatter: ({ data }) => size(data.value),
    },
    series: [{
      type: "treemap", data: chartData(nodes), roam: false, nodeClick: false, breadcrumb: { show: false },
      visibleMin: 1, left: 0, top: 0, right: 0, bottom: 0,
      label: { show: true, color: "#fff", overflow: "truncate" },
      upperLabel: { show: true, color: "#fff", height: 23 },
      levels: [
        { itemStyle: { borderColor: "#203f40", borderWidth: 1, gapWidth: 3 } },
        { itemStyle: { borderColor: "#29494a", borderWidth: 1, gapWidth: 2 } },
        { itemStyle: { borderColor: "#29494a", gapWidth: 1 }, upperLabel: { show: false } },
      ],
    }],
  });
  chart.on("click", ({ data }) => {
    if (data?.navigationPath) location.hash = href("/explorer", { path: data.navigationPath });
  });
  return () => {
    resize.disconnect();
    chart.dispose();
  };
}
