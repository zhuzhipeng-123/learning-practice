(() => {
function index(items) {
  const result = new Map(), pending = [...(items || [])];
  while (pending.length) {
    const item = pending.shift();
    if (!item || typeof item !== 'object') continue;
    if (item.block_id) result.set(String(item.block_id), item);
    if (Array.isArray(item.attachments)) pending.push(...item.attachments);
  }
  return result;
}

function image(blockId, alt, materialUrl) {
  const element = document.createElement('img');
  element.src = materialUrl(blockId);
  element.alt = alt;
  element.loading = 'lazy';
  return element;
}

function cellAt(cells, row, column, columns) {
  if (!Array.isArray(cells)) return null;
  if (Array.isArray(cells[row])) return cells[row][column] ?? '';
  return cells.find(cell => cell?.row === row && cell?.column === column)
    || cells[row * columns + column];
}

function cellNode(target, node, resources, materialUrl) {
  if (node.kind === 'text' || node.kind === 'code') {
    const element = document.createElement(node.kind === 'code' ? 'pre' : 'span');
    element.textContent = node.text || '';
    if (node.kind === 'code' && node.language) element.dataset.language = node.language;
    target.append(element);
  } else if (node.kind === 'media' && resources.get(String(node.block_id))?.status === 'complete') {
    target.append(image(node.block_id, '表格内图片', materialUrl));
  } else if (node.kind === 'media') {
    gap(target, '表格内图片未完整归档');
  } else if (node.kind === 'sheet' || node.kind === 'table') {
    const nested = resources.get(String(node.block_id));
    if (nested?.status === 'complete') target.append(table(nested, resources, materialUrl));
    else gap(target, '表格内素材未完整归档');
  }
}

function gap(target, message) {
  const notice = document.createElement('p');
  notice.className = 'notice material-gap';
  notice.textContent = message;
  target.append(notice);
}

function table(item, resources, materialUrl) {
  const wrapper = document.createElement('div'), element = document.createElement('table');
  wrapper.className = 'structured-table';
  wrapper.tabIndex = 0;
  wrapper.setAttribute('aria-label', '冻结版本表格，可横向滚动');
  const structure = item.structure || {};
  const rows = Math.max(0, Number(structure.rows) || 0);
  const columns = Math.max(0, Number(structure.columns) || 0);
  const merges = Array.isArray(structure.merge_info) ? structure.merge_info : [];
  for (let row = 0; row < rows; row += 1) {
    const tr = document.createElement('tr');
    for (let column = 0; column < columns; column += 1) {
      const merge = merges.find(value => row >= Number(value.row) && row < Number(value.row) + (Number(value.row_span) || 1)
        && column >= Number(value.column) && column < Number(value.column) + (Number(value.column_span) || 1));
      if (merge && (row !== Number(merge.row) || column !== Number(merge.column))) continue;
      const td = document.createElement('td'), cell = cellAt(structure.cells, row, column, columns);
      if (merge) {
        td.rowSpan = Math.max(1, Number(merge.row_span) || 1);
        td.colSpan = Math.max(1, Number(merge.column_span) || 1);
      }
      if (typeof cell === 'string' || typeof cell === 'number') td.textContent = String(cell);
      else for (const node of cell?.content || []) cellNode(td, node, resources, materialUrl);
      tr.append(td);
    }
    element.append(tr);
  }
  wrapper.append(element);
  return wrapper;
}

function render(target, materials, materialUrl, fallback='') {
  const values = Array.isArray(materials) ? materials : [];
  const resources = index(values);
  const ordered = values.find(item => item?.kind === 'ordered_content');
  if (!ordered) {
    target.textContent = fallback || '';
    return false;
  }
  target.replaceChildren();
  for (const node of ordered.nodes || []) {
    if (node.kind === 'text') {
      const text = document.createElement('p');
      text.textContent = node.text || '';
      target.append(text);
    } else if (node.kind === 'code') {
      const pre = document.createElement('pre'), code = document.createElement('code');
      code.textContent = node.text || '';
      if (node.language) code.dataset.language = node.language;
      pre.append(code);
      target.append(pre);
    } else if (node.kind === 'media') {
      const item = resources.get(String(node.block_id));
      if (item?.status === 'complete') {
        target.append(image(node.block_id, '冻结版本原图', materialUrl));
      } else gap(target, '这张冻结版本图片未完整归档');
    } else if (node.kind === 'sheet' || node.kind === 'table') {
      const item = resources.get(String(node.block_id));
      if (item?.status === 'complete') target.append(table(item, resources, materialUrl));
      else gap(target, '这份冻结版本表格未完整归档');
    }
  }
  if (!target.childNodes.length) target.textContent = fallback || '结构化内容暂不可显示';
  return true;
}

window.learningMaterials = {render};
})();
