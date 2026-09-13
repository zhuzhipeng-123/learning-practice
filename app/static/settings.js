const providerDefaults = JSON.parse(document.querySelector('#provider-defaults').textContent);
const globalProvider = document.querySelector('#global-provider');
globalProvider.value = document.querySelector('[data-module] [name="provider"]').value;
document.querySelector('#apply-provider').addEventListener('click', async () => {
  const provider = globalProvider.value, output = document.querySelector('#provider-status');
  const unlock = lockControls(document.querySelectorAll('[data-module] input,[data-module] select,[data-module] textarea,[data-module] button,#apply-provider,#global-provider'));
  try {
    await requestJSON('/api/model-provider', {method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'}, body:JSON.stringify({provider})});
    for (const form of document.querySelectorAll('[data-module]')) {
      const field = form.querySelector('[name="provider"]'); field.value = provider;
      field.dispatchEvent(new Event('change')); form.querySelector('[name="model"]').value = providerDefaults[provider];
    }
    output.textContent = '各环节的服务和模型已切换。提示词、输出长度的未保存修改仍保留在页面中。';
  } catch (error) { output.textContent = error.message; }
  finally { unlock(); }
});
for (const form of document.querySelectorAll('[data-module]')) {
  const providerInput = form.querySelector('[name="provider"]'), modelInput = form.querySelector('[name="model"]');
  const rememberedModels = {...providerDefaults};
  let previousProvider = providerInput.value;
  providerInput.addEventListener('change', () => {
    rememberedModels[previousProvider] = modelInput.value; previousProvider = providerInput.value;
    modelInput.value = rememberedModels[previousProvider];
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const output = form.querySelector('[role="status"]'), fields = new FormData(form);
    const unlock = lockControls([...form.querySelectorAll('input,textarea,select,button'), document.querySelector('#apply-provider')]);
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
