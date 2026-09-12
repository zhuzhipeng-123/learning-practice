const card = document.querySelector("[data-task-id]");
const result = document.querySelector("#result");
const taskId = card.dataset.taskId;
const storageKey = `learning-submit-${taskId}`;
let starting;
try {
  const pending = JSON.parse(sessionStorage.getItem(storageKey) || "null");
  if (pending && card.dataset.taskStatus !== "completed") {
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
  const response = await fetch(path, {method: "POST", headers, body: JSON.stringify(body)});
  const data = await readResponse(response);
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data));
  return data;
}

async function ensureStarted() {
  if (card.dataset.taskStatus === "completed") return;
  if (!starting) starting = request(`/api/tasks/${taskId}/attempts`, {
    entry_mode: "web", started_at: new Date().toISOString()
  }).catch(error => { starting = null; throw error; });
  await starting;
}

// Opening an unstarted practice protects it from plan redistribution in another tab.
if (card.dataset.taskStatus === 'pending') ensureStarted().catch(error => show(error.message));

async function submit(path, values) {
  const buttons = document.querySelectorAll("[data-code-result],#submit-theory");
  buttons.forEach(button => button.disabled = true);
  try {
    await ensureStarted();
    let pending = JSON.parse(sessionStorage.getItem(storageKey) || "null");
    if (pending && JSON.stringify(pending.values) !== JSON.stringify(values)) {
      throw new Error("上次提交结果尚未确认。请先恢复原答案重试，或刷新查看保存结果。");
    }
    if (!pending) {
      pending = {path, key: crypto.randomUUID(), values,
        body: {...values, entry_mode: "web", submitted_at: new Date().toISOString()}};
      sessionStorage.setItem(storageKey, JSON.stringify(pending));
    }
    await request(pending.path, pending.body, pending.key);
    sessionStorage.removeItem(storageKey);
    location.reload();
  } catch (error) {
    show(`提交未确认：${error.message}。原请求已保留，可以重试。`);
  } finally {
    buttons.forEach(button => button.disabled = false);
  }
}

for (const button of document.querySelectorAll("[data-code-result]")) {
  button.addEventListener("click", () => submit(`/api/tasks/${taskId}/code-submit`, {
    code_self_result: button.dataset.codeResult, note: document.querySelector("#note").value
  }));
}
document.querySelector("#submit-theory")?.addEventListener("click", () => submit(
  `/api/tasks/${taskId}/theory-submit`, {answer_text: document.querySelector("#answer").value}
));

function action(selector, operation) {
  const button = document.querySelector(selector);
  button?.addEventListener("click", async () => {
    button.disabled = true;
    try { await operation(button); } catch (error) { show(error.message); }
    finally { button.disabled = false; }
  });
}

action("#show-reference", async () => {
  await ensureStarted();
  const data = await request(`/api/tasks/${taskId}/expose-answer`, {exposed_at: new Date().toISOString()});
  document.querySelector("#reference-text").textContent = data.reference_text || "参考材料不足";
  const media = document.querySelector("#reference-media");
  media.replaceChildren();
  for (const item of data.materials) {
    if (item.kind !== "media" || item.status !== "complete") continue;
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
    entered_by: "explicit_click", happened_at: new Date().toISOString()
  });
  show("已加入复习库，可从复习库开始或继续练习。");
});
action("#run-evaluation", async button => {
  show("正在评价，回答已保存在本地……");
  show(evaluationText(await request(`/api/model-jobs/${button.dataset.jobId}/run`, {})));
});
action("#reevaluate", async button => {
  const keyName = `learning-reevaluate-${button.dataset.attemptId}`;
  let key = sessionStorage.getItem(keyName);
  if (!key) {
    key = crypto.randomUUID();
    sessionStorage.setItem(keyName, key);
  }
  const job = await request(`/api/attempts/${button.dataset.attemptId}/reevaluate`, {}, key);
  sessionStorage.removeItem(keyName);
  // Keep the new job reachable even when the network call fails.
  let retry = document.querySelector("#run-evaluation");
  if (!retry) {
    retry = document.createElement("button");
    retry.id = "run-evaluation";
    retry.textContent = "按本次设置重试";
    button.after(retry);
    action("#run-evaluation", async current => {
      show(evaluationText(await request(`/api/model-jobs/${current.dataset.jobId}/run`, {})));
    });
  }
  retry.dataset.jobId = job.job_id;
  retry.disabled = true;
  show("正在用当前设置重新评价，原回答与历史评价已保留……");
  try { show(evaluationText(await request(`/api/model-jobs/${job.job_id}/run`, {}))); }
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
action("#correct-evaluation", async button => {
  await request(`/api/attempts/${button.dataset.attemptId}/evaluation`, {
    verdict: document.querySelector("#manual-verdict").value
  });
  show('已采用你的评价，复习进度已重新计算。');
});
