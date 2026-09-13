// Render common model formatting with DOM nodes; never interpret model HTML.
window.renderModelText = function(target, text) {
  target.replaceChildren();
  target.classList.add('model-text');
  let list = null, code = null;
  function inline(node, value) {
    for (const part of value.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
      const tag = part.startsWith('**') && part.endsWith('**') ? 'strong'
        : part.startsWith('`') && part.endsWith('`') ? 'code' : null;
      if (!tag) { node.append(document.createTextNode(part)); continue; }
      const span = document.createElement(tag);
      span.textContent = tag === 'strong' ? part.slice(2, -2) : part.slice(1, -1);
      node.append(span);
    }
  }
  const lines = String(text || '').split('\n');
  const cells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(value => value.trim());
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (line.trim().startsWith('```')) {
      if (code) code = null;
      else { code = document.createElement('pre'); target.append(code); }
      list = null; continue;
    }
    if (code) { code.append(document.createTextNode(line + '\n')); continue; }
    if (line.includes('|') && lines[index+1]?.includes('|') && cells(lines[index+1]).every(value => /^:?-{3,}:?$/.test(value))) {
      const wrapper = document.createElement('div'), table = document.createElement('table');
      wrapper.style.overflowX = 'auto'; table.style.width = '100%';
      const appendRow = (values, tag) => {
        const row = document.createElement('tr');
        for (const value of values) { const cell = document.createElement(tag); inline(cell, value); row.append(cell); }
        table.append(row);
      };
      appendRow(cells(line), 'th'); index += 1;
      while (lines[index+1]?.includes('|') && lines[index+1].trim()) appendRow(cells(lines[++index]), 'td');
      wrapper.append(table); target.append(wrapper); list = null; continue;
    }
    if (!line.trim() || /^\s*---+\s*$/.test(line)) { list = null; continue; }
    const heading = line.match(/^#{1,6}\s+(.+)/);
    const bullet = line.match(/^\s*(?:[-*]\s+|([0-9]+)[.)]\s+)(.*)/);
    if (bullet) {
      const tag = bullet[1] ? 'ol' : 'ul';
      if (!list || list.localName !== tag) {
        list = document.createElement(tag);
        if (bullet[1]) list.start = Number(bullet[1]);
        target.append(list);
      }
      const item = document.createElement('li'); inline(item, bullet[2]); list.append(item);
    } else {
      list = null;
      const block = document.createElement(heading ? 'h4' : 'p');
      inline(block, heading ? heading[1] : line.replace(/^>\s?/, ''));
      target.append(block);
    }
  }
};
