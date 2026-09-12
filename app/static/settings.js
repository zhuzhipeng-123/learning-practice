document.querySelector('#global-provider').value=document.querySelector('[data-module] [name="provider"]').value;
document.querySelector('#apply-provider').addEventListener('click',async event=>{event.target.disabled=true;try{await readResponse(await fetch('/api/model-provider',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'},body:JSON.stringify({provider:document.querySelector('#global-provider').value})}));location.reload();}catch(error){document.querySelector('#provider-status').textContent=error.message;}finally{event.target.disabled=false;}});
for (const form of document.querySelectorAll('[data-module]')) {
  const providerInput = form.querySelector('[name="provider"]');
  const modelInput = form.querySelector('[name="model"]');
  const rememberedModels = {agnes:'agnes-2.5-flash',openrouter:'nex-agi/nex-n2.5-mini:free'};
  let previousProvider = providerInput.value;
  providerInput.addEventListener('change', () => {
    rememberedModels[previousProvider] = modelInput.value;
    previousProvider = providerInput.value;
    modelInput.value = rememberedModels[previousProvider];
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    const output = form.querySelector('[role="status"]');
    button.disabled = true;
    try {
      const fields = new FormData(form);
      const body = Object.fromEntries(fields);
      body.max_tokens = Number(body.max_tokens);
      const response = await fetch(`/api/model-settings/${form.dataset.module}`, {
        method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'}, body:JSON.stringify(body)
      });
      const data = await readResponse(response);
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请检查输入');
      output.textContent = '已保存，新请求将使用这份配置。';
    } catch (error) { output.textContent = error.message; }
    finally { button.disabled = false; }
  });
}
