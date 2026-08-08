import test from "node:test";
import assert from "node:assert/strict";

import {
  allocationSummary,
  calculateAllocation,
  confirmationState,
} from "./reconciliation-workbench.js";


test("calculateAllocation uses integer cents for a partial allocation", () => {
  assert.deepEqual(
    calculateAllocation(4_635_500, [
      { id: "a", availableCents: 2_000_000 },
      { id: "b", availableCents: 2_518_700 },
    ]),
    { selected: 4_518_700, allocated: 4_518_700, difference: 116_800 },
  );
});


test("calculateAllocation caps selected funds at the invoice amount", () => {
  assert.deepEqual(
    calculateAllocation(10_000, [
      { id: "a", availableCents: 7_000 },
      { id: "b", availableCents: 5_000 },
    ]),
    { selected: 12_000, allocated: 10_000, difference: 0 },
  );
});


test("calculateAllocation accepts no selection", () => {
  assert.deepEqual(calculateAllocation(10_000, []), {
    selected: 0,
    allocated: 0,
    difference: 10_000,
  });
});


test("calculateAllocation rejects duplicates and unsafe or negative cents", () => {
  assert.throws(
    () => calculateAllocation(10_000, [
      { id: "same", availableCents: 1_000 },
      { id: "same", availableCents: 2_000 },
    ]),
    /资金流水不能重复/,
  );
  assert.throws(
    () => calculateAllocation(Number.MAX_SAFE_INTEGER + 1, []),
    /安全整数/,
  );
  assert.throws(
    () => calculateAllocation(10_000, [{ id: "a", availableCents: -1 }]),
    /非负整数/,
  );
  assert.throws(
    () => calculateAllocation(Number.MAX_SAFE_INTEGER, [
      { id: "a", availableCents: Number.MAX_SAFE_INTEGER },
      { id: "b", availableCents: 1 },
    ]),
    /安全整数/,
  );
});


test("confirmationState disables zero, stale and unconfirmed partial selections", () => {
  const partial = [{ id: "a", availableCents: 7_000 }];
  const complete = [{ id: "a", availableCents: 10_000 }];

  assert.equal(confirmationState(10_000, [], false, false).canConfirm, false);
  assert.equal(confirmationState(10_000, complete, false, true).canConfirm, false);
  assert.deepEqual(confirmationState(10_000, partial, false, false), {
    selected: 7_000,
    allocated: 7_000,
    difference: 3_000,
    partial: true,
    canConfirm: false,
  });
  assert.equal(confirmationState(10_000, partial, true, false).canConfirm, true);
  assert.equal(confirmationState(10_000, complete, false, false).canConfirm, true);
});


test("allocationSummary formats every live workbench total from integer cents", () => {
  assert.deepEqual(
    allocationSummary({
      selected: 4_505_000,
      allocated: 4_505_000,
      difference: 100_000,
    }),
    {
      selected: "45,050.00",
      allocated: "45,050.00",
      difference: "1,000.00",
    },
  );
});
