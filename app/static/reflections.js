for (const id of ['save-reflection', 'generate-reflection']) {
  const button = document.getElementById(id);
  button.addEventListener('click', async () => {
    const output = document.querySelector('#reflection-output');
    const activityDate = document.querySelector('#reflection-date').value;
    const generate = id === 'generate-reflection';
    button.disabled = true;
    try {
      if (button.dataset.requestDate !== activityDate) {
        button.dataset.requestDate = activityDate;
        button.dataset.requestKey = crypto.randomUUID();
      }
      output.textContent = generate ? '正在生成反思…' : '保存中…';
      const body = {activity_date:activityDate};
      if (!generate) Object.assign(body,{content:document.querySelector('#personal-reflection').value,created_at:new Date().toISOString()});
      const response = await fetch(generate ? '/api/reflections/generate' : '/api/reflections/user', {
        method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':button.dataset.requestKey},body:JSON.stringify(body)
      });
      const data = await readResponse(response);
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请检查日期和内容');
      if (generate) {
        renderModelText(document.querySelector('#model-reflection-content'),data.content);
        output.textContent='错题复盘已保存。';
        button.textContent='更新错题复盘';
        delete button.dataset.requestDate;
        const view=document.querySelector('#daily-reflection [data-view-reflection]');
        if(view)view.dataset.viewReflection=data.result_id;
      } else location.reload();
    } catch (error) { output.textContent = error.message; }
    finally { button.disabled = false; }
  });
}
