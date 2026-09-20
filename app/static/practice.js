const card = document.querySelector("[data-task-id]");
const result = document.querySelector("#result");
const taskId = card.dataset.taskId;
const storageKey = `learning-submit-${taskId}`;
const questionText = document.querySelector('#question-content');
const questionFallback = questionText.textContent;
if (card.dataset.variant === 'true') renderModelText(questionText, questionFallback);
requestJSON(`/api/questions/${card.dataset.questionId}/versions/${card.dataset.questionVersion}/prompt-materials`)
  .then(data => learningMaterials.render(questionText, data.materials,
    blockId => `/api/tasks/${taskId}/materials/${encodeURIComponent(blockId)}`, questionFallback))
  .catch(() => { /* The frozen plain-text prompt remains usable. */ });
const draft = bindDraft(document.querySelector('#answer,#note'), `learning-draft-${taskId}`);
// A recovered older submission must not erase edits made after its response was lost.
const remainingDraft = learningStore.get(`learning-draft-${taskId}`);
if (card.dataset.taskStatus === 'completed' && typeof remainingDraft === 'string' && remainingDraft.trim()) {
  const section = document.createElement('section'), label = document.createElement('label');
  const text = document.createElement('textarea'), clear = document.createElement('button');
  section.className = 'panel'; section.id = 'remaining-draft';
  label.textContent = '提交后修改的草稿（尚未保存到作答，可复制另存）';
  text.value = remainingDraft; text.readOnly = true; text.rows = 5;
  clear.textContent = '我已另存，清除这份草稿';
  clear.onclick = () => { learningStore.remove(`learning-draft-${taskId}`); section.remove(); };
  label.append(text); section.append(label, clear); result.before(section);
}
let starting;
try {
  const legacy = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
  const legacyValues = legacy?.values || legacy?.body;
  if (legacyValues && (legacyValues.answer_text || legacyValues.code_self_result)) {
    learningStore.set(storageKey, {path:legacy.path, values:legacyValues, key:legacy.key});
  }
  sessionStorage.removeItem(storageKey);
  const pending = learningStore.get(storageKey);
  if (pending && card.dataset.taskStatus !== "completed" && typeof remainingDraft !== 'string') {
    const input = document.querySelector("#answer,#note");
    if (input) input.value = pending.values.answer_text ?? pending.values.note ?? "";
  }
} catch { show("无法恢复上次未确认的提交，请检查浏览器存储设置。"); }

function show(value) {
  result.hidden = false;
  result.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

async function request(path, body, key) {
  const headers = {"Content-Type": "application/json", "X-Requested-With": "learning-practice"};
  if (key) headers["Idempotency-Key"] = key;
  return requestJSON(path, {method: "POST", headers, body: JSON.stringify(body)});
}

async function ensureStarted() {
  if (card.dataset.taskStatus === "completed") return;
  if (!starting) starting = request(`/api/tasks/${taskId}/attempts`, {
    entry_mode: "web", started_at: new Date().toISOString()
  }).catch(error => { starting = null; throw error; });
  await starting;
}

// Opening an unstarted practice freezes its version; explicit redraws can retire it.
if (card.dataset.taskStatus === 'pending') ensureStarted().catch(error => show(error.message));

async function submit(path, values, preserveDraft=false) {
  if ('answer_text' in values && !values.answer_text.trim()) {
    show('请先填写回答，再保存。');
    return;
  }
  const buttons = document.querySelectorAll("[data-code-result],#submit-theory");
  const unlock = lockControls([...buttons, document.querySelector('#answer,#note')]);
  try {
    await ensureStarted();
    const old = learningStore.get(storageKey);
    await savedRequest(storageKey, path, {...values, entry_mode:'web',
      submitted_at:old?.values.submitted_at || new Date().toISOString()});
    if (!preserveDraft) draft.clear(values.answer_text ?? values.note);
    location.reload();
  } catch (error) {
    show(error.definitelyRejected ? `没有保存：${error.message}` : `提交未确认：${error.message}`);
    if (error.pending) {
      const retry = document.createElement('button'); retry.textContent = '重试上次提交';
      retry.onclick = async () => {
        retry.disabled = true;
        try {
          await savedRequest(storageKey, error.pending.path, error.pending.values);
          if (!preserveDraft) draft.clear(error.pending.values.answer_text ?? error.pending.values.note);
          location.reload();
        }
        catch (failure) { show(failure.message); }
        finally { retry.disabled = false; }
      };
      result.append(document.createElement('br'), retry);
    }
  } finally {
    unlock();
  }
}

for (const button of document.querySelectorAll("[data-code-result]")) {
  button.addEventListener("click", () => {
    const level = document.querySelector('#mastery-level')?.value || null;
    if (button.dataset.codeResult === 'cannot_solve' && !level) { show('请先选择当前掌握程度。'); return; }
    submit(`/api/tasks/${taskId}/code-submit`, {
      code_self_result: button.dataset.codeResult, note: document.querySelector("#note").value,
      mastery_level: button.dataset.codeResult === 'cannot_solve' ? level : null
    });
  });
}
document.querySelector("#submit-theory")?.addEventListener("click", () => submit(
  `/api/tasks/${taskId}/theory-submit`, {answer_text: document.querySelector("#answer").value}
));
document.querySelector('#submit-unable')?.addEventListener('click', () => {
  const level = document.querySelector('#mastery-level')?.value;
  if (!level) { show('请先选择当前掌握程度。'); return; }
  submit(`/api/tasks/${taskId}/unable-submit`, {mastery_level:level}, true);
});

actionMastery();

function actionMastery() {
  const button=document.querySelector('#save-mastery'), select=document.querySelector('#mastery-edit');
  if (!button || !select) return;
  button.addEventListener('click', async () => {
    const unlock=lockControls([button,select]);
    try {
      await savedRequest(`learning-mastery-${taskId}`, `/api/tasks/${taskId}/mastery`, {
        mastery_level:select.value, expected_mastery_id:select.dataset.currentId || null
      }, 'PUT');
      location.reload();
    } catch(error) { show(error.definitelyRejected ? `没有保存：${error.message}` : `保存结果未确认：${error.message}`); }
    finally { unlock(); }
  });
}

function action(selector, operation) {
  const button = document.querySelector(selector);
  button?.addEventListener("click", async () => {
    const controls = ['#run-evaluation','#reevaluate'].includes(selector) ? document.querySelectorAll('#run-evaluation,#reevaluate') : [button];
    const unlock = lockControls(controls);
    try { await operation(button); } catch (error) {
      show(error.message);
      if (selector === '#correct-evaluation') {
        if (error.pending) show('这次评价的结果尚未确认。点击“重试上次评价”恢复原提交，当前选择暂不生效。');
        if (error.status === 409) await refreshEvaluationState();
      }
    }
    finally {
      if (selector === '#correct-evaluation') button.textContent = learningStore.get(`learning-evaluation-${button.dataset.attemptId}`) ? '重试上次评价' : '采用我的评价';
      unlock();
    }
  });
}

action("#show-reference", async () => {
  await ensureStarted();
  const data = await request(`/api/tasks/${taskId}/expose-answer`, {exposed_at: new Date().toISOString()});
  const reference = document.querySelector("#reference-text");
  const media = document.querySelector("#reference-media");
  media.replaceChildren();
  const rendered = learningMaterials.render(reference, data.materials,
    blockId => `/api/tasks/${taskId}/materials/${encodeURIComponent(blockId)}`,
    data.reference_text || '参考材料不足');
  if (!rendered) {
    if (card.dataset.variant === 'true') renderModelText(reference, data.reference_text || '参考材料不足');
    else reference.textContent = data.reference_text || '参考材料不足';
  }
  if (data.reference_correction) {
    const correction = document.createElement('section'), title = document.createElement('h4'), text = document.createElement('div');
    title.textContent = '本版本的参考校正'; text.textContent = data.reference_correction.content;
    correction.append(title, text);
    for (const source of data.reference_correction.sources) {
      const link = document.createElement('a'); link.href = source; link.textContent = source; link.target = '_blank'; link.rel = 'noopener noreferrer';
      correction.append(document.createElement('br'), link);
    }
    media.append(correction);
  }
  for (const item of data.materials) {
    if (rendered || item.kind !== "media" || item.status !== "complete") continue;
    const img = document.createElement("img");
    img.src = `/api/tasks/${taskId}/materials/${encodeURIComponent(item.block_id)}`;
    img.alt = "参考原图";
    img.style.maxWidth = "100%";
    media.append(img);
  }
  document.querySelector("#reference").hidden = false;
});

action("#enter-review", async () => {
  await request(`/api/questions/${card.dataset.questionId}/review`, {
    entered_by: "explicit_click", happened_at: new Date().toISOString(), task_id: taskId
  });
  show("已加入复习库，可从复习库开始或继续练习。");
});
action("#run-evaluation", async button => {
  show("正在评价，回答已保存在本地……");
  await showEvaluation(await request(`/api/model-jobs/${button.dataset.jobId}/run`, {}));
});
action("#reevaluate", async button => {
  const keyName = `learning-reevaluate-${button.dataset.attemptId}`;
  let key = learningStore.get(keyName);
  if (!key) {
    key = crypto.randomUUID();
    learningStore.set(keyName, key);
  }
  const job = await request(`/api/attempts/${button.dataset.attemptId}/reevaluate`, {}, key);
  learningStore.remove(keyName);
  // Keep the new job reachable even when the network call fails.
  let retry = document.querySelector("#run-evaluation");
  if (!retry) {
    retry = document.createElement("button");
    retry.id = "run-evaluation";
    retry.textContent = "按本次设置重试";
    button.after(retry);
    action("#run-evaluation", async current => {
      await showEvaluation(await request(`/api/model-jobs/${current.dataset.jobId}/run`, {}));
    });
  }
  retry.dataset.jobId = job.job_id; retry.hidden = false;
  retry.disabled = true;
  show("正在用当前设置重新评价，原回答与历史评价已保留……");
  try { await showEvaluation(await request(`/api/model-jobs/${job.job_id}/run`, {})); }
  finally { retry.disabled = false; }
});
function evaluationText(value) {
  const labels = {aligned:'通过',needs_review:'需要复习',unable_to_assess:'依据不足'};
  let feedback = '';
  if (value.raw_json) {
    const data = JSON.parse(value.raw_json);
    feedback = [data.brief_feedback, ...(data.missing_points || []), ...(data.errors || [])].filter(Boolean).join('\n');
  }
  return `${labels[value.verdict] || value.verdict}${value.adopted ? '（当前采用）' : '（保留记录）'}${value.corrected_by_user ? ' · 人工评价' : ''}\n${feedback}`;
}
action("#show-saved", async () => {
  const data = await request(`/api/tasks/${taskId}/saved-answer`, {});
  show([data.answer_text, data.code_self_result === 'can_solve' ? '自评：会做' : data.code_self_result ? '自评：不会做' : '', data.note,
    ...data.evaluations.map(evaluationText)].filter(Boolean).join('\n\n'));
});
const correctionButton = document.querySelector('#correct-evaluation');
if (correctionButton && learningStore.get(`learning-evaluation-${correctionButton.dataset.attemptId}`)) correctionButton.textContent = '重试上次评价';
action("#correct-evaluation", async button => {
  const key = `learning-evaluation-${button.dataset.attemptId}`;
  const pending = learningStore.get(key);
  await savedRequest(key, `/api/attempts/${button.dataset.attemptId}/evaluation`, pending?.values || {
    verdict: document.querySelector("#manual-verdict").value,
    expected_adoption: button.dataset.adoptionId || null
  });
  show('已采用你的评价，复习进度已重新计算。');
  await refreshEvaluationState();
});

async function showEvaluation(data) {
  show(evaluationText(data));
  await refreshEvaluationState();
}
async function refreshEvaluationState() {
  try {
    const html = new DOMParser().parseFromString(await requestText(location.pathname), 'text/html');
    for (const id of ['evaluation-summary','review-credit','evaluation-job-state']) {
      const current = document.getElementById(id), updated = html.getElementById(id);
      if (current) { current.textContent = updated?.textContent || ''; current.hidden = !updated; }
    }
    const correction = document.querySelector('#correct-evaluation');
    if (correction) correction.dataset.adoptionId = html.querySelector('#correct-evaluation')?.dataset.adoptionId || '';
    const retry = document.querySelector('#run-evaluation');
    if (retry) retry.hidden = !html.querySelector('#run-evaluation');
  } catch {
    result.append(document.createTextNode('\n记录已保存，但页面状态未刷新。可刷新页面核对。'));
  }
}
