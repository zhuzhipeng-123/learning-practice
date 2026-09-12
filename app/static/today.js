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
const originalSelection = selectionState();
const readyKey = `learning-plan-ready-${generate.dataset.date}`;
const readyValue = JSON.stringify([generate.dataset.planId, originalSelection]);
let ready = sessionStorage.getItem(readyKey) === readyValue;
function showSavedTasks() {
  const changed = selectionState() !== originalSelection;
  document.querySelector('#unapplied-selection').hidden = ready && !changed;
  document.querySelector('#unapplied-selection').textContent = changed
    ? '选择已修改。保存后显示对应题单。'
    : '先选好题量，再点击保存，下面会显示代码和八股题目。';
  document.querySelector('#daily-task-list').hidden = !ready || changed;
  document.querySelector('#saved-task-count').hidden = !ready || changed;
}
choices.forEach(input => input.addEventListener('input', () => {
  showSavedTasks();
  feedback.hidden = true;
}));
generate.addEventListener('click', async () => {
  generate.disabled = true;
  feedback.hidden = false;
  feedback.textContent = '正在从本地题库抽取…';
  try {
    const code = document.querySelector('#code-target'), theory = document.querySelector('#theory-target');
    if (!code.checkValidity() || !theory.checkValidity()) throw Error('题量必须是 0–100 的整数。');
    const body = {plan_date:generate.dataset.date,code_target:Number(code.value),theory_target:Number(theory.value),module_quotas:quotas()};
    if (Object.values(body.module_quotas).reduce((a,b)=>a+b,0) > body.theory_target) throw Error('模块题数之和超过八股总量。');
    choices.forEach(input => input.disabled = true);
    const keyName = `learning-plan-${body.plan_date}`;
    let pending = JSON.parse(sessionStorage.getItem(keyName) || 'null');
    if (!pending || JSON.stringify(pending.body) !== JSON.stringify(body)) pending = {key:crypto.randomUUID(),body};
    sessionStorage.setItem(keyName,JSON.stringify(pending));
    const path = generate.dataset.planId ? `/api/plans/${generate.dataset.planId}` : '/api/plans';
    const data = await readResponse(await fetch(path,{method:generate.dataset.planId ? 'PUT' : 'POST',headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':pending.key},body:JSON.stringify(body)}));
    sessionStorage.removeItem(keyName);
    const shortages = Object.entries(data.shortages || {}).map(([kind,n])=>`${kind==='code'?'代码':kind==='unallocated'?'八股总计':kind}缺 ${n} 题`).join('；');
    sessionStorage.setItem('learning-plan-notice',shortages ? `任务已保存。${shortages}。可调整数量或范围，或更新题库后再次保存。` : `已按选择保存：代码 ${body.code_target} 题、八股 ${body.theory_target} 题。下方显示今日题单。`);
    location.reload();
  } catch(error) { feedback.textContent = error.message; }
  finally { generate.disabled = false; choices.forEach(input => input.disabled = false); }
});
const notice = sessionStorage.getItem('learning-plan-notice');
if (notice) {
  feedback.hidden=false;feedback.textContent=notice;sessionStorage.removeItem('learning-plan-notice');
  ready=true;sessionStorage.setItem(readyKey,readyValue);
}
showSavedTasks();
