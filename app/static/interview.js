(() => {
const root = document.querySelector('[data-session-id]');
const sessionId = root.dataset.sessionId;
const output = document.querySelector('#interview-output');
const input = document.querySelector('#interview-answer');
const mainQuestion = document.querySelector('[data-main-question]');
const mainQuestionFallback = mainQuestion.textContent;
requestJSON(`/api/questions/${root.dataset.questionId}/versions/${root.dataset.questionVersion}/prompt-materials`)
  .then(data => learningMaterials.render(mainQuestion, data.materials,
    blockId => `/api/questions/${root.dataset.questionId}/versions/${root.dataset.questionVersion}/materials/${encodeURIComponent(blockId)}`,
    mainQuestionFallback))
  .catch(() => { /* Keep the frozen plain-text question available. */ });
const draftStore = tabLearningStore;
const draftKey = `learning-interview-draft-${sessionId}`;
const draftRevisionKey = `${draftKey}-revision`;
let previousScope;
try { previousScope = sessionStorage.getItem('learning-interview-tab'); } catch { /* Memory fallback. */ }
const previousKey = previousScope ? `${draftKey}-${previousScope}` : draftKey;
if (draftStore.get(draftKey) === null && typeof learningStore.get(previousKey) === 'string') {
  draftStore.set(draftKey, learningStore.get(previousKey));
  draftStore.set(draftRevisionKey, learningStore.get(previousScope ? `${previousKey}-revision` : `learning-interview-draft-revision-${sessionId}`));
  learningStore.remove(previousKey);
}
const draft = bindDraft(input, draftKey, draftStore);
const pendingAnswer = learningStore.get(`learning-interview-${sessionId}-save`);
if (input && pendingAnswer && typeof draftStore.get(draftKey) !== 'string') input.value = pendingAnswer.values.content;
let referenceRevision = 0;
const referencePanel = document.querySelector('#interview-reference-panel');
function hideReference() { referenceRevision += 1; referencePanel.hidden = true; }
document.querySelector('#hide-interview-reference').onclick = hideReference;
async function showReference(turnId, generate=false) {
  const revision = ++referenceRevision;
  const status = document.querySelector('#interview-reference-status');
  const content = document.querySelector('#interview-reference-content');
  document.querySelector('.interview-toolbox').open = true;
  referencePanel.hidden = false; content.replaceChildren();
  status.textContent = generate ? '正在为原问题补充并核对答案…' : '正在读取这道题的参考答案…';
  try {
    const path = `/api/interviews/${sessionId}/reference${generate ? '/generate' : ''}`;
    const body = {turn_id:turnId || null};
    const data = generate ? await savedRequest(`learning-reference-${sessionId}-${turnId || 'main'}`, path, body)
      : await requestJSON(path, {method:'POST', headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'}, body:JSON.stringify(body)});
    if (revision !== referenceRevision) return;
    if (!data.available) {
      status.textContent = data.message;
      const button = document.createElement('button'); button.textContent = '为这道旧题补充参考答案';
      button.onclick = () => showReference(turnId, true); content.append(button); return;
    }
    status.textContent = data.model_generated ? '模型参考答案，已记录答案暴露；关键结论仍需核对。' : '来源参考答案，已记录答案暴露。';
    const title = document.createElement('h4'), answer = document.createElement('div');
    title.textContent = data.question;
    const rendered = learningMaterials.render(answer, data.materials,
      blockId => `/api/questions/${data.question_id}/versions/${data.question_version_id}/materials/${encodeURIComponent(blockId)}`,
      data.reference_text);
    if (!rendered) renderModelText(answer, data.reference_text);
    content.append(title, answer);
    if (data.reference_correction) {
      const correction = document.createElement('p'); correction.textContent = '本版本参考校正：' + data.reference_correction.content;
      content.append(correction);
      for (const url of data.reference_correction.sources) {
        const link = document.createElement('a'); link.href = url; link.textContent = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        content.append(document.createElement('br'), link);
      }
    }
  } catch (error) {
    if (revision !== referenceRevision) return;
    status.textContent = error.message;
    const retry = document.createElement('button'); retry.textContent = '重试读取或补充答案';
    retry.onclick = () => showReference(turnId, generate); content.append(retry);
  }
}
root.addEventListener('click', event => {
  const button = event.target.closest('[data-reference-turn]');
  if (button) showReference(button.dataset.referenceTurn);
});
function failed(error, key, after, target=output, controls=[]) {
  target.textContent = error.message;
  if (!error.pending) return;
  const retry = document.createElement('button'); retry.textContent = '重试上次提交';
  retry.onclick = async () => {
    const unlock = lockControls([retry, ...controls]);
    try { const data = await savedRequest(key, error.pending.path, error.pending.values, error.pending.method || 'POST'); after(data, error.pending.values); }
    catch (failure) { failed(failure, key, after, target, controls); }
    finally { unlock(); }
  };
  target.append(retry);
}
let observedRevision = null, lastRole = null, loadingRevision = 0, busy = false;
let continuation = null, conversationTimer = null, pollCount = 0;
const followupKey = `learning-interview-${sessionId}-followup`;
function scheduleConversation() {
  clearTimeout(conversationTimer);
  if (!active() || document.hidden || pollCount >= 40) return;
  if (learningStore.get(followupKey) || ['running','pending'].includes(continuation?.status)) {
    conversationTimer = setTimeout(() => {
      pollCount += 1;
      loadConversation().catch(() => scheduleConversation());
    }, 3000);
  }
}
const active = () => root.dataset.sessionStatus === 'active';
input?.addEventListener('input', () => {
  if (!input.value.trim()) draftStore.remove(draftRevisionKey);
  else if (typeof draftStore.get(draftRevisionKey) !== 'string' && observedRevision !== null) draftStore.set(draftRevisionKey, observedRevision);
});
function syncControls() {
  if (busy) return;
  const outstanding = Boolean(learningStore.get(followupKey)) || ['running','pending'].includes(continuation?.status);
  for (const button of root.querySelectorAll('[data-interview-action="save"],[data-interview-action="send"]')) button.disabled = !active() || observedRevision === null || outstanding;
  const followup = root.querySelector('[data-interview-action="followup"]');
  if (followup) followup.disabled = !active() || observedRevision === null || ['running','pending'].includes(continuation?.status) || (lastRole !== 'user' && !learningStore.get(followupKey));
}
function showDialogue(data) {
  document.querySelector('#saved-dialogue').replaceChildren(...data.turns.map(turn => {
    const section = document.createElement('section'), title = document.createElement('h4');
    section.className = `interview-turn interview-turn-${turn.role}`; section.dataset.turnId = turn.id;
    const content = document.createElement('div'); content.className = 'prose';
    title.textContent = turn.role === 'user' ? '我' : turn.is_feedback ? '面试总结' : '面试官';
    if (turn.role === 'user') content.textContent = turn.content;
    else renderModelText(content, turn.content);
    section.append(title, content);
    if (turn.role === 'assistant' && !turn.is_feedback) {
      const tools = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = '这次问题的参考与复习'; tools.append(summary);
      const review = document.createElement('button'); review.textContent = '这次追问不会，整理成复习题';
      review.dataset.reviewTurn = turn.id; review.onclick = () => prepareReview(turn, review);
      const reference = document.createElement('button'); reference.textContent = '查看这次追问的参考答案';
      reference.dataset.referenceTurn = turn.id; tools.append(reference, review); section.append(tools);
    }
    return section;
  }));
  document.querySelector('#interview-turn-count').textContent = data.turns.length;
}
async function loadConversation(confirmDraft=false) {
  const revision = ++loadingRevision;
  const status = document.querySelector('#interview-dialogue-status');
  if (observedRevision === null || confirmDraft) status.textContent = '正在读取已保存对话…';
  const pending = learningStore.get(followupKey);
  const query = pending?.values.expected_revision ? `?pending_revision=${encodeURIComponent(pending.values.expected_revision)}` : '';
  let data;
  try { data = await requestJSON(`/api/interviews/${sessionId}/conversation${query}`); }
  catch (error) { if (revision === loadingRevision) { observedRevision = null; syncControls(); } throw error; }
  if (revision !== loadingRevision) return;
  observedRevision = data.revision; lastRole = data.turns.at(-1)?.role || null;
  continuation = data.continuation;
  if (pending && continuation?.status === 'complete' && continuation.answer_revision === pending.values.expected_revision
      && data.turns.some(turn => turn.id === continuation.result_id) && learningStore.get(followupKey)?.key === pending.key) {
    learningStore.remove(followupKey);
    output.textContent = '面试官已回复，已自动恢复这一轮，可以继续回答。';
  }
  showDialogue(data);
  if (confirmDraft && input?.value.trim()) draftStore.set(draftRevisionKey, observedRevision);
  status.textContent = confirmDraft && input?.value.trim() ? '对话已刷新，草稿保留。请核对当前问题，再发送。' : data.turns.length ? '已恢复连续对话，参考答案仍默认隐藏。' : '面试官已提出第一问，写下你的思路开始吧。';
  if (['running','pending'].includes(continuation?.status)) status.textContent = '面试官正在生成并核对下一问，完成后自动显示。可以离开再回来，当前回答已保存。';
  else if (['failed','expired'].includes(continuation?.status)) status.textContent = '这一轮模型生成未完成，回答已保存。点击“继续面试”按原请求重试。';
  syncControls();
  scheduleConversation();
}
document.querySelector('#refresh-conversation')?.addEventListener('click', async event => {
  const unlock = lockControls([event.target]);
  try { await loadConversation(true); }
  catch (error) { document.querySelector('#interview-dialogue-status').textContent = error.message; }
  finally { unlock(); }
});
async function perform(action, restored=null) {
  if (busy) return;
  const chain = action === 'send';
  let step = chain ? 'save' : action;
  const target = document.querySelector(step === 'feedback' ? '#interview-feedback-output' : step === 'dialogue' ? '#interview-dialogue-status' : '#interview-output');
  if (step === 'save' && !(restored?.content ?? input.value).trim()) { output.textContent = '先填写回答或想继续聊的内容。'; return; }
  if (['end','followup','feedback'].includes(step) && input?.value.trim() && !restored) { output.textContent = '这段内容还未保存，请先发送或保存回答。'; return; }
  if (['save','followup'].includes(step) && observedRevision === null && !restored) { output.textContent = '请先读取对话，再继续。'; return; }
  const controls = [...root.querySelectorAll('[data-interview-action],#interview-answer,#refresh-conversation')];
  busy = true;
  pollCount = 0;
  const unlock = lockControls(controls);
  let key = `learning-interview-${sessionId}-${step}`;
  const old = learningStore.get(key);
  let body = restored || (step === 'save' ? {role:'user', content:input.value, created_at:old?.values.created_at || new Date().toISOString(),
    expected_revision:typeof draftStore.get(draftRevisionKey) === 'string' ? draftStore.get(draftRevisionKey) : observedRevision}
    : step === 'end' ? {ended_at:old?.values.ended_at || new Date().toISOString()}
    : step === 'followup' ? {expected_revision:observedRevision} : {});
  let saved = false;
  async function requestStep() {
    const path = step === 'save' ? 'turns' : ['end','dialogue'].includes(step) ? step : `generate/${step}`;
    target.textContent = step === 'save' ? '正在保存你的回答…' : step === 'followup' ? '回答已保存，面试官正在思考下一问…' : '处理中…';
    const response = savedRequest(key, `/api/interviews/${sessionId}/${path}`, body);
    if (step === 'followup') scheduleConversation();
    return response;
  }
  try {
    let data = await requestStep();
    if (step === 'save') {
      saved = true;
      draft.clear(body.content);
      if (input.value === body.content) { input.value = ''; draftStore.remove(draftRevisionKey); }
      await loadConversation();
      if (chain && !input.value.trim()) {
        step = 'followup'; key = `learning-interview-${sessionId}-followup`; body = {expected_revision:data.turn_id};
        data = await requestStep();
      } else target.textContent = '回答已保存。可以继续补充，也可以点击“继续面试”。';
    }
    if (step === 'followup') {
      hideReference(); await loadConversation();
      target.textContent = '面试官已回复。你可以回答，也可以说明哪里不懂或想换个相关方向。';
    } else if (step === 'feedback') renderModelText(target, data.response_text);
    else if (step === 'dialogue') { showDialogue(data); target.textContent = '完整对话已加载，已记录答案暴露。'; }
    else if (step === 'end') {
      root.dataset.sessionStatus = 'ended'; document.querySelector('#active-interview').hidden = true;
      document.querySelector('#refresh-conversation').hidden = true;
      document.querySelector('#interview-state').textContent = '会话状态：已结束';
      document.querySelector('.interview-toolbox').open = true;
      target.textContent = '面试已结束，可以生成总结或回看这场对话。';
    }
  } catch (error) {
    target.textContent = `${saved || step === 'followup' ? '已保存的回答仍在。' : ''}${error.message}`;
    if (error.pending) {
      const retry = document.createElement('button'); retry.textContent = '恢复上次操作';
      retry.onclick = () => perform(step === 'save' && chain ? 'send' : step, error.pending.values); target.append(retry);
    } else if (step === 'followup') {
      // A completed model failure still needs the original request key/configuration on retry.
      const retry = document.createElement('button'); retry.textContent = '重试这一轮';
      retry.onclick = () => perform('followup', body); target.append(retry);
    }
  } finally { busy = false; unlock(); syncControls(); scheduleConversation(); }
}
for (const button of root.querySelectorAll('[data-interview-action]')) button.addEventListener('click', () => perform(button.dataset.interviewAction));
input?.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) {
    event.preventDefault(); root.querySelector('[data-interview-action="send"]').click();
  }
});
if (active()) {
  loadConversation().then(() => {
    for (const action of ['save','followup']) {
      const pending = learningStore.get(`learning-interview-${sessionId}-${action}`);
      if (!pending) continue;
      const notice = document.createElement('p'); notice.textContent = '上次操作的结果还未确认，恢复后再继续。';
      const retry = document.createElement('button'); retry.textContent = action === 'save' ? '恢复上次回答' : '恢复上次追问';
      retry.onclick = () => { notice.remove(); perform(action, pending.values); }; notice.append(retry); output.append(notice);
    }
  }).catch(error => { document.querySelector('#interview-dialogue-status').textContent = error.message + '。点击刷新对话重试。'; });
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && active()) { pollCount = 0; loadConversation().catch(() => scheduleConversation()); }
});

let selectedTurn = null, selectedKnowledge = null, previewRevision = 0, confirming = false;
const editor = document.querySelector('#derived-editor');
for (const field of editor.querySelectorAll('textarea')) field.addEventListener('input', () => { document.querySelector('#derived-verified').checked = false; });
async function prepareReview(turn, button) {
  if (confirming) return;
  const revision = ++previewRevision;
  selectedTurn = turn.id; selectedKnowledge = null; editor.hidden = false;
  const unlockButton = lockControls([button]);
  const fields = editor.querySelectorAll('input,textarea');
  fields.forEach(field => field.disabled = true);
  document.querySelector('#derived-prompt').value = `${root.querySelector('[data-main-question]').textContent}\n\n追问：${turn.content}`;
  document.querySelector('#derived-reference').value = '';
  document.querySelector('#derived-verified').checked = false;
  const status = document.querySelector('#derived-status'); status.textContent = '正在整理题干和参考要点…';
  const confirm = document.querySelector('#confirm-derived'); confirm.disabled = true;
  try {
    const data = await savedRequest(`learning-preview-${sessionId}-${turn.id}`, `/api/interviews/${sessionId}/review-preview`, {turn_id:turn.id});
    if (revision !== previewRevision) return;
    document.querySelector('#derived-prompt').value = data.prompt;
    document.querySelector('#derived-reference').value = data.reference_text;
    document.querySelector('#derived-category').value = data.category_path;
    document.querySelector('#derived-verified').checked = data.reference_verified;
    selectedKnowledge = data.question_id ? data : null;
    confirm.textContent = selectedKnowledge ? '保存修改为新版本' : '确认加入复习';
    status.textContent = selectedKnowledge ? '已读取当前保存版本，可以修改后保存；旧作答保留原依据。' : '整理草稿已生成，请核对后确认。';
  } catch (error) { if (revision === previewRevision) status.textContent = `自动整理未完成：${error.message}。可以直接在上面填写参考要点，或再次点击整理。`; }
  finally {
    unlockButton();
    if (revision === previewRevision) { confirm.disabled = false; fields.forEach(field => field.disabled = false); editor.scrollIntoView({block:'nearest'}); }
  }
}
document.querySelector('#cancel-derived').onclick = () => {
  if (confirming) return;
  previewRevision += 1; selectedTurn = null; editor.hidden = true;
  editor.querySelectorAll('input,textarea').forEach(field => field.disabled = false);
};
document.querySelector('#confirm-derived').onclick = async event => {
  if (!selectedTurn || confirming) return;
  const status = document.querySelector('#derived-status'), button = event.target;
  const key = `learning-derived-${sessionId}-${selectedTurn}`;
  const old = learningStore.get(key);
  const body = {prompt:document.querySelector('#derived-prompt').value, reference_text:document.querySelector('#derived-reference').value,
    category_path:document.querySelector('#derived-category').value, confirmed_by_user:true, turn_id:selectedTurn,
    reference_verified:document.querySelector('#derived-verified').checked, created_at:old?.values.created_at || new Date().toISOString()};
  if (!body.prompt.trim() || !body.reference_text.trim()) { status.textContent = '请补全题干和参考要点，再确认。'; return; }
  if (selectedKnowledge) body.expected_version_id = selectedKnowledge.version_id;
  confirming = true;
  status.textContent = '正在保存题目与答案…';
  const unlock = lockControls([button, ...editor.querySelectorAll('input,textarea,#cancel-derived'), ...root.querySelectorAll('[data-review-turn]')]);
  const after = data => {
    selectedKnowledge = data; button.textContent = '保存修改为新版本';
    status.textContent = data.current_version_id && data.current_version_id !== data.version_id
      ? '上次保存已恢复，但题库已有更新版本。请重新打开编辑器；当前草稿仍保留。'
      : '题目已保存到复习库，旧作答保留原版本。';
    const link = document.createElement('a'); link.href = `/questions/${data.question_id}/history`; link.textContent = '查看这道题 →';
    status.append(link);
  };
  try {
    const path = selectedKnowledge ? `/api/questions/${selectedKnowledge.question_id}/knowledge` : `/api/interviews/${sessionId}/derived`;
    after(await savedRequest(key, path, body, selectedKnowledge ? 'PUT' : 'POST'));
  } catch (error) { failed(error, key, after, status); }
  finally { confirming = false; unlock(); }
};
document.querySelector('#review-main-question')?.addEventListener('click', async event => {
  const output = document.querySelector('#main-review-output');
  const button = event.target; button.disabled = true;
  try {
    await requestJSON(`/api/interviews/${sessionId}/review-main`, {method:'POST',
      headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice'},
      body:JSON.stringify({entered_by:'interview', happened_at:new Date().toISOString()})});
    output.textContent = '主问题已加入复习，沿用原题记录。';
  } catch (error) { output.textContent = error.message; }
  finally { button.disabled = false; }
});
})();
for (const button of document.querySelectorAll('[data-interview-code]')) {
  const controls = [...document.querySelectorAll('[data-interview-code]')];
  async function assess(restored) {
    const id = document.querySelector('[data-session-id]').dataset.sessionId;
    const output = document.querySelector('#interview-code-output');
    const unlock = lockControls(controls);
    let complete = false;
    try {
      const data = await savedRequest(`learning-interview-code-${id}`, `/api/interviews/${id}/code-assessment`, restored || {result:button.dataset.interviewCode});
      output.textContent = data.result === 'can_solve' ? '已记录会做；这次面试不增加独立复习次数。' : data.review_basis_conflict ? '已记录不会做。复习库保留另一个版本，请到复习库核对。' : '已记录不会做，并加入复习库。';
      complete = true;
    } catch (error) {
      output.textContent = error.message;
      if (error.pending) {
        const retry = document.createElement('button'); retry.textContent = '重试上次提交';
        retry.onclick = () => assess(error.pending.values); output.append(retry);
      }
    } finally { unlock(); if (complete) controls.forEach(control => control.disabled = true); }
  }
  button.addEventListener('click', () => assess());
}
