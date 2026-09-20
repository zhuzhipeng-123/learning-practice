const generate = document.querySelector('#create-plan');
const feedback = document.querySelector('#plan-result');
const adoptLegacy = document.querySelector('#adopt-legacy');
const quotaInputs = [...document.querySelectorAll('.module-quota')];
const scopeInputs = [...document.querySelectorAll('.module-scope')];
const saved = JSON.parse(document.querySelector('#saved-allocation').textContent);
const savedScope = JSON.parse(document.querySelector('#saved-theory-scope').textContent);
quotaInputs.forEach(input => { input.value = saved[input.dataset.path] || 0; });
const scopeById = new Map(scopeInputs.map(input => [input.dataset.moduleId, input]));
function descendants(id) { return scopeInputs.filter(input => {
  let parent = input.dataset.parentId;
  while (parent) { if (parent === id) return true; parent = scopeById.get(parent)?.dataset.parentId; }
  return false;
}); }
function updateScopeParents() {
  [...scopeInputs].reverse().forEach(input => {
    const children = scopeInputs.filter(child => child.dataset.parentId === input.dataset.moduleId);
    if (!children.length) return;
    input.checked = children.every(child => child.checked);
    input.indeterminate = !input.checked && children.some(child => child.checked || child.indeterminate);
  });
}
function applyScope(ids) {
  scopeInputs.forEach(input => { input.checked = false; input.indeterminate = false; });
  ids.forEach(id => {
    const input = scopeById.get(id);
    if (input) { input.checked = true; descendants(id).forEach(child => { child.checked = true; }); }
  });
  updateScopeParents();
}
applyScope(savedScope);
function scopes() {
  return scopeInputs.filter(input => input.checked && !scopeById.get(input.dataset.parentId)?.checked)
    .map(input => input.dataset.moduleId);
}
function quotas() {
  const result = {};
  for (const input of quotaInputs) {
    if (!input.checkValidity()) throw Error('模块题数必须是 0–100 的整数。');
    if (Number(input.value)) result[input.dataset.path] = Number(input.value);
  }
  const paths = Object.keys(result);
  if (paths.some(a => paths.some(b => a !== b && descendants(a).some(input => input.dataset.moduleId === b)))) throw Error('父模块和子模块不能同时分配题数，请选择其中一层。');
  const allowed = new Set(scopes().flatMap(id => [id, ...descendants(id).map(input => input.dataset.moduleId)]));
  if (paths.some(path => !allowed.has(path))) throw Error('已排除范围仍有模块配额，请清零或重新勾选该模块。');
  return result;
}
function updateSummary() {
  try {
    const sum = Object.values(quotas()).reduce((a,b) => a+b,0);
    const remaining = Number(document.querySelector('#theory-target').value) - sum;
    const selected = scopes().length;
    document.querySelector('#quota-summary').textContent = remaining < 0 ? `指定范围超出八股题量 ${-remaining} 题，请调整上方数量` : !selected ? '尚未选择八股范围。' : sum ? `已选范围内指定 ${sum} 题，另外随机 ${remaining} 题` : `已选择 ${selected} 个范围，将只从这些范围抽取。`;
  } catch (error) { document.querySelector('#quota-summary').textContent = error.message; }
}
[...quotaInputs, document.querySelector('#theory-target')].forEach(input => input.addEventListener('input',updateSummary));
scopeInputs.forEach(input => input.addEventListener('click', event => event.stopPropagation()));
scopeInputs.forEach(input => input.addEventListener('change', () => {
  descendants(input.dataset.moduleId).forEach(child => { child.checked = input.checked; child.indeterminate = false; });
  updateScopeParents(); updateSummary(); showSavedTasks(); feedback.hidden = true;
}));
updateSummary();
const choices = [...scopeInputs, ...quotaInputs, document.querySelector('#code-target'), document.querySelector('#theory-target')];
const selectionState = () => JSON.stringify(choices.map(input => input.type === 'checkbox' ? [input.checked,input.indeterminate] : input.value));
let originalSelection = selectionState();
let ready = Boolean(generate.dataset.batchKey);
function showSavedTasks() {
  const changed = selectionState() !== originalSelection;
  document.querySelector('#unapplied-selection').hidden = ready && !changed;
  document.querySelector('#unapplied-selection').textContent = changed
    ? '选择已修改，当前题单或旧题单仍保留；确认后才会更新。'
    : adoptLegacy ? '旧题单尚未确认。请明确采用，或重新安排今天的题单。'
    : '选择题量并点击“确认今日题单”，题目才会出现在这里。';
  document.querySelector('#daily-task-list').hidden = !ready;
  document.querySelector('#saved-task-count').hidden = !ready;
}
choices.forEach(input => input.addEventListener('input', () => {
  showSavedTasks();
  feedback.hidden = true;
}));
async function applyPlan(data, body) {
  const preserveSelection = !body && selectionState() !== originalSelection;
  generate.dataset.planId = data.plan_id;
  const html = await requestText('/', {cache:'no-store'});
  const documentCopy = new DOMParser().parseFromString(html, 'text/html');
  if (documentCopy.querySelector('#create-plan').dataset.batchKey !== data.batch_key) throw Error('题单已在其他页面更新，请刷新后重新出题。');
  for (const id of ['daily-practice','plan-progress']) {
    const region = documentCopy.getElementById(id);
    if (!region) throw Error('题量已保存，请重新读取题单。');
    document.getElementById(id).replaceWith(region);
  }
  const allocation = JSON.parse(documentCopy.querySelector('#saved-allocation').textContent);
  const selectedScope = JSON.parse(documentCopy.querySelector('#saved-theory-scope').textContent);
  if (!preserveSelection) {
    for (const input of quotaInputs) input.value = allocation[input.dataset.path] || 0;
    for (const input of [document.querySelector('#code-target'),document.querySelector('#theory-target')]) input.value = documentCopy.getElementById(input.id).value;
    applyScope(selectedScope);
    updateSummary();
    originalSelection = selectionState();
  }
  generate.dataset.batchKey = documentCopy.querySelector('#create-plan').dataset.batchKey;
  ready = true; generate.textContent = '替换今天的题单'; showSavedTasks();
  const shortages = Object.entries(data.shortages || {}).map(([kind,n])=>`${kind==='code'?'代码':kind==='unallocated'?'八股总计':kind}缺 ${n} 题`).join('；');
  feedback.textContent = shortages ? `题量已保存。${shortages}。可调整范围或更新题库后再次保存。` : '题量已保存，下面的题单已更新。';
}
async function savePlan(body, path, method) {
  const unlock = lockControls([generate, adoptLegacy, ...choices].filter(Boolean));
  const keyName = `learning-plan-${body.plan_date}`;
  feedback.hidden = false;
  feedback.textContent = '正在从本地题库抽取…';
  try {
    const data = await savedRequest(keyName, path, body, method);
    try { await applyPlan(data, body); }
    catch (error) {
      feedback.textContent = error.message;
      const retry = document.createElement('button'); retry.textContent = '重新读取已保存的题单';
      retry.onclick = async () => { try { await applyPlan(data, body); } catch (failure) { feedback.textContent = failure.message; } };
      feedback.append(retry);
    }
  } catch(error) {
    feedback.textContent = error.message;
    if (error.pending) {
      const retry = document.createElement('button'); retry.textContent = '恢复上次保存';
      retry.onclick = () => savePlan(error.pending.values, error.pending.path, error.pending.method || 'POST');
      feedback.append(retry);
    }
  } finally { unlock(); }
}
generate.addEventListener('click', () => {
  try {
    const code = document.querySelector('#code-target'), theory = document.querySelector('#theory-target');
    if (!code.checkValidity() || !theory.checkValidity()) throw Error('题量必须是 0–100 的整数。');
    const body = {plan_date:generate.dataset.date,code_target:Number(code.value),theory_target:Number(theory.value),module_quotas:quotas(),theory_scope:scopes(),theory_catalog_version:generate.dataset.catalogVersion,fresh_batch:true,expected_batch:generate.dataset.batchKey || ''};
    if (body.theory_target > 0 && !body.theory_scope.length) throw Error('八股题量大于 0 时至少选择一个模块。');
    if (Object.values(body.module_quotas).reduce((a,b)=>a+b,0) > body.theory_target) throw Error('模块题数之和超过八股总量。');
    const path = generate.dataset.planId ? `/api/plans/${generate.dataset.planId}` : '/api/plans';
    savePlan(body, path, generate.dataset.planId ? 'PUT' : 'POST');
  } catch(error) { feedback.hidden = false; feedback.textContent = error.message; }
});
adoptLegacy?.addEventListener('click', () => {
  const code = document.querySelector('#code-target'), theory = document.querySelector('#theory-target');
  const body = {plan_date:generate.dataset.date,code_target:Number(code.value),theory_target:Number(theory.value),
    module_quotas:quotas(),theory_scope:scopes(),theory_catalog_version:generate.dataset.catalogVersion,
    adopt_legacy:true,expected_batch:adoptLegacy.dataset.batchKey};
  savePlan(body, `/api/plans/${generate.dataset.planId}`, 'PUT');
});
showSavedTasks();

async function restoreToday() {
  if (ready || learningStore.get(`learning-plan-${generate.dataset.date}`)) return;
  const unlock = lockControls([generate]);
  try {
    const data = await requestJSON('/api/practice/restore', {method:'POST', headers:{'X-Requested-With':'learning-practice'}});
    if (data.batch) {
      await applyPlan(data.batch);
      feedback.hidden = false;
      feedback.textContent = '已恢复今天确认的题单和进度。';
    }
  } catch (error) {
    feedback.hidden = false; feedback.textContent = error.message;
    const retry = document.createElement('button'); retry.textContent = '重新读取今日题单'; retry.onclick = restoreToday; feedback.append(retry);
  } finally { unlock(); }
}
restoreToday();

async function refreshToday() {
  try {
    const data = await requestJSON('/api/practice/current', {cache:'no-store'});
    if (data.batch) await applyPlan(data.batch);
  } catch (error) {
    feedback.hidden = false;
    feedback.textContent = `重新读取今日题单失败：${error.message}`;
  }
}
window.addEventListener('pageshow', event => { if (event.persisted) refreshToday(); });
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshToday(); });
