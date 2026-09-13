window.readResponse = async function(response) {
  const text = await response.text();
  let value;
  try { value = text ? JSON.parse(text) : {}; }
  catch {
    const error = new Error(`服务暂时未能处理请求（HTTP ${response.status}），请稍后重试。`);
    error.status = response.status;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(typeof value.detail === 'string' ? value.detail : `请求失败（HTTP ${response.status}），请检查输入长度和内容后重试。`);
    error.status = response.status;
    error.definitelyRejected = value.request_state === 'failed' ||
      (value.request_state !== 'pending' && [400,401,403,404,405,409,413,422].includes(response.status));
    throw error;
  }
  return value;
};

// Bound both connection and body reading; a deadline does not undo server writes.
window.requestJSON = async function(path, options={}, timeoutMs=options.method && options.method !== 'GET' ? 120000 : 15000) {
  return requestWithDeadline(path, options, readResponse, timeoutMs);
};
window.requestText = async function(path, options={}) {
  return requestWithDeadline(path, options, async response => {
    if (!response.ok) throw new Error(`读取页面失败（HTTP ${response.status}），请重新读取。`);
    return response.text();
  }, 15000);
};
async function requestWithDeadline(path, options, read, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try { return await read(await fetch(path, {...options, signal:controller.signal})); }
  catch (error) {
    if (controller.signal.aborted) throw new Error('等待响应超时，服务端可能仍在处理。请按页面提示恢复或重新读取，已有记录保留。');
    throw error;
  } finally { clearTimeout(timer); }
}

// Persist drafts and unknown requests separately. Storage failure never prevents saving.
window.learningStore = (() => {
  const memory = new Map();
  let available = true;
  return {
    get available() { return available; },
    get(key) {
      try { return JSON.parse(localStorage.getItem(key) || 'null') ?? memory.get(key) ?? null; }
      catch { available = false; return memory.get(key) ?? null; }
    },
    set(key, value) {
      memory.set(key, value);
      try { localStorage.setItem(key, JSON.stringify(value)); }
      catch { available = false; }
    },
    remove(key) {
      memory.delete(key);
      try { localStorage.removeItem(key); } catch { available = false; }
    }
  };
})();

window.bindDraft = function(input, key) {
  if (!input) return {clear() {}};
  const restored = learningStore.get(key);
  if (typeof restored === 'string') input.value = restored;
  const status = document.createElement('small');
  status.className = 'muted draft-status'; status.setAttribute('role', 'status');
  input.after(status);
  const announce = () => {
    status.textContent = learningStore.available ? '草稿保存在当前浏览器，未提交不计完成。' : '浏览器禁止存储：可正常提交，但关闭或刷新会丢失草稿。';
  };
  input.addEventListener('input', () => { learningStore.set(key, input.value); announce(); });
  announce();
  return {clear(savedValue) { if (savedValue === undefined || input.value === savedValue) learningStore.remove(key); }};
};

// Overlapping actions on the same object must not unlock one another's controls.
window.lockControls = (() => {
  const locks = new WeakMap();
  return controls => {
    const owned = [...new Set(controls)].filter(Boolean);
    for (const control of owned) {
      const state = locks.get(control) || {count:0, disabled:control.disabled};
      state.count += 1; locks.set(control, state); control.disabled = true;
    }
    let released = false;
    return () => {
      if (released) return; released = true;
      for (const control of owned) {
        const state = locks.get(control); state.count -= 1;
        if (!state.count) { control.disabled = state.disabled; locks.delete(control); }
      }
    };
  };
})();

window.savedRequest = async function(storageKey, path, values, method='POST') {
  let pending = learningStore.get(storageKey);
  if (pending && (pending.path !== path || JSON.stringify(pending.values) !== JSON.stringify(values))) {
    const error = new Error('上次请求结果还未确认。请先点击“重试上次提交”，确认后再继续。');
    error.pending = pending;
    throw error;
  }
  if (!pending) {
    pending = {path, values, method, key: crypto.randomUUID()};
    learningStore.set(storageKey, pending);
  }
  try {
    const data = await requestJSON(path, {method:pending.method || method,
      headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':pending.key},
      body:JSON.stringify(pending.values)});
    learningStore.remove(storageKey);
    return data;
  } catch (error) {
    if (error.definitelyRejected) learningStore.remove(storageKey);
    else error.pending = pending;
    throw error;
  }
};

document.addEventListener('click', async event => {
  const button = event.target.closest('[data-cancel-task]');
  if (!button) return;
  button.disabled = true;
  try {
    await requestJSON(`/api/tasks/${button.dataset.cancelTask}/cancel`, {
      method:'POST', headers:{'X-Requested-With':'learning-practice'}
    });
    const row = button.closest('.task-row'), parent = row.parentElement;
    row.remove();
    parent.dispatchEvent(new CustomEvent('learning-task-cancelled', {bubbles:true}));
  } catch (error) {
    const status = document.createElement('p'); status.setAttribute('role','status'); status.textContent = error.message;
    button.after(status); button.disabled = false;
  }
});
