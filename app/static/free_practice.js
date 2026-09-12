(() => {
const mode = document.querySelector('#free-mode');
const source = document.querySelector('#free-source');
const forms = [...document.querySelectorAll('.free-form')];
function resetResults() {
  forms.forEach(form => {
    const panel = form.closest('section');
    panel.querySelector('.free-result').hidden = true;
    panel.querySelector('.free-status').textContent = '选择已修改，点击抽题后显示新的题目。';
  });
}
function updateChoice() {
  document.querySelector('#theme-label').hidden = mode.value === 'random';
  document.querySelector('#generation-hint').hidden = source.value !== 'variant';
  forms.forEach(form => {
    const input = form.querySelector('.free-count'); input.max = source.value === 'variant' ? '3' : '30';
    if (Number(input.value) > Number(input.max)) input.value = input.max;
    form.querySelector('button').textContent = `${source.value === 'variant' ? '生成' : mode.value === 'random' ? '随机抽' : '按描述抽'}${form.dataset.kind === 'code' ? '代码' : '八股'}${source.value === 'variant' ? '变种题' : '题'}`;
  });
  resetResults();
}
mode.addEventListener('change', updateChoice);
source.addEventListener('change', updateChoice);
['#theme', '#only-new'].forEach(selector => document.querySelector(selector).addEventListener('input', resetResults));
for (const form of forms) {
  const panel = form.closest('section'), status = panel.querySelector('.free-status'), result = panel.querySelector('.free-result');
  let pending = null;
  form.addEventListener('input', () => { result.hidden = true; status.textContent = '数量已修改，点击抽题后显示新的题目。'; });
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!form.reportValidity()) return;
    const body = {question_source: source.value, mode: mode.value, theme: document.querySelector('#theme').value, count: Number(form.querySelector('.free-count').value), only_new: document.querySelector('#only-new').checked, question_type: form.dataset.kind};
    if (!pending || JSON.stringify(pending.body) !== JSON.stringify(body)) pending = {body, key: crypto.randomUUID()};
    const controls = document.querySelectorAll('main input, main textarea, main select, main button');
    controls.forEach(control => control.disabled = true);
    result.hidden = true; status.textContent = body.question_source === 'variant' ? '正在基于原题生成变种题，完成后显示题目…' : body.mode === 'random' ? '正在随机抽题…' : '正在理解你的范围并匹配题库…';
    try {
      const data = await readResponse(await fetch('/api/free-practice', {method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':pending.key}, body:JSON.stringify(body)}));
      result.replaceChildren();
      const label = body.question_type === 'code' ? '代码' : '八股';
      status.textContent = `本次抽到 ${data.added} 道${label}题${data.missing ? `，还缺 ${data.missing} 题。可调整范围或更新题库。` : '，选一题开始吧。'}`;
      for (const task of data.tasks) {
        const card = document.createElement('div'); card.className = 'task-row'; card.dataset.kind = task.question_type;
        const content = document.createElement('div'), title = document.createElement('h3'), module = document.createElement('p');
        title.textContent = `${task.is_variant ? '变种题 · ' : ''}${task.prompt.split('\n')[0]}`; module.className = 'muted'; module.textContent = task.category_path; content.append(title, module);
        const link = document.createElement('a'); link.className = 'button-link'; link.href = `/practice/${task.id}`; link.textContent = '开始练习 →';
        card.append(content, link); result.append(card);
      }
      result.hidden = false; pending = null;
    } catch (error) { status.textContent = error.message; }
    finally { controls.forEach(control => control.disabled = false); }
  });
}
})();
