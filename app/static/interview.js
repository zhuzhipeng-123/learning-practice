const sessionId = document.querySelector('[data-session-id]').dataset.sessionId;
for (const button of document.querySelectorAll('[data-interview-action]')) {
  button.addEventListener('click', async () => {
    const output = document.querySelector('#interview-output');
    const buttons = document.querySelectorAll('[data-interview-action]');
    buttons.forEach(item => item.disabled = true);
    const action = button.dataset.interviewAction;
    try {
      const now = new Date().toISOString();
      const path = action === 'save' ? 'turns' : action === 'end' ? 'end' : action === 'dialogue' ? 'dialogue' : `generate/${action}`;
      const body = action === 'save' ? {role:'user',content:document.querySelector('#interview-answer').value,created_at:now}
        : action === 'end' ? {ended_at:now} : {};
      button.dataset.requestKey ||= crypto.randomUUID();
      output.textContent = action === 'save' ? '保存中…' : '处理中，已保存的回答不会丢失…';
      const response = await fetch(`/api/interviews/${sessionId}/${path}`, {method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':button.dataset.requestKey},body:JSON.stringify(body)});
      const data = await readResponse(response);
      if (!response.ok) throw new Error(data.detail || '请求失败');
      if (action === 'dialogue') {
        document.querySelector('#saved-dialogue').replaceChildren(...data.turns.map(turn => {
          const item = document.createElement('section'), title = document.createElement('h4');
          const content = document.createElement('div'); content.className = 'prose';
          title.textContent = turn.role === 'user' ? '我' : '面试官';
          if (turn.role === 'user') content.textContent = turn.content;
          else renderModelText(content, turn.content);
          item.append(title, content);
          return item;
        }));
        output.textContent = '已加载对话，并记录本次查看。';
        return;
      }
      if (action === 'feedback') {
        renderModelText(output, data.response_text);
        return;
      }
      location.reload();
    } catch (error) { output.textContent = error.message; }
    finally { buttons.forEach(item => item.disabled = false); }
  });
}
