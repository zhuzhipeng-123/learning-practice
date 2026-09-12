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
generate.addEventListener('click', async () => {
  generate.disabled = true;
  feedback.hidden = false;
  feedback.textContent = '正在从本地题库抽取…';
  try {
    const code = document.querySelector('#code-target'), theory = document.querySelector('#theory-target');
    if (!code.checkValidity() || !theory.checkValidity()) throw Error('题量必须是 0–100 的整数。');
    const body = {plan_date:generate.dataset.date,code_target:Number(code.value),theory_target:Number(theory.value),module_quotas:quotas()};
    if (Object.values(body.module_quotas).reduce((a,b)=>a+b,0) > body.theory_target) throw Error('模块题数之和超过八股总量。');
    const keyName = `learning-plan-${body.plan_date}`;
    let pending = JSON.parse(sessionStorage.getItem(keyName) || 'null');
    if (!pending || JSON.stringify(pending.body) !== JSON.stringify(body)) pending = {key:crypto.randomUUID(),body};
    sessionStorage.setItem(keyName,JSON.stringify(pending));
    const path = generate.dataset.planId ? `/api/plans/${generate.dataset.planId}` : '/api/plans';
    const data = await readResponse(await fetch(path,{method:generate.dataset.planId ? 'PUT' : 'POST',headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':pending.key},body:JSON.stringify(body)}));
    sessionStorage.removeItem(keyName);
    const shortages = Object.entries(data.shortages || {}).map(([kind,n])=>`${kind==='code'?'代码':kind==='unallocated'?'八股总计':kind}缺 ${n} 题`).join('；');
    sessionStorage.setItem('learning-plan-notice',shortages ? `任务已保存。${shortages}。题源更新或确认后，可点击补齐。` : '任务已保存，可以开始练习。');
    location.reload();
  } catch(error) { feedback.textContent = error.message; }
  finally { generate.disabled = false; }
});
const notice = sessionStorage.getItem('learning-plan-notice');
if (notice) { feedback.hidden=false;feedback.textContent=notice;sessionStorage.removeItem('learning-plan-notice'); }
