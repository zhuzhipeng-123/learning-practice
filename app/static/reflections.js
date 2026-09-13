(() => {
const day = document.querySelector('#reflection-date').value;
const input = document.querySelector('#personal-reflection');
const draft = bindDraft(input, `learning-reflection-draft-${day}`);
for (const id of ['save-reflection', 'generate-reflection']) {
  const button = document.getElementById(id), generate = id === 'generate-reflection';
  const output = document.querySelector(generate ? '#model-reflection-output' : '#personal-reflection-output');
  const content = document.querySelector('#model-reflection-content');
  const key = `learning-${id}-${day}`;
  async function send(body, path) {
    const unlock = lockControls([button]);
    const token = crypto.randomUUID();
    if (generate) content.dataset.renderRequest = token;
    const show = data => {
      if (generate) {
        if (data.stale) {
          output.textContent = '这份复盘基于较早的记录，已保留到历史；有新的学习事实，请更新复盘。';
          return;
        }
        if (content.dataset.renderRequest === token) renderModelText(content, data.content);
        output.textContent = '学习复盘已保存。'; button.textContent = '更新学习复盘';
        const view = document.querySelector('#daily-reflection [data-view-reflection]');
        if (view) view.dataset.viewReflection = data.result_id;
      } else {
        input.dataset.savedId = data.reflection_id;
        draft.clear(body.content);
        output.textContent = input.value === body.content ? '我的复盘已保存。' : '刚才提交的内容已保存；新输入仍是草稿，可以继续保存。';
      }
    };
    try {
      output.textContent = generate ? '正在生成复盘…你可以继续写自己的复盘或安排练习。' : '保存中…';
      show(await savedRequest(key, path, body));
    } catch (error) {
      output.textContent = error.message;
      if (error.pending) {
        const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '重试上次提交';
        retry.onclick = () => send(error.pending.values, error.pending.path);
        output.append(retry);
      }
    } finally { unlock(); }
  }
  button.addEventListener('click', () => {
    const pending = learningStore.get(key), body = {activity_date:day};
    if (!generate) Object.assign(body, {content:input.value, expected_id:input.dataset.savedId || '', created_at:pending?.values.created_at || new Date().toISOString()});
    if (!generate && !body.content.trim()) { output.textContent = '先写几句复盘，再保存。'; return; }
    send(body, generate ? '/api/reflections/generate' : '/api/reflections/user');
  });
}
})();
