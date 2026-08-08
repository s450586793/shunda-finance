function requireSafeNonNegativeInteger(value, label) {
  if (!Number.isSafeInteger(value)) {
    throw new RangeError(`${label}必须是安全整数`);
  }
  if (value < 0) {
    throw new RangeError(`${label}必须是非负整数`);
  }
}


export function calculateAllocation(invoiceAmount, selectedTransactions) {
  requireSafeNonNegativeInteger(invoiceAmount, "发票金额");
  const identifiers = new Set();
  let selected = 0;
  for (const item of selectedTransactions) {
    if (!item.id || identifiers.has(item.id)) {
      throw new RangeError("资金流水不能重复");
    }
    identifiers.add(item.id);
    requireSafeNonNegativeInteger(item.availableCents, "可核销金额");
    selected += item.availableCents;
    if (!Number.isSafeInteger(selected)) {
      throw new RangeError("资金合计必须是安全整数");
    }
  }
  const allocated = Math.min(invoiceAmount, selected);
  return { selected, allocated, difference: invoiceAmount - allocated };
}


export function confirmationState(
  invoiceAmount,
  selectedTransactions,
  partialConfirmed,
  stale,
) {
  const allocation = calculateAllocation(invoiceAmount, selectedTransactions);
  const partial = allocation.allocated > 0 && allocation.difference > 0;
  return {
    ...allocation,
    partial,
    canConfirm: allocation.allocated > 0
      && !stale
      && (!partial || partialConfirmed),
  };
}


function centsToInput(cents) {
  requireSafeNonNegativeInteger(cents, "核销金额");
  return `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}


function centsToText(cents) {
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(cents / 100);
}


export function allocationSummary(state) {
  return {
    selected: centsToText(state.selected),
    allocated: centsToText(state.allocated),
    difference: centsToText(state.difference),
  };
}


function initWorkbench() {
  const form = document.querySelector("[data-reconciliation-form]");
  if (!form) return;
  const invoiceCents = Number(form.dataset.invoiceCents);
  const rows = [...form.querySelectorAll("[data-transaction-row]")];
  const button = form.querySelector("[data-confirm-button]");
  const partialBlock = form.querySelector("[data-partial-block]");
  const partialCheckbox = form.querySelector("[name=partial_confirm]");
  const selectedOutput = form.querySelector("[data-selected-total]");
  const allocatedOutput = form.querySelector("[data-allocated-total]");
  const differenceOutputs = [...form.querySelectorAll("[data-difference-total]")];

  function update() {
    let selectedItems = [];
    let stale = false;
    for (const row of rows) {
      const checkbox = row.querySelector("[data-transaction-select]");
      if (!checkbox.checked) continue;
      selectedItems.push({
        id: checkbox.dataset.transactionId,
        availableCents: Number(checkbox.dataset.availableCents),
      });
      stale ||= checkbox.dataset.stale === "true";
    }

    try {
      const result = confirmationState(
        invoiceCents,
        selectedItems,
        partialCheckbox.checked,
        stale,
      );
      let remaining = result.allocated;
      for (const row of rows) {
        const checkbox = row.querySelector("[data-transaction-select]");
        const idInput = row.querySelector("[data-transaction-input]");
        const expectedOpenInput = row.querySelector(
          "[data-expected-transaction-input]",
        );
        const amountInput = row.querySelector("[data-allocation-input]");
        const available = Number(checkbox.dataset.availableCents);
        const amount = checkbox.checked ? Math.min(remaining, available) : 0;
        remaining -= amount;
        idInput.disabled = amount === 0;
        expectedOpenInput.disabled = amount === 0;
        amountInput.disabled = amount === 0;
        amountInput.value = centsToInput(amount);
      }
      const summary = allocationSummary(result);
      selectedOutput.textContent = summary.selected;
      allocatedOutput.textContent = summary.allocated;
      for (const output of differenceOutputs) {
        output.textContent = summary.difference;
      }
      partialBlock.hidden = !result.partial;
      partialCheckbox.required = result.partial;
      if (!result.partial) partialCheckbox.checked = false;
      button.disabled = !result.canConfirm;
    } catch (_error) {
      button.disabled = true;
    }
  }

  for (const row of rows) {
    row.querySelector("[data-transaction-select]").addEventListener("change", update);
  }
  partialCheckbox.addEventListener("change", update);
  update();
}


if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", initWorkbench);
}
