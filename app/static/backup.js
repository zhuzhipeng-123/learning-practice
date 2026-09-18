(() => {
const status = document.querySelector('#backup-status');
const download = document.querySelector('#download-backup'), verify = document.querySelector('#verify-backup');
async function run(action) {
  const unlock = lockControls([download, verify]);
  try { await action(); } catch (error) { status.textContent = error.message; } finally { unlock(); }
}
download.onclick = () => run(async () => {
  status.textContent = '正在打包学习记录与附件…';
  const blob = await requestBlob('/api/exports', {method:'POST', headers:{'X-Requested-With':'learning-practice'}});
  const url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = `learning-backup-${document.body.dataset.localDate}.zip`;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  status.textContent = '备份已生成，已发起下载。可再核验最近一次备份。';
});
verify.onclick = () => run(async () => {
  status.textContent = '正在独立目录核验数据库与附件…';
  await requestJSON('/api/exports/verify', {method:'POST', headers:{'X-Requested-With':'learning-practice'}});
  status.textContent = '核验通过：最近一次备份可读取，当前学习记录未被覆盖。';
});
})();
