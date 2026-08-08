import test from "node:test";
import assert from "node:assert/strict";

import {
  canStart,
  createTaskId,
  formatShanghaiTimestamp,
  loadPendingStart,
  nextRetryDelay,
  pendingStartForTarget,
  pollDecision,
  reconcilePendingStart,
  statusMetadata,
  statusPresentation,
  upgradeDecision,
} from "./system-update.js";


const activeStatus = {
  current_version: "v0.2.0",
  current_published_at: "2026-08-06T23:30:00+00:00",
  latest_version: "v0.2.1",
  latest_published_at: "2026-08-07T12:00:00+00:00",
  update_available: true,
  checked_at: "2026-08-07T12:00:00+00:00",
  task: {
    id: "00000000-0000-0000-0000-000000000001",
    from_version: "v0.2.0",
    to_version: "v0.2.1",
    stage: "pulling",
    created_at: "2026-08-07T12:00:00+00:00",
    started_at: "2026-08-07T12:00:00+00:00",
    finished_at: null,
    backup_complete: true,
    rolled_back: false,
    cleanup: "not_run",
    error_code: "",
    error_message: "",
  },
};

const PENDING_GUIDANCE = "升级已完成，但旧版本清理尚未完成。请联系系统管理员按运维流程处理，完成前请勿再次升级。";
const MANUAL_GUIDANCE = "自动恢复未完成。请停止继续升级并联系系统管理员处理；备份和相关版本已保留。";


function memoryStorage(initialValue = null, { failRead = false, failWrite = false, failRemove = false } = {}) {
  let value = initialValue;
  return {
    get value() { return value; },
    getItem() {
      if (failRead) throw new Error("storage_read_failed");
      return value;
    },
    setItem(_key, nextValue) {
      if (failWrite) throw new Error("storage_write_failed");
      value = nextValue;
    },
    removeItem() {
      if (failRemove) throw new Error("storage_remove_failed");
      value = null;
    },
  };
}


const zeroCrypto = {
  getRandomValues(bytes) {
    bytes.fill(0);
    return bytes;
  },
};


test("task ids use getRandomValues and set UUID v4 version and variant bits", () => {
  const randomSource = {
    getRandomValues(bytes) {
      bytes.fill(0xff);
      return bytes;
    },
  };

  assert.equal(createTaskId(randomSource), "ffffffff-ffff-4fff-bfff-ffffffffffff");
});


test("pending starts load only an exact canonical version and UUID v4 schema", async (context) => {
  const valid = {
    target_version: "v0.2.1",
    task_id: "00000000-0000-4000-8000-000000000000",
  };
  assert.deepEqual(loadPendingStart(memoryStorage(JSON.stringify(valid))), valid);

  for (const malformed of [
    null,
    [],
    { target_version: "v0.2.1" },
    { ...valid, extra: true },
    { ...valid, target_version: "v00.2.1" },
    { ...valid, task_id: "00000000-0000-0000-0000-000000000001" },
    { ...valid, task_id: "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA" },
  ]) {
    await context.test(JSON.stringify(malformed), () => {
      assert.equal(loadPendingStart(memoryStorage(JSON.stringify(malformed))), null);
    });
  }
  assert.equal(loadPendingStart(memoryStorage("not-json")), null);
  assert.equal(loadPendingStart(memoryStorage(null, { failRead: true })), null);
});


test("a retry reuses the same pending start and a new target replaces it", () => {
  const storage = memoryStorage();
  const first = pendingStartForTarget("v0.2.1", null, storage, zeroCrypto);
  const retry = pendingStartForTarget("v0.2.1", first, storage, {
    getRandomValues() { throw new Error("must_not_generate"); },
  });

  assert.deepEqual(first, {
    target_version: "v0.2.1",
    task_id: "00000000-0000-4000-8000-000000000000",
  });
  assert.strictEqual(retry, first);
  assert.deepEqual(JSON.parse(storage.value), first);

  const replacement = pendingStartForTarget("v0.2.2", first, storage, zeroCrypto);
  assert.equal(replacement.target_version, "v0.2.2");
  assert.deepEqual(JSON.parse(storage.value), replacement);
});


test("storage failures degrade without losing the in-memory retry correlation", () => {
  const storage = memoryStorage(null, { failWrite: true });

  const pending = pendingStartForTarget("v0.2.1", null, storage, zeroCrypto);

  assert.equal(pending.task_id, "00000000-0000-4000-8000-000000000000");
  assert.equal(storage.value, null);
});


test("active status retains pending correlation and any terminal task clears it", () => {
  const pending = {
    target_version: "v0.2.1",
    task_id: "00000000-0000-4000-8000-000000000000",
  };
  const storage = memoryStorage(JSON.stringify(pending));
  const matchingActive = {
    ...activeStatus,
    task: { ...activeStatus.task, id: pending.task_id },
  };

  assert.strictEqual(reconcilePendingStart(pending, matchingActive, storage), pending);
  assert.notEqual(storage.value, null);

  const matchingTerminal = {
    ...matchingActive,
    task: { ...matchingActive.task, stage: "succeeded", cleanup: "complete" },
  };
  assert.equal(reconcilePendingStart(pending, matchingTerminal, storage), null);
  assert.equal(storage.value, null);

  const staleStorage = memoryStorage(JSON.stringify(pending));
  const unrelatedTerminal = {
    ...activeStatus,
    task: { ...activeStatus.task, stage: "failed", error_code: "pull_failed", error_message: "下载升级版本失败，请联系管理员。" },
  };
  assert.equal(reconcilePendingStart(pending, unrelatedTerminal, staleStorage), null);
  assert.equal(staleStorage.value, null);

  const failingStorage = memoryStorage(JSON.stringify(pending), { failRemove: true });
  assert.equal(reconcilePendingStart(pending, matchingTerminal, failingStorage), null);
});


test("retry delay is bounded", () => {
  assert.deepEqual([0, 1, 2, 3, 9].map(nextRetryDelay), [2000, 4000, 5000, 5000, 5000]);
});


test("UTC timestamps render in Asia/Shanghai with nullable values as a dash", () => {
  assert.equal(formatShanghaiTimestamp("2026-08-06T23:30:00+00:00"), "2026-08-07 07:30:00");
  assert.equal(formatShanghaiTimestamp(null), "-");
});


test("status metadata exposes safe task correlation, Shanghai times, and fixed guidance", () => {
  const pending = {
    ...activeStatus,
    task: { ...activeStatus.task, stage: "succeeded", cleanup: "pending", finished_at: "2026-08-07T12:01:00+00:00" },
  };

  assert.deepEqual(statusMetadata(pending), {
    currentPublishedAt: "2026-08-07 07:30:00",
    latestPublishedAt: "2026-08-07 20:00:00",
    taskId: "00000000-0000-0000-0000-000000000001",
    taskCreatedAt: "2026-08-07 20:00:00",
    taskStartedAt: "2026-08-07 20:00:00",
    taskFinishedAt: "2026-08-07 20:01:00",
    guidance: PENDING_GUIDANCE,
  });
  assert.equal(statusMetadata({
    ...pending,
    task: { ...pending.task, stage: "manual_intervention", error_code: "rollback_failed", error_message: "升级失败，需要人工处理。" },
  }).guidance, MANUAL_GUIDANCE);
  assert.equal(statusMetadata({ ...pending, task: { ...pending.task, cleanup: "complete" } }).guidance, "");
});


test("status presentation only returns allowlisted Chinese labels", () => {
  assert.deepEqual(statusPresentation(activeStatus), {
    valid: true,
    state: "active",
    label: "正在下载升级版本",
    stage: "pulling",
    error: "",
  });
  assert.deepEqual(statusPresentation({ ...activeStatus, task: { ...activeStatus.task, stage: "succeeded" } }), {
    valid: true,
    state: "succeeded",
    label: "升级成功",
    stage: "succeeded",
    error: "",
  });
  assert.deepEqual(statusPresentation({ ...activeStatus, task: { ...activeStatus.task, stage: "manual_intervention", error_code: "rollback_failed", error_message: "升级失败，需要人工处理。" } }), {
    valid: true,
    state: "manual",
    label: "需要人工处理",
    stage: "manual_intervention",
    error: "升级失败，需要人工处理。",
  });
});


test("malformed payload fails closed without retaining server values", () => {
  const malformed = {
    ...activeStatus,
    latest_version: "<img src=x onerror=alert(1)>",
  };

  assert.deepEqual(statusPresentation(malformed), {
    valid: false,
    state: "invalid",
    label: "状态数据无效",
    stage: "",
    error: "状态数据无效，请稍后重试。",
  });
  assert.equal(canStart(malformed), false);
  assert.deepEqual(pollDecision(malformed), { poll: false, delay: 0 });
  assert.deepEqual(statusMetadata(malformed), {
    currentPublishedAt: "-",
    latestPublishedAt: "-",
    taskId: "-",
    taskCreatedAt: "-",
    taskStartedAt: "-",
    taskFinishedAt: "-",
    guidance: "",
  });
  assert.equal(statusPresentation({ ...activeStatus, private_digest: "sha256:private" }).valid, false);
  assert.equal(statusPresentation({ ...activeStatus, current_published_at: "2026-02-31T12:00:00+00:00" }).valid, false);
});


test("an active task suppresses starting and checks every two seconds", () => {
  assert.equal(canStart(activeStatus), false);
  assert.deepEqual(pollDecision(activeStatus), { poll: true, delay: 2000 });
});


test("a terminal task stops polling while a verified available version can restart", () => {
  const completed = {
    ...activeStatus,
    task: { ...activeStatus.task, stage: "failed", error_code: "pull_failed", error_message: "下载升级版本失败，请联系管理员。" },
  };

  assert.equal(canStart(completed), true);
  assert.deepEqual(upgradeDecision(completed), {
    canStart: true,
    availability: "可升级",
    guidance: "",
  });
  assert.deepEqual(pollDecision(completed), { poll: false, delay: 0 });
  const manual = { ...completed, task: { ...completed.task, stage: "manual_intervention", error_code: "rollback_failed", error_message: "升级失败，需要人工处理。" } };
  const failedCleanupPending = { ...completed, task: { ...completed.task, cleanup: "pending" } };
  const succeededCleanupPending = { ...completed, task: { ...completed.task, stage: "succeeded", cleanup: "pending", error_code: "", error_message: "" } };
  const cleanupComplete = { ...succeededCleanupPending, task: { ...succeededCleanupPending.task, cleanup: "complete" } };
  assert.equal(canStart(manual), false);
  assert.equal(canStart(failedCleanupPending), false);
  assert.equal(canStart(succeededCleanupPending), false);
  assert.equal(canStart(cleanupComplete), true);
  assert.deepEqual(upgradeDecision(failedCleanupPending), {
    canStart: false,
    availability: "旧版本清理待完成，暂不可升级",
    guidance: PENDING_GUIDANCE,
  });
  assert.deepEqual(upgradeDecision(manual), {
    canStart: false,
    availability: "需要管理员处理，暂不可升级",
    guidance: MANUAL_GUIDANCE,
  });
  assert.deepEqual(pollDecision(manual), { poll: false, delay: 0 });
});
