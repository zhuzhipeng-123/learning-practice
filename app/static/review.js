for (const button of document.querySelectorAll('[data-review-question]')) {
  button.addEventListener('click', async () => {
    button.disabled = true;
    const output = button.closest('[data-review-kind]')?.querySelector('[role="status"]') || document.querySelector('#review-result');
    try {
      const data = await savedRequest(`learning-review-${button.dataset.reviewQuestion}`, `/api/questions/${button.dataset.reviewQuestion}/start-review`, {});
      if (data.version_conflict) {
        output.textContent = data.message;
        const link = document.createElement('a'); link.href = `/practice/${data.task_id}`; link.textContent = '继续已安排的版本 →'; output.append(link);
      } else location.href = `/practice/${data.task_id}`;
    } catch (error) { output.textContent = error.message; }
    finally { button.disabled = false; }
  });
}
