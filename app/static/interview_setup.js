(() => {
const form = document.querySelector('#interview-choice'), mode = document.querySelector('#interview-mode');
const direction = document.querySelector('#interview-direction'), focus = document.querySelector('#job-focus');
const suggest = document.querySelector('#suggest-directions');
const recentKey = 'learning-interview-directions';
let recent = learningStore.get(recentKey) || [];
bindDraft(direction, 'learning-interview-direction-draft'); bindDraft(focus, 'learning-interview-focus-draft');
mode.addEventListener('change', () => { document.querySelector('#random-directions').hidden = mode.value !== 'random'; });
async function request(action, restored) {
  const body = restored || {mode:action, direction:action === 'opening' ? direction.value : '', job_focus:focus.value, avoid:action === 'suggest' ? recent : []};
  const output = document.querySelector(action === 'suggest' ? '#directions-status' : '#interview-result');
  const key = `learning-interview-preparation-${action}`;
  const unlock = lockControls(action === 'suggest' ? [suggest] : [form.querySelector('[type="submit"]'), direction, focus]);
  output.textContent = action === 'suggest' ? '正在想几个不同的练习方向…' : '正在围绕你的方向准备第一问…';
  try {
    const data = await savedRequest(key, '/api/interviews/direction', body);
    if (action === 'opening') { location.href = `/interview/${data.session_id}`; return; }
    recent = [...recent, ...data.directions].slice(-12); learningStore.set(recentKey, recent);
    document.querySelector('#direction-options').replaceChildren(...data.directions.map(text => {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
      button.addEventListener('click', () => {
        if (direction.disabled) { output.textContent = '开场题正在使用已提交的方向，生成后再选择新方向。'; return; }
        direction.value = text; direction.dispatchEvent(new Event('input', {bubbles:true})); output.textContent = '已选方向，可以改写或开始面试。';
      });
      return button;
    }));
    suggest.textContent = '换一组方向'; output.textContent = '选择一个方向，或按自己的想法改写。';
  } catch (error) {
    output.textContent = error.message;
    if (error.pending) {
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '恢复上次请求';
      retry.onclick = () => request(action, error.pending.values); output.append(retry);
    }
  } finally { unlock(); }
}
suggest.addEventListener('click', () => request('suggest'));
form.addEventListener('submit', event => { event.preventDefault(); request('opening'); });
})();
for (const button of document.querySelectorAll('[data-interview-pool]')) {
  const output = document.createElement('p'); output.setAttribute('role','status'); button.after(output);
  async function draw(restored) {
    const unlock = lockControls([button]);
    const key = `learning-interview-pool-${button.dataset.interviewPool}`;
    try {
      const data = await savedRequest(key, '/api/interviews/pool', restored || {pool:button.dataset.interviewPool, job_focus:document.querySelector('#job-focus').value});
      if (data.message) {
        output.textContent = data.message;
        const link = document.createElement('a'); link.href = `/interview/${data.session_id}`; link.textContent = '开始这场面试 →'; output.append(link);
      } else location.href = `/interview/${data.session_id}`;
    } catch (error) {
      output.textContent = error.message;
      if (error.pending) {
        const retry = document.createElement('button'); retry.textContent = '重试上次提交';
        retry.onclick = () => draw(error.pending.values); output.append(retry);
      }
    } finally { unlock(); }
  }
  button.addEventListener('click', () => draw());
}
