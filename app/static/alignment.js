(() => {
  const buttons = [...document.querySelectorAll('[data-align-source]')];
  const output = document.querySelector('#alignment-result');
  if (!output) return;
  const labels = {added:'新增题目',updated:'修改题目',removed:'确认移除（历史保留）',missing:'原位置缺失，暂停新抽题待核验',restored:'恢复题目',modules_added:'新增目录 / 标题',modules_removed:'移除目录 / 标题',modules_updated:'调整目录 / 标题'};
  const decisions = {single:'单题 / 多种解法',split:'建议拆题',note:'知识笔记',missing:'资料不足'};
  let timer, watching = false;
  function line(parent, tag, value) { const node=document.createElement(tag);node.textContent=value;parent.append(node);return node; }
  function render(sources) {
    output.hidden=false;output.replaceChildren();
    for (const source of sources) {
      const box=document.createElement('article');output.append(box);
      const summary=JSON.parse(source.summary_json || '{}');
      if (summary.change_note) line(box,'p',`你的改动说明：${summary.change_note}`);
      if (summary.user_review) {line(box,'p',`模型复核：${summary.user_review.review}`); for (const item of summary.user_review.unresolved) line(box,'p',`待核验：${item}`);}
      if (summary.review_error) line(box,'p',`原文对齐已保留，模型复核未完成：${summary.review_error}`);
      line(box,'h4',`${source.question_type==='theory'?'八股':'代码'} · ${source.running ? '正在对齐…' : source.sync_status==='failed' ? '读取失败' : '最近一次对齐结果'}`);
      if (source.last_check_at) line(box,'p',`检查时间：${new Date(source.last_check_at).toLocaleString()}`);
      if (source.last_error && !summary.documents?.length) line(box,'p',source.last_error);
      if (summary.phase) {
        line(box,'p',`目录遍历：${summary.tree_complete ? '已完整读取可访问的目录树' : '尚未完成 / 有缺项'}；发现 ${summary.discovered_nodes || 0} 个节点。`);
        for (const error of summary.tree_errors || []) line(box,'p',`${error.path}：${error.error}`);
        const docs=summary.documents || [];
        line(box,'p',`文档进度：${docs.filter(item=>['complete','partial','failed'].includes(item.status)).length} / ${docs.length}。${summary.phase==='discovery' ? '正在递归查找所有子目录…' : ''}`);
        const dc=summary.document_changes || {};
        for (const [key,label] of Object.entries({added:'发现 / 新增文档节点',moved:'移动 / 改名文档节点',missing:'缺失文档节点',restored:'恢复文档节点',unsupported:'未支持的文档类型'})) {
          if (!dc[key]?.length) continue;
          const detail=line(box,'details','');line(detail,'summary',`${label}（${dc[key].length}）`);
          for (const item of dc[key]) line(detail,'p',typeof item==='string' ? item : item.before ? `${item.before} → ${item.after}` : `${item.path}：${item.error || (item.confirmed ? '已确认移除，历史保留' : '首次缺失，暂停新抽题')}`);
        }
        if (docs.length) {
          const detail=line(box,'details','');line(detail,'summary','逐篇文档读取结果');
          const states={pending:'等待中',reading:'读取正文和素材',analyzing:'大模型分析中',complete:'已完成',partial:'有待确认项',failed:'失败'};
          for (const item of docs) {
            line(detail,'p',`${item.path}：${states[item.status]}${item.result ? ` · ${item.result.block_count || 0} 个原文块 · ${item.result.published_count || 0} 道已入库 · ${item.result.candidate_count || 0} 条待确认 · ${item.result.unsupported_block_count || 0} 个未支持内容块 · ${item.result.material_failures || 0} 个素材失败` : ''}${item.error ? ' — '+item.error : ''}`);
            for (const error of item.result?.material_errors || []) line(detail,'p',typeof error==='string' ? error : JSON.stringify(error));
          }
        }
      }
      if (source.running) line(box,'p',summary.analysis?.status==='running' ? `大模型分析中：${summary.analysis.processed} / ${summary.analysis.total} 条` : '正在检查文档版本、题目边界和图片 / 表格，请稍候。');
      const changes=summary.changes;
      if (changes) {
        line(box,'p',`新增 ${changes.added.length} · 修改 ${changes.updated.length} · 确认移除 ${changes.removed.length} · 缺失待核验 ${changes.missing.length} · 未变化 ${changes.unchanged}`);
        for (const [key,label] of Object.entries(labels)) {
          if (!changes[key]?.length) continue;
          const details=line(box,'details','');line(details,'summary',`${label}（${changes[key].length}）`);
          const list=line(details,'ul','');
          for (const item of changes[key]) line(list,'li',typeof item==='string' ? item : item.before ? `${item.before} → ${item.after}` : `${item.title} · ${item.module}${item.changed_fields ? "："+item.changed_fields.join("、") : ""}${item.previous_module && item.previous_module!==item.module ? `（原：${item.previous_module}）`:''}`);
        }
      }
      if (summary.candidate_count) line(box,'p',`待确认候选 ${summary.candidate_count} 条；需在题库管理中确认后进入抽题。`);
      const analysis=summary.analysis;
      if (analysis) {
        line(box,'p',analysis.status==='not_needed' ? '没有需要模型判断的候选。' : `大模型已分析 ${analysis.processed} / ${analysis.total} 条${analysis.errors.length ? '，未全部完成' : ''}。`);
        for (const error of analysis.errors) line(box,'p',`分析失败：${error}。可修复配置后再次点击对齐。`);
        if (analysis.suggestions.length) {
          const details=line(box,'details','');line(details,'summary','查看大模型的逐条分析');
          for (const item of analysis.suggestions) line(details,'p',`${item.title}：${decisions[item.decision]}。${item.reason}${item.parts.length ? '；建议：'+item.parts.join(' / ') : ''}`);
        }
      }
    }
  }
  async function poll() {
    try {
      const data=await readResponse(await fetch('/api/source-status'));
      const running=data.sources.some(source=>source.running);
      if (watching || running) render(data.sources);
      buttons.forEach(button=>button.disabled=running);
      if (running) { watching=true;timer=setTimeout(poll,2000); }
      else if (watching) {
        watching=false;
        line(output,'p','本轮对齐已结束。历史任务、作答和评价保持原样。');
        const link=line(output,'a','刷新页面，查看最新模块和候选 →');link.href=location.pathname;
      }
    } catch(error) {
      output.hidden=false;line(output,'p',`${error.message} 页面将在 5 秒后重新查询后台状态。`);
      timer=setTimeout(poll,5000);
    }
  }
  buttons.forEach(button=>button.addEventListener('click',async()=>{
    buttons.forEach(item=>item.disabled=true);output.hidden=false;output.textContent='已请求重新对齐飞书：本次会读取最新内容，并分析待确认的题目边界…';
    try {
      clearTimeout(timer);
      await readResponse(await fetch(button.dataset.alignSource ? `/api/sources/${button.dataset.alignSource}/sync` : '/api/sources/align-all',{method:'POST',headers:{'X-Requested-With':'learning-practice','Content-Type':'application/json'},body:JSON.stringify({change_note:document.querySelector('#alignment-note')?.value || ''})}));
      watching=true;await poll();
    } catch(error) { output.textContent=error.message;buttons.forEach(item=>item.disabled=false); }
  }));
  const history=document.querySelector('#show-alignment-result');
  history?.addEventListener('click',async()=>{try {render((await readResponse(await fetch('/api/source-status'))).sources);}catch(error){output.hidden=false;output.textContent=error.message;}});
  poll();
})();
