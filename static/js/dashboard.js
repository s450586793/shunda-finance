function chartNumber(value) {
  if (typeof value !== "string" || !/^-?\d+(?:\.\d{2})$/.test(value)) {
    throw new TypeError("图表金额格式不合法");
  }
  const number = Number(value);
  if (!Number.isFinite(number)) throw new TypeError("图表金额格式不合法");
  return number;
}


const DEFAULT_PALETTE = {
  text: "#252825",
  muted: "#68706a",
  border: "#d9ddd8",
  inflow: "#23764a",
  outflow: "#b33131",
  accent: "#24639a",
};


function emptyGraphic(empty, color) {
  return empty ? {
    type: "text",
    left: "center",
    top: "middle",
    style: { text: "暂无数据", fill: color, fontSize: 14 },
  } : undefined;
}


export function buildDashboardOptions(data, colors = DEFAULT_PALETTE) {
  const palette = { ...DEFAULT_PALETTE, ...colors };
  const cashflowEmpty = data.dailyCashflow.length === 0
    || data.dailyCashflow.every(
      (item) => chartNumber(item.inflow) === 0 && chartNumber(item.outflow) === 0,
    );
  const receivableEmpty = data.receivableAging.length === 0
    || data.receivableAging.every((item) => chartNumber(item.amount) === 0);
  const payableEmpty = data.payableDue.length === 0
    || data.payableDue.every((item) => chartNumber(item.amount) === 0);
  return {
    cashflow: {
      animationDuration: 300,
      color: [palette.inflow, palette.outflow],
      textStyle: { color: palette.text },
      tooltip: { trigger: "axis", valueFormatter: (value) => Number(value).toFixed(2) },
      legend: { data: ["收款", "付款"], bottom: 0, textStyle: { color: palette.text } },
      grid: { left: 58, right: 20, top: 24, bottom: 48 },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: data.dailyCashflow.map((item) => item.date),
        axisLabel: { color: palette.muted },
        axisLine: { lineStyle: { color: palette.border } },
      },
      yAxis: {
        type: "value",
        min: 0,
        axisLabel: { color: palette.muted },
        splitLine: { lineStyle: { color: palette.border } },
      },
      series: [
        { name: "收款", type: "line", showSymbol: false, smooth: false, lineStyle: { color: palette.inflow }, data: data.dailyCashflow.map((item) => chartNumber(item.inflow)) },
        { name: "付款", type: "line", showSymbol: false, smooth: false, lineStyle: { color: palette.outflow }, data: data.dailyCashflow.map((item) => chartNumber(item.outflow)) },
      ],
      graphic: emptyGraphic(cashflowEmpty, palette.muted),
    },
    receivable: {
      animationDuration: 300,
      color: [palette.inflow, palette.accent, palette.outflow, palette.muted],
      textStyle: { color: palette.text },
      tooltip: { trigger: "item", valueFormatter: (value) => Number(value).toFixed(2) },
      legend: { type: "scroll", bottom: 0, textStyle: { color: palette.text } },
      series: [{
        name: "应收账龄",
        type: "pie",
        radius: ["46%", "70%"],
        center: ["50%", "44%"],
        label: { show: false },
        data: data.receivableAging.map((item) => ({ name: item.label, value: chartNumber(item.amount) })),
      }],
      graphic: emptyGraphic(receivableEmpty, palette.muted),
    },
    payable: {
      animationDuration: 300,
      textStyle: { color: palette.text },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: (value) => Number(value).toFixed(2) },
      grid: { left: 72, right: 20, top: 20, bottom: 36 },
      xAxis: {
        type: "category",
        data: data.payableDue.map((item) => item.label),
        axisLabel: { interval: 0, color: palette.muted },
        axisLine: { lineStyle: { color: palette.border } },
      },
      yAxis: {
        type: "value",
        min: 0,
        axisLabel: { color: palette.muted },
        splitLine: { lineStyle: { color: palette.border } },
      },
      series: [{ name: "应付金额", type: "bar", barMaxWidth: 42, itemStyle: { color: palette.accent }, data: data.payableDue.map((item) => chartNumber(item.amount)) }],
      graphic: emptyGraphic(payableEmpty, palette.muted),
    },
  };
}


function initDashboard() {
  const dataElement = document.getElementById("dashboard-data");
  if (!dataElement || !window.echarts) return;
  let data;
  try {
    data = JSON.parse(dataElement.textContent);
  } catch (_error) {
    return;
  }
  const styles = getComputedStyle(document.documentElement);
  const token = (name) => styles.getPropertyValue(name).trim();
  const options = buildDashboardOptions(data, {
    text: token("--text"),
    muted: token("--text-muted"),
    border: token("--border"),
    inflow: token("--success"),
    outflow: token("--error"),
    accent: token("--info"),
  });
  const definitions = [
    ["cashflow-chart", options.cashflow],
    ["receivable-aging-chart", options.receivable],
    ["payable-due-chart", options.payable],
  ];
  const charts = [];
  for (const [id, option] of definitions) {
    const element = document.getElementById(id);
    if (!element) continue;
    const chart = window.echarts.init(element);
    chart.setOption(option);
    charts.push(chart);
  }
  let resizeFrame;
  const resize = () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => charts.forEach((chart) => chart.resize()));
  };
  window.addEventListener("resize", resize, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) resize();
  });
}


if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", initDashboard);
}
