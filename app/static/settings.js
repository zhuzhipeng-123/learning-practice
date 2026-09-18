for (const form of document.querySelectorAll('[data-module]')) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const output = form.querySelector('[role="status"]'), fields = new FormData(form);
    const unlock = lockControls(form.querySelectorAll('input,textarea,button'));
    try {
      const body = Object.fromEntries(fields); body.max_tokens = Number(body.max_tokens);
      await requestJSON(`/api/model-settings/${form.dataset.module}`, {
        method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'}, body:JSON.stringify(body)
      });
      output.textContent = '已保存，新请求将使用这份配置。';
    } catch (error) { output.textContent = error.message; }
    finally { unlock(); }
  });
}
