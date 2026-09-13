for (const button of document.querySelectorAll('[data-view-reflection]')) {
  button.addEventListener('click', async () => {
    const output = document.getElementById(button.dataset.output), token = crypto.randomUUID();
    const unlock = lockControls([button]); output.dataset.renderRequest = token;
    try {
      const data = await requestJSON(`/api/model-reflections/${button.dataset.viewReflection}/view`, {
        method:'POST', headers:{'X-Requested-With':'learning-practice'}
      });
      if (output.dataset.renderRequest === token) renderModelText(output, data.content);
    } catch (error) { if (output.dataset.renderRequest === token) output.textContent = error.message; }
    finally { unlock(); }
  });
}
