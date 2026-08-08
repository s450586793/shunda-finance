import test from "node:test";
import assert from "node:assert/strict";

import { buildDashboardOptions } from "./dashboard.js";


const payload = {
  dailyCashflow: [
    { date: "2026-07-01", inflow: "12.50", outflow: "3.20" },
    { date: "2026-07-02", inflow: "0.00", outflow: "8.00" },
  ],
  receivableAging: [
    { label: "0-30", amount: "10.00" },
    { label: "31-60", amount: "20.00" },
  ],
  payableDue: [
    { label: "已逾期", amount: "7.00" },
    { label: "7日内", amount: "5.00" },
  ],
};


test("buildDashboardOptions converts decimal strings only for chart rendering", () => {
  const options = buildDashboardOptions(payload);

  assert.deepEqual(options.cashflow.xAxis.data, ["2026-07-01", "2026-07-02"]);
  assert.deepEqual(options.cashflow.series[0].data, [12.5, 0]);
  assert.deepEqual(options.cashflow.series[1].data, [3.2, 8]);
  assert.deepEqual(options.receivable.series[0].data, [
    { name: "0-30", value: 10 },
    { name: "31-60", value: 20 },
  ]);
  assert.deepEqual(options.payable.series[0].data, [7, 5]);
});


test("buildDashboardOptions returns renderable empty options", () => {
  const options = buildDashboardOptions({
    dailyCashflow: [],
    receivableAging: [],
    payableDue: [],
  });

  assert.deepEqual(options.cashflow.xAxis.data, []);
  assert.deepEqual(options.cashflow.series[0].data, []);
  assert.deepEqual(options.receivable.series[0].data, []);
  assert.deepEqual(options.payable.series[0].data, []);
});


test("buildDashboardOptions shows empty state for a zero-filled server month", () => {
  const dailyCashflow = Array.from({ length: 28 }, (_, index) => ({
    date: `2026-02-${String(index + 1).padStart(2, "0")}`,
    inflow: "0.00",
    outflow: "0.00",
  }));

  const options = buildDashboardOptions({
    dailyCashflow,
    receivableAging: [],
    payableDue: [],
  });

  assert.equal(options.cashflow.graphic.type, "text");
  assert.equal(options.cashflow.graphic.style.text, "暂无数据");
  assert.equal(options.cashflow.series[0].data.length, 28);
});


test("buildDashboardOptions keeps the cashflow chart when any day is nonzero", () => {
  const options = buildDashboardOptions({
    dailyCashflow: [
      { date: "2026-02-01", inflow: "0.00", outflow: "0.00" },
      { date: "2026-02-02", inflow: "1.00", outflow: "0.00" },
    ],
    receivableAging: [],
    payableDue: [],
  });

  assert.equal(options.cashflow.graphic, undefined);
  assert.deepEqual(options.cashflow.series[0].data, [0, 1]);
});


test("buildDashboardOptions rejects malformed financial display values", () => {
  assert.throws(
    () => buildDashboardOptions({
      dailyCashflow: [{ date: "2026-07-01", inflow: "NaN", outflow: "1.00" }],
      receivableAging: [],
      payableDue: [],
    }),
    /图表金额格式不合法/,
  );
});


test("buildDashboardOptions applies local theme tokens to chart text and axes", () => {
  const options = buildDashboardOptions(payload, {
    text: "#e6e9e5",
    muted: "#a9b0aa",
    border: "#3a403c",
    inflow: "#72bf91",
    outflow: "#ef8585",
    accent: "#7eb3df",
  });

  assert.equal(options.cashflow.textStyle.color, "#e6e9e5");
  assert.equal(options.cashflow.xAxis.axisLabel.color, "#a9b0aa");
  assert.equal(options.cashflow.yAxis.splitLine.lineStyle.color, "#3a403c");
  assert.equal(options.cashflow.series[0].lineStyle.color, "#72bf91");
  assert.equal(options.cashflow.series[1].lineStyle.color, "#ef8585");
  assert.equal(options.payable.series[0].itemStyle.color, "#7eb3df");
});
