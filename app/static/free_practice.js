(() => {
const active = batch => batch && ['queued', 'running'].includes(batch.status);
function promiseCopy(batch, resultBatch) {
  const currentTasks = (resultBatch?.result?.tasks || []).filter(task => task.status !== 'cancelled');
  if (currentTasks.length) return ['今天接着练', '题单保存在本机，修改下次设置不会换掉当前题目。'];
  if (active(batch)) return ['正在准备这批题', '可以先练另一栏；完成后会自动显示题目。'];
  if (resultBatch) return ['这次没有可用新题', '调整范围或更新题库后，再主动确认一批。'];
  if (batch) return ['这次没有成功出题', '按原设置重试，或修改设置后重新确认。'];
  return ['先确认今天的题单', '选好范围和数量，点击确认出题后，当天关闭再打开仍是同一批。'];
}
function cards(container, tasks, cancellable=false) {
  container.replaceChildren(...tasks.map(task => {
    const card = document.createElement('div'), content = document.createElement('div');
    card.className = 'task-row'; card.dataset.kind = task.question_type;
    const title = document.createElement('h3'), detail = document.createElement('p');
    title.textContent = `${task.is_variant ? '变种题 · ' : ''}${task.prompt.replace(/\*\*|`/g, '').split('\n').filter(Boolean).join(' ').slice(0, 200)}${task.prompt.length > 200 ? '…' : ''}`;
    detail.className = 'muted'; detail.textContent = `${({pending:'待练习',in_progress:'进行中',completed:'已完成',cancelled:'已取消'})[task.status] || task.status} · ${task.category_path}`;
    if (task.has_correction) detail.textContent += ' · 本版本有内容校正，请先核对';
    content.append(title, detail); card.append(content);
    if (task.status !== 'cancelled') {
      const link = document.createElement('a'); link.className = 'button-link'; link.href = task.session_id ? `/interview/${task.session_id}` : `/practice/${task.id}`;
      link.textContent = task.status === 'completed' ? '查看记录 →' : task.status === 'in_progress' ? '继续练习 →' : '开始练习 →';
      card.append(link);
    }
    if (cancellable && task.status === 'pending' && !task.session_id) {
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.dataset.cancelTask = task.id;
      cancel.textContent = '取消这道未开始的题'; card.append(cancel);
    }
    return card;
  }));
}
function bindPanel(panel) {
  const find = selector => panel.querySelector(selector), kind = panel.dataset.kind, label = kind === 'code' ? '代码' : '八股';
  const form = find('form'), source = find('.free-source'), mode = find('.free-mode'), theme = find('.free-theme'), count = find('.free-count');
  const submit = find('.free-submit'), output = find('.free-status'), recovery = find('.free-recovery'), choice = find('.free-choice-status');
  const requestKey = `learning-free-confirmed-batch-${kind}`, preferencesKey = `learning-free-preferences-${kind}`;
  let state = JSON.parse(document.querySelector('#free-state').textContent)[kind], activated = true, sending = false, timer = null, refreshing = false, revision = 0;
  const values = () => ({question_source:source.value, mode:mode.value, theme:theme.value,
    count:Number(count.value), only_new:source.value === 'original', question_type:kind});
  const preferences = learningStore.get(preferencesKey);
  if (preferences && typeof preferences === 'object') {
    if (['original','variant'].includes(preferences.question_source)) source.value = preferences.question_source;
    if (['random','topic'].includes(preferences.mode)) mode.value = preferences.mode;
    if (typeof preferences.theme === 'string') theme.value = preferences.theme;
    if (Number.isInteger(Number(preferences.count))) count.value = preferences.count;
  }
  function updateChoice(edited=false) {
    const variant = source.value === 'variant';
    find('.free-theme-label').hidden = mode.value === 'random'; theme.required = mode.value === 'topic';
    find('.free-generation-hint').hidden = !variant;
    count.max = variant ? panel.dataset.variantLimit : panel.dataset.originalLimit;
    if (Number(count.value) > Number(count.max)) count.value = count.max;
    submit.textContent = active(state.batch) ? '这一批正在准备中…' : `确认并${variant ? '生成' : mode.value === 'random' ? '随机抽' : '按描述抽'}${label}${variant ? '变种题' : '题'}`;
    submit.disabled = sending || active(state.batch) || Boolean(learningStore.get(requestKey));
    if (edited) {
      learningStore.set(preferencesKey, values());
      choice.textContent = `新设置将在下次点击时使用，已有题单和正在生成的内容不变。${learningStore.available ? '' : '浏览器未允许保存设置，刷新后使用最近一次请求的设置。'}`;
    }
  }
  function showButton(text, action) {
    const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
    button.addEventListener('click', action); recovery.append(button);
  }
  function render() {
    const batch = state.batch;
    const copy = promiseCopy(batch, state.result_batch);
    find('.batch-promise-label').textContent = copy[0];
    find('.batch-promise small').textContent = copy[1];
    recovery.replaceChildren();
    if (active(batch)) {
      const description = batch.spec.mode === 'topic' ? `，范围：${batch.spec.theme}` : '，随机范围';
      output.textContent = `正在后台准备 ${batch.spec.count} 道${label}${batch.spec.question_source === 'variant' ? '变种题' : '题'}${description}。请在本页查看结果，可以先练另一栏。`;
      if (batch.selection_progress) output.textContent += ` 已筛选 ${batch.selection_progress.completed} / ${batch.selection_progress.total} 批候选题。`;
    } else if (batch?.status === 'complete') {
      const method = batch.spec.question_source === 'variant' ? '模型生成并核对' : batch.spec.mode === 'topic' ? '模型筛选范围后从本地题库抽取' : '直接从本地题库随机抽取，未调用模型';
      output.textContent = `本次已准备 ${batch.result.added} 道${label}题 · ${method}。${batch.result.missing ? `还缺 ${batch.result.missing} 道，可调整范围或更新题库。` : ''}`;
      if (batch.result.warnings?.length) output.textContent += ' ' + batch.result.warnings.join(' ');
    } else if (batch) {
      output.textContent = `这批未完成：${batch.error}${state.result_batch ? ' 已确认的题目仍可练习。' : ' 可以重试，或修改设置后重新确认出题。'}`;
      if (!learningStore.get(requestKey)) showButton('按这批原设置重试', () => send(`/api/free-practice/batches/${batch.id}/retry`, {}, true));
    }
    find('.free-current').hidden = !state.result_batch;
    if (state.result_batch) {
      find('.free-current').hidden = false;
      const remaining = state.result_batch.result.tasks.filter(task => task.status !== 'cancelled');
      const completed = remaining.filter(task => task.status === 'completed').length;
      find('.free-current h4').textContent = remaining.length
        ? `今天的${label}题 · ${completed} / ${remaining.length} 已完成`
        : '这次没有可用新题';
      cards(find('.free-result'), remaining);
      if (!remaining.length) find('.free-result').textContent = state.result_batch.result.added ? '这批练习已处理完，可以再抽一批；需巩固的题请加入复习库。' : '本次没有可用的新题，旧待办已作废。请调整范围或更新题库后再试。';
    } else { find('.free-result').replaceChildren(); if (!batch) output.textContent = '今天还没有确认题单，选择范围并确认出题后开始。'; }
    const pending = learningStore.get(requestKey);
    if (pending) {
      output.textContent = '上次提交的响应还未确认，已有题目保留。请恢复上次提交，不会重复创建。';
      showButton('恢复上次提交', () => send(pending.path, pending.values));
    }
    updateChoice();
  }
  async function refresh() {
    if (!activated || refreshing || sending || document.hidden) return;
    refreshing = true;
    const startedRevision = revision;
    try {
      const current = (await requestJSON('/api/free-practice/state', {cache:'no-store'}))[kind];
      if (startedRevision === revision && !sending) {
        state = current;
        render();
      }
    } catch (error) {
      if (startedRevision === revision && !sending) {
        output.textContent = `暂时无法读取${label}进度：${error.message}。已显示的题目保留。`;
        recovery.replaceChildren(); showButton('重新读取进度', refresh);
      }
    } finally { refreshing = false; schedule(); }
  }
  function schedule() {
    clearTimeout(timer);
    if (active(state.batch)) timer = setTimeout(refresh, 1800);
  }
  async function send(path, body, retry=false) {
    if (sending) return;
    activated = true; sending = true; revision += 1; clearTimeout(timer); updateChoice();
    let accepted = false;
    recovery.querySelectorAll('button').forEach(button => button.disabled = true);
    output.textContent = retry ? '正在恢复这批练习…' : '正在保存这次练习请求…';
    try {
      const batch = await savedRequest(requestKey, path, body);
      accepted = true;
      state.batch = batch;
      if (batch.status === 'complete') state.result_batch = batch;
      if (batch.reused_active) choice.textContent = `已有一批${label}题正在准备，当前继续显示那一批；新设置尚未提交。`;
      render();
    } catch (error) {
      render();
      if (!error.pending) output.textContent = error.message;
    } finally { sending = false; updateChoice(); schedule(); }
    if (accepted && !active(state.batch) && !learningStore.get(requestKey)) await refresh();
  }
  form.addEventListener('input', () => updateChoice(true));
  form.addEventListener('change', () => updateChoice(true));
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (sending || active(state.batch) || !form.reportValidity()) return;
    if (mode.value === 'topic' && !theme.value.trim()) { output.textContent = '请写下这一栏想练的内容。'; return; }
    send('/api/free-practice/batches', values());
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pageshow', () => refresh());
  window.addEventListener('focus', () => refresh());
  panel.addEventListener('learning-task-cancelled', () => refresh());
  render(); schedule();
  window.addEventListener('learning-free-restored', refresh);
}
requestJSON('/api/free-practice/restore', {method:'POST', headers:{'X-Requested-With':'learning-practice'}})
  .then(() => window.dispatchEvent(new Event('learning-free-restored')))
  .catch(error => { for (const node of document.querySelectorAll('.free-status')) node.textContent = `今日题单恢复未确认：${error.message}。刷新可安全重试。`; });
for (const panel of document.querySelectorAll('.free-panel')) bindPanel(panel);
})();
