const VERSION_PATTERN = /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/;
const TIMESTAMP_PATTERN = /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,6})?\+00:00$/;
const TASK_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const PENDING_START_KEY = "shunda.system-update.pending-start";
const PENDING_START_FIELDS = ["target_version", "task_id"];
const PENDING_CLEANUP_GUIDANCE = "升级已完成，但旧版本清理尚未完成。请联系系统管理员按运维流程处理，完成前请勿再次升级。";
const MANUAL_INTERVENTION_GUIDANCE = "自动恢复未完成。请停止继续升级并联系系统管理员处理；备份和相关版本已保留。";
const SHANGHAI_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const STAGE_PRESENTATIONS = {
  checking: { state: "active", label: "正在检查升级条件" },
  backing_up: { state: "active", label: "正在创建备份" },
  pulling: { state: "active", label: "正在下载升级版本" },
  stopping_web: { state: "active", label: "正在准备切换" },
  migrating: { state: "active", label: "正在执行数据迁移" },
  starting_web: { state: "active", label: "正在启动服务" },
  checking_health: { state: "active", label: "正在检查服务状态" },
  stabilizing: { state: "active", label: "正在稳定运行状态" },
  persisting_version: { state: "active", label: "正在记录版本" },
  cleaning: { state: "active", label: "正在清理临时资源" },
  rolling_back: { state: "active", label: "正在回退版本" },
  succeeded: { state: "succeeded", label: "升级成功" },
  failed: { state: "failed", label: "升级失败" },
  manual_intervention: { state: "manual", label: "需要人工处理" },
};

const ERROR_MESSAGES = {
  "": "",
  backup_failed: "备份失败，请联系管理员。",
  pull_failed: "下载升级版本失败，请联系管理员。",
  migration_failed: "升级失败，请联系管理员。",
  health_check_failed: "升级后检查失败，请联系管理员。",
  rollback_failed: "升级失败，需要人工处理。",
  update_failed: "升级失败，请联系管理员。",
};

const TERMINAL_STAGES = new Set(["succeeded", "failed", "manual_intervention"]);
const TASK_FIELDS = [
  "id",
  "from_version",
  "to_version",
  "stage",
  "created_at",
  "started_at",
  "finished_at",
  "backup_complete",
  "rolled_back",
  "cleanup",
  "error_code",
  "error_message",
];
const STATUS_FIELDS = [
  "current_version",
  "current_published_at",
  "latest_version",
  "latest_published_at",
  "update_available",
  "checked_at",
  "task",
];


function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}


function hasExactFields(value, fields) {
  return isRecord(value)
    && Object.keys(value).length === fields.length
    && fields.every((field) => Object.hasOwn(value, field));
}


function isVersion(value) {
  return typeof value === "string" && VERSION_PATTERN.test(value);
}


export function createTaskId(randomSource) {
  if (randomSource === null || typeof randomSource?.getRandomValues !== "function") {
    throw new Error("secure_random_unavailable");
  }
  const bytes = new Uint8Array(16);
  randomSource.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}


function isPendingStart(value) {
  return hasExactFields(value, PENDING_START_FIELDS)
    && isVersion(value.target_version)
    && typeof value.task_id === "string"
    && UUID_V4_PATTERN.test(value.task_id);
}


export function loadPendingStart(storage) {
  try {
    const value = JSON.parse(storage.getItem(PENDING_START_KEY));
    return isPendingStart(value) ? value : null;
  } catch (_error) {
    return null;
  }
}


function persistPendingStart(storage, pendingStart) {
  try {
    storage.setItem(PENDING_START_KEY, JSON.stringify(pendingStart));
  } catch (_error) {
    return;
  }
}


function clearPendingStart(storage) {
  try {
    storage.removeItem(PENDING_START_KEY);
  } catch (_error) {
    return;
  }
}


export function pendingStartForTarget(targetVersion, pendingStart, storage, randomSource) {
  if (!isVersion(targetVersion)) throw new Error("invalid_target_version");
  if (isPendingStart(pendingStart) && pendingStart.target_version === targetVersion) {
    return pendingStart;
  }
  const nextPendingStart = {
    target_version: targetVersion,
    task_id: createTaskId(randomSource),
  };
  persistPendingStart(storage, nextPendingStart);
  return nextPendingStart;
}


export function reconcilePendingStart(pendingStart, status, storage) {
  if (!isPendingStart(pendingStart)) return null;
  if (
    !isStatus(status)
    || status.task === null
    || !TERMINAL_STAGES.has(status.task.stage)
  ) {
    return pendingStart;
  }
  clearPendingStart(storage);
  return null;
}


function parseUtcTimestamp(value) {
  if (typeof value !== "string") return null;
  const match = TIMESTAMP_PATTERN.exec(value);
  if (match === null) return null;
  const [year, month, day, hour, minute, second] = match.slice(1).map(Number);
  const monthEnd = new Date(0);
  monthEnd.setUTCFullYear(year, month, 0);
  if (
    year < 1
    || month < 1
    || month > 12
    || day < 1
    || day > monthEnd.getUTCDate()
    || hour > 23
    || minute > 59
    || second > 59
  ) {
    return null;
  }
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? null : timestamp;
}


function isNullableTimestamp(value) {
  return value === null || parseUtcTimestamp(value) !== null;
}


function isTask(value) {
  return hasExactFields(value, TASK_FIELDS)
    && typeof value.id === "string"
    && TASK_ID_PATTERN.test(value.id)
    && isVersion(value.from_version)
    && isVersion(value.to_version)
    && Object.hasOwn(STAGE_PRESENTATIONS, value.stage)
    && typeof value.created_at === "string"
    && TIMESTAMP_PATTERN.test(value.created_at)
    && isNullableTimestamp(value.started_at)
    && isNullableTimestamp(value.finished_at)
    && typeof value.backup_complete === "boolean"
    && typeof value.rolled_back === "boolean"
    && ["not_run", "complete", "pending"].includes(value.cleanup)
    && typeof value.error_code === "string"
    && Object.hasOwn(ERROR_MESSAGES, value.error_code)
    && value.error_message === ERROR_MESSAGES[value.error_code];
}


function isStatus(value) {
  return hasExactFields(value, STATUS_FIELDS)
    && isVersion(value.current_version)
    && isNullableTimestamp(value.current_published_at)
    && (value.latest_version === null || isVersion(value.latest_version))
    && isNullableTimestamp(value.latest_published_at)
    && typeof value.update_available === "boolean"
    && isNullableTimestamp(value.checked_at)
    && (value.task === null || isTask(value.task));
}


export function formatShanghaiTimestamp(value) {
  if (value === null) return "-";
  const timestamp = parseUtcTimestamp(value);
  if (timestamp === null) return "-";
  const parts = Object.fromEntries(
    SHANGHAI_FORMATTER.formatToParts(timestamp).map(({ type, value: part }) => [type, part]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}


function emptyStatusMetadata() {
  return {
    currentPublishedAt: "-",
    latestPublishedAt: "-",
    taskId: "-",
    taskCreatedAt: "-",
    taskStartedAt: "-",
    taskFinishedAt: "-",
    guidance: "",
  };
}


export function statusMetadata(status) {
  if (!isStatus(status)) return emptyStatusMetadata();
  const metadata = emptyStatusMetadata();
  metadata.currentPublishedAt = formatShanghaiTimestamp(status.current_published_at);
  metadata.latestPublishedAt = formatShanghaiTimestamp(status.latest_published_at);
  if (status.task === null) return metadata;
  metadata.taskId = status.task.id;
  metadata.taskCreatedAt = formatShanghaiTimestamp(status.task.created_at);
  metadata.taskStartedAt = formatShanghaiTimestamp(status.task.started_at);
  metadata.taskFinishedAt = formatShanghaiTimestamp(status.task.finished_at);
  metadata.guidance = upgradeDecision(status).guidance;
  return metadata;
}


export function nextRetryDelay(attempt) {
  return [2000, 4000, 5000][Math.min(Math.max(0, attempt), 2)];
}


export function statusPresentation(status) {
  if (!isStatus(status)) {
    return {
      valid: false,
      state: "invalid",
      label: "状态数据无效",
      stage: "",
      error: "状态数据无效，请稍后重试。",
    };
  }
  if (status.task === null) {
    return {
      valid: true,
      state: status.update_available ? "available" : "current",
      label: status.update_available ? "发现可用更新" : "已是最新版本",
      stage: "",
      error: "",
    };
  }
  const presentation = STAGE_PRESENTATIONS[status.task.stage];
  return {
    valid: true,
    state: presentation.state,
    label: presentation.label,
    stage: status.task.stage,
    error: ERROR_MESSAGES[status.task.error_code],
  };
}


export function upgradeDecision(status) {
  if (!isStatus(status)) {
    return { canStart: false, availability: "无法确认", guidance: "" };
  }
  if (status.task?.stage === "manual_intervention") {
    return {
      canStart: false,
      availability: "需要管理员处理，暂不可升级",
      guidance: MANUAL_INTERVENTION_GUIDANCE,
    };
  }
  if (status.task?.cleanup === "pending") {
    return {
      canStart: false,
      availability: "旧版本清理待完成，暂不可升级",
      guidance: PENDING_CLEANUP_GUIDANCE,
    };
  }
  if (!status.update_available) {
    return { canStart: false, availability: "当前版本已是最新", guidance: "" };
  }
  if (!isVersion(status.latest_version)) {
    return { canStart: false, availability: "无法确认可用版本", guidance: "" };
  }
  if (
    status.task === null
    || status.task.stage === "failed"
    || (status.task.stage === "succeeded" && status.task.cleanup === "complete")
  ) {
    return { canStart: true, availability: "可升级", guidance: "" };
  }
  return { canStart: false, availability: "升级进行中，暂不可升级", guidance: "" };
}


export function canStart(status) {
  return upgradeDecision(status).canStart;
}


export function pollDecision(status) {
  if (!isStatus(status) || status.task === null || TERMINAL_STAGES.has(status.task.stage)) {
    return { poll: false, delay: 0 };
  }
  return { poll: true, delay: 2000 };
}


function initSystemUpdate() {
  const root = document.querySelector("[data-system-update]");
  if (!root) return;

  const elements = {
    check: root.querySelector("[data-check]"),
    refresh: root.querySelector("[data-status-refresh]"),
    start: root.querySelector("[data-start]"),
    startLabel: root.querySelector("[data-start-label]"),
    dialog: document.querySelector("[data-confirm-dialog]"),
    dialogVersion: document.querySelector("[data-confirm-version]"),
    confirmStart: document.querySelector("[data-confirm-start]"),
    currentVersion: root.querySelector("[data-current-version]"),
    currentPublishedAt: root.querySelector("[data-current-published-at]"),
    latestVersion: root.querySelector("[data-latest-version]"),
    latestPublishedAt: root.querySelector("[data-latest-published-at]"),
    availability: root.querySelector("[data-update-availability]"),
    status: root.querySelector("[data-update-status]"),
    message: root.querySelector("[data-update-message]"),
    backup: root.querySelector("[data-backup-state]"),
    rollback: root.querySelector("[data-rollback-state]"),
    cleanup: root.querySelector("[data-cleanup-state]"),
    taskId: root.querySelector("[data-task-id]"),
    taskCreatedAt: root.querySelector("[data-task-created-at]"),
    taskStartedAt: root.querySelector("[data-task-started-at]"),
    taskFinishedAt: root.querySelector("[data-task-finished-at]"),
    guidance: root.querySelector("[data-operation-guidance]"),
    stages: [...root.querySelectorAll("[data-progress-stage]")],
    csrf: root.querySelector("[data-csrf]"),
  };
  if (Object.values(elements).some((element) => element === null)) return;

  let latestStatus = null;
  let retryAttempt = 0;
  let timerId = null;
  let inflight = false;
  let disposed = false;
  let pendingStorage = null;
  try {
    pendingStorage = window.sessionStorage;
  } catch (_error) {
    pendingStorage = null;
  }
  let pendingStart = loadPendingStart(pendingStorage);

  const setText = (element, value) => {
    element.textContent = value;
  };
  const activeTask = () => latestStatus !== null && statusPresentation(latestStatus).state === "active";

  function clearTimer() {
    if (timerId !== null) {
      window.clearTimeout(timerId);
      timerId = null;
    }
  }

  function scheduleStatus(delay) {
    clearTimer();
    if (disposed) return;
    timerId = window.setTimeout(() => {
      timerId = null;
      void requestStatus();
    }, delay);
  }

  function setControls() {
    const canStartUpdate = upgradeDecision(latestStatus).canStart;
    elements.check.disabled = inflight || activeTask();
    elements.refresh.disabled = inflight;
    elements.start.hidden = !canStartUpdate;
    elements.start.disabled = inflight || !canStartUpdate;
  }

  function renderProgress(stage) {
    const activeIndex = elements.stages.findIndex((row) => row.dataset.progressStage === stage);
    for (let index = 0; index < elements.stages.length; index += 1) {
      const row = elements.stages[index];
      row.dataset.state = activeIndex === -1 ? "pending" : index < activeIndex ? "complete" : index === activeIndex ? "active" : "pending";
    }
  }

  function renderTaskStates(task) {
    if (task === null) {
      setText(elements.backup, "等待任务");
      setText(elements.rollback, "未发生");
      setText(elements.cleanup, "等待任务");
      return;
    }
    setText(elements.backup, task.backup_complete ? "已完成" : "未完成");
    setText(elements.rollback, task.rolled_back ? "已完成" : "未发生");
    setText(elements.cleanup, { not_run: "未执行", pending: "待处理", complete: "已完成" }[task.cleanup]);
  }

  function renderMetadata(status) {
    const metadata = statusMetadata(status);
    setText(elements.currentPublishedAt, metadata.currentPublishedAt);
    setText(elements.latestPublishedAt, metadata.latestPublishedAt);
    setText(elements.taskId, metadata.taskId);
    setText(elements.taskCreatedAt, metadata.taskCreatedAt);
    setText(elements.taskStartedAt, metadata.taskStartedAt);
    setText(elements.taskFinishedAt, metadata.taskFinishedAt);
    setText(elements.guidance, metadata.guidance);
    elements.guidance.hidden = !metadata.guidance;
    elements.guidance.dataset.state = status?.task?.stage === "manual_intervention" ? "error" : "warning";
  }

  function render(status) {
    const presentation = statusPresentation(status);
    if (!presentation.valid) {
      latestStatus = null;
      setText(elements.currentVersion, "—");
      setText(elements.latestVersion, "—");
      setText(elements.availability, "无法确认");
      setText(elements.status, presentation.label);
      elements.status.dataset.state = presentation.state;
      renderProgress("");
      renderTaskStates(null);
      renderMetadata(null);
      setMessage(presentation.error, "error");
      setControls();
      return false;
    }

    latestStatus = status;
    const decision = upgradeDecision(status);
    setText(elements.currentVersion, status.current_version);
    setText(elements.latestVersion, status.latest_version || "暂无可用版本");
    setText(elements.availability, decision.availability);
    setText(elements.status, presentation.label);
    elements.status.dataset.state = presentation.state;
    renderProgress(presentation.stage);
    renderTaskStates(status.task);
    renderMetadata(status);
    setText(elements.startLabel, status.latest_version ? `升级到 ${status.latest_version}` : "等待检查");
    setMessage(presentation.error, presentation.error ? "error" : "");
    setControls();
    return true;
  }

  function setMessage(message, state) {
    setText(elements.message, message);
    elements.message.hidden = !message;
    elements.message.dataset.state = state;
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json", ...options.headers },
      ...options,
    });
    if (!response.ok) throw new Error("updater_unavailable");
    try {
      return await response.json();
    } catch (_error) {
      throw new Error("invalid_response");
    }
  }

  function handleStatusFailure() {
    setMessage("无法连接升级服务，将自动重试。", "error");
    setControls();
    scheduleStatus(nextRetryDelay(retryAttempt));
    retryAttempt += 1;
  }

  function settleStatus(status) {
    if (!render(status)) {
      clearTimer();
      return;
    }
    pendingStart = reconcilePendingStart(pendingStart, status, pendingStorage);
    retryAttempt = 0;
    const decision = pollDecision(status);
    if (decision.poll) scheduleStatus(decision.delay);
    else clearTimer();
  }

  async function requestStatus() {
    if (disposed || inflight) return;
    inflight = true;
    setControls();
    try {
      const status = await fetchJson(root.dataset.statusUrl);
      settleStatus(status);
    } catch (_error) {
      handleStatusFailure();
    } finally {
      inflight = false;
      setControls();
    }
  }

  async function checkForUpdates() {
    if (disposed || inflight || activeTask()) return;
    inflight = true;
    setControls();
    setMessage("正在检查更新。", "info");
    try {
      const status = await fetchJson(root.dataset.checkUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": elements.csrf.value,
        },
        body: "{}",
      });
      settleStatus(status);
    } catch (_error) {
      handleStatusFailure();
    } finally {
      inflight = false;
      setControls();
    }
  }

  function openConfirmation() {
    if (!canStart(latestStatus) || latestStatus.latest_version === null) return;
    setText(elements.dialogVersion, latestStatus.latest_version);
    setText(elements.confirmStart, `确认升级到 ${latestStatus.latest_version}`);
    elements.dialog.showModal();
  }

  async function startUpdate() {
    if (disposed || inflight || !canStart(latestStatus) || latestStatus.latest_version === null) return;
    const targetVersion = latestStatus.latest_version;
    elements.dialog.close();
    inflight = true;
    setControls();
    setMessage("正在提交升级任务。", "info");
    try {
      pendingStart = pendingStartForTarget(
        targetVersion,
        pendingStart,
        pendingStorage,
        window.crypto,
      );
      const task = await fetchJson(root.dataset.startUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": elements.csrf.value,
        },
        body: JSON.stringify({
          target_version: targetVersion,
          task_id: pendingStart.task_id,
        }),
      });
      if (
        !isTask(task)
        || task.id !== pendingStart.task_id
        || task.to_version !== targetVersion
      ) {
        throw new Error("invalid_response");
      }
      settleStatus({ ...latestStatus, task });
    } catch (_error) {
      setMessage("无法提交升级任务，请稍后重试。", "error");
    } finally {
      inflight = false;
      setControls();
    }
  }

  elements.check.addEventListener("click", () => void checkForUpdates());
  elements.refresh.addEventListener("click", () => void requestStatus());
  elements.start.addEventListener("click", openConfirmation);
  elements.confirmStart.addEventListener("click", () => void startUpdate());
  window.addEventListener("pagehide", () => {
    disposed = true;
    clearTimer();
  }, { once: true });
  void requestStatus();
}


if (typeof document !== "undefined") {
  initSystemUpdate();
}
