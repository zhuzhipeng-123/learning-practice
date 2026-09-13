(() => {
const root = document.querySelector('#knowledge-editor'), questionId = root.dataset.questionId;
const status = document.querySelector('#knowledge-status');
const prompt = document.querySelector('#knowledge-prompt'), reference = document.querySelector('#knowledge-reference');
const category = document.querySelector('#knowledge-category'), verified = document.querySelector('#knowledge-verified');
const correction = document.querySelector('#correction-content'), sources = document.querySelector('#correction-sources');
let current = null;
for (const field of [prompt, reference]) field.addEventListener('input', () => { verified.checked = false; });
async function load() {
  const unlock = lockControls(root.querySelectorAll('button,input,textarea,select'));
  try {
    current = await requestJSON(`/api/questions/${questionId}/knowledge`, {method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'},
      body:JSON.stringify({version_id:document.querySelector('#knowledge-version').value})});
    prompt.value = current.prompt; reference.value = current.reference_text; category.value = current.category_path;
    [prompt, reference, category].forEach(field => { field.readOnly = !current.editable; });
    verified.checked = current.reference_verification.verified;
    correction.value = current.reference_correction?.content || '';
    sources.value = (current.reference_correction?.sources || []).join('\n');
    document.querySelector('#knowledge-fields').hidden = false;
    document.querySelector('#knowledge-derived').hidden = !current.editable;
    status.textContent = '已读取保存内容。修改题干或答案后，需要重新核对才能勾选核验。';
  } catch (error) { status.textContent = error.message; }
  finally { unlock(); }
}
document.querySelector('#load-knowledge').onclick = load;
async function save(kind, pending) {
  if (!current) return;
  const isCorrection = kind === 'correction';
  const body = pending?.values || (isCorrection ? {content:correction.value,
    sources:sources.value.split('\n').map(line => line.trim()).filter(Boolean), expected_id:current.reference_correction?.id || ''}
    : {expected_version_id:current.version_id, prompt:prompt.value, reference_text:reference.value,
       category_path:category.value, reference_verified:verified.checked});
  const path = pending?.path || (isCorrection ? `/api/versions/${current.version_id}/correction` : `/api/questions/${questionId}/knowledge`);
  const unlock = lockControls(root.querySelectorAll('button,input,textarea,select'));
  const key = `learning-knowledge-${questionId}-${kind}`;
  try {
    const data = await savedRequest(key, path, body, 'PUT');
    if (isCorrection) current.reference_correction = data;
    else {
      if (data.current_version_id && data.current_version_id !== data.version_id) {
        const select = document.querySelector('#knowledge-version');
        if (![...select.options].some(option => option.value === data.current_version_id)) select.add(new Option('最新保存版本', data.current_version_id));
        select.value = data.current_version_id;
        status.textContent = '上次保存已恢复，题库已有更新版本。请重新读取后继续编辑；当前草稿保留。';
        return;
      }
      const changedVersion = current.version_id !== data.version_id;
      current.version_id = data.version_id;
      root.dataset.currentVersion = data.version_id;
      const select = document.querySelector('#knowledge-version');
      if (![...select.options].some(option => option.value === data.version_id)) select.add(new Option('刚保存的新版本', data.version_id));
      select.value = data.version_id;
      if (changedVersion) { current.reference_correction = null; correction.value = ''; sources.value = ''; }
      document.querySelector('#question-heading').textContent = body.prompt;
      document.querySelector('#question-metadata').textContent = body.category_path + ' · 派生题';
      document.querySelector('#knowledge-verification-status').textContent = body.reference_verified ? '当前参考已由你核对' : '当前参考待核对，不能用于自动评分；你仍可手动评价。';
    }
    status.textContent = isCorrection ? '校正已保存；新评价会使用它，历史请求保持原依据。' : '新版本已保存。已安排的任务和旧作答仍使用原版本。';
  } catch (error) {
    status.textContent = error.message;
    if (error.pending) {
      const retry = document.createElement('button'); retry.textContent = '恢复上次保存';
      retry.onclick = () => save(kind, error.pending); status.append(retry);
    }
  } finally { unlock(); }
}
document.querySelector('#save-knowledge').onclick = () => save('content');
document.querySelector('#save-correction').onclick = () => save('correction');
})();
