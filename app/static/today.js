const generate = document.querySelector('#create-plan');
const feedback = document.querySelector('#plan-result');
const quotaInputs = [...document.querySelectorAll('.module-quota')];
const saved = JSON.parse(document.querySelector('#saved-allocation').textContent);
quotaInputs.forEach(input => { input.value = saved[input.dataset.path] || 0; });
function quotas() {
  const result = {};
  for (const input of quotaInputs) {
    if (!input.checkValidity()) throw Error('模块题数必须是 0–100 的整数。');
    if (Number(input.value)) result[input.dataset.path] = Number(input.value);
  }
  const paths = Object.keys(result);
  if (paths.some(a => paths.some(b => a !== b && b.startsWith(a + ' > ')))) throw Error('父模块和子模块不能同时分配题数，请选择其中一层。');
  return result;
}
function updateSummary() {
  try {
    const sum = Object.values(quotas()).reduce((a,b) => a+b,0);
    const remaining = Number(document.querySelector('#theory-target').value) - sum;
    document.querySelector('#quota-summary').textContent = remaining < 0 ? `指定范围超出八股题量 ${-remaining} 题，请调整上方数量` : sum ? `指定模块 ${sum} 题，另外随机 ${remaining} 题` : '八股未指定子模块，将从全部八股题中随机选择。';
  } catch (error) { document.querySelector('#quota-summary').textContent = error.message; }
}
[...quotaInputs, document.querySelector('#theory-target')].forEach(input => input.addEventListener('input',updateSummary));
updateSummary();
const choices = [...quotaInputs, document.querySelector('#code-target'), document.querySelector('#theory-target')];
const selectionState = () => JSON.stringify(choices.map(input => input.value));
let originalSelection = selectionState();
let ready = Boolean(generate.dataset.batchKey);
function showSavedTasks() {
  const changed = selectionState() !== originalSelection;
  document.querySelector('#unapplied-selection').hidden = ready && !changed;
  document.querySelector('#unapplied-selection').textContent = changed
    ? '选择已修改，当前仍显示已保存的题单；保存后更新。'
    : '选择题量并点击“生成这次练习”，题目才会出现在这里。';
  document.querySelector('#daily-task-list').hidden = !ready;
  document.querySelector('#saved-task-count').hidden = !ready;
}
choices.forEach(input => input.addEventListener('input', () => {
  showSavedTasks();
  feedback.hidden = true;
}));
async function applyPlan(data, body) {
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
  if (!body && selectionState() === originalSelection) {
    for (const input of choices) input.value = input.dataset.path ? allocation[input.dataset.path] || 0 : documentCopy.getElementById(input.id).value;
    updateSummary();
  }
  generate.dataset.batchKey = documentCopy.querySelector('#create-plan').dataset.batchKey;
  originalSelection = JSON.stringify(choices.map(input => String(input.dataset.path ? allocation[input.dataset.path] || 0 : documentCopy.getElementById(input.id).value)));
  ready = true; generate.textContent = '替换今天的题单'; showSavedTasks();
  const shortages = Object.entries(data.shortages || {}).map(([kind,n])=>`${kind==='code'?'代码':kind==='unallocated'?'八股总计':kind}缺 ${n} 题`).join('；');
  feedback.textContent = shortages ? `题量已保存。${shortages}。可调整范围或更新题库后再次保存。` : '题量已保存，下面的题单已更新。';
}
async function savePlan(body, path, method) {
  const unlock = lockControls([generate, ...choices]);
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
    const body = {plan_date:generate.dataset.date,code_target:Number(code.value),theory_target:Number(theory.value),module_quotas:quotas(),fresh_batch:true,expected_batch:generate.dataset.batchKey || ''};
    if (Object.values(body.module_quotas).reduce((a,b)=>a+b,0) > body.theory_target) throw Error('模块题数之和超过八股总量。');
    const path = generate.dataset.planId ? `/api/plans/${generate.dataset.planId}` : '/api/plans';
    savePlan(body, path, generate.dataset.planId ? 'PUT' : 'POST');
  } catch(error) { feedback.hidden = false; feedback.textContent = error.message; }
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
      feedback.textContent = '新的一天，已按上次确认的题量安排本地原题。今天的题单会保持不变。';
    }
  } catch (error) {
    feedback.hidden = false; feedback.textContent = error.message;
    const retry = document.createElement('button'); retry.textContent = '重新读取今日题单'; retry.onclick = restoreToday; feedback.append(retry);
  } finally { unlock(); }
}
restoreToday();
