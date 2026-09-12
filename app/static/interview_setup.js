(() => {
const form = document.querySelector('#interview-choice');
const mode = document.querySelector('#interview-mode');
const direction = document.querySelector('#interview-direction');
const output = document.querySelector('#interview-result');
const suggest = document.querySelector('#suggest-directions');
let recent = [], pending = null;
mode.addEventListener('change', () => {
  document.querySelector('#random-directions').hidden = mode.value !== 'random';
});
async function request(action) {
  const body = {mode: action, direction: action === 'opening' ? direction.value : '', job_focus: document.querySelector('#job-focus').value, avoid: action === 'suggest' ? recent : []};
  if (!pending || JSON.stringify(pending.body) !== JSON.stringify(body)) pending = {body, key: crypto.randomUUID()};
  const buttons = form.querySelectorAll('button, input, textarea, select');
  buttons.forEach(button => button.disabled = true);
  output.textContent = action === 'suggest' ? '正在想几个不同的练习方向…' : '正在围绕你的方向准备第一问…';
  try {
    const data = await readResponse(await fetch('/api/interviews/direction', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'learning-practice', 'Idempotency-Key': pending.key}, body: JSON.stringify(body)}));
    pending = null;
    if (action === 'opening') { location.href = `/interview/${data.session_id}`; return; }
    recent = [...recent, ...data.directions].slice(-12);
    document.querySelector('#direction-options').replaceChildren(...data.directions.map(text => {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = text;
      button.addEventListener('click', () => { direction.value = text; direction.dispatchEvent(new Event('input', {bubbles: true})); output.textContent = '已选方向，可以改写或开始面试。'; });
      return button;
    }));
    suggest.textContent = '换一组方向';
    output.textContent = '选择一个方向，或按自己的想法改写。';
  } catch (error) { output.textContent = error.message; }
  finally { buttons.forEach(button => button.disabled = false); }
}
suggest.addEventListener('click', () => request('suggest'));
form.addEventListener('submit', event => { event.preventDefault(); request('opening'); });
})();
