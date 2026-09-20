// Run the actual browser request helper in two isolated JS globals sharing storage.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/static/http.js'), 'utf8');
const alignmentSource = fs.readFileSync(require('node:path').join(__dirname, '../app/static/alignment.js'), 'utf8');
const materialsSource = fs.readFileSync(require('node:path').join(__dirname, '../app/static/materials.js'), 'utf8');
const persisted = new Map();
const localStorage = {
  getItem: key => persisted.get(key) ?? null,
  setItem: (key, value) => persisted.set(key, value),
  removeItem: key => persisted.delete(key),
};
const requests = [];
function tab(name, storage=localStorage) {
  const context = {localStorage:storage, sessionStorage:storage, crypto:{randomUUID}, AbortController, setTimeout, clearTimeout,
    document:{addEventListener() {}},
    fetch(path, options) {
      return new Promise((resolve, reject) => requests.push({name, path, options, resolve, reject}));
    },
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return context;
}

function alignmentTab(calls) {
  const listeners = {};
  const node = () => ({hidden:false, disabled:false, dataset:{}, append(){}, replaceChildren(){}, addEventListener(type, handler){listeners[type]=handler;}});
  const button = node(); button.dataset.alignSource = 'source-code';
  const output = node(), note = {value:'frozen note'}, history = null;
  const document = {
    querySelectorAll: selector => selector === '[data-align-source]' ? [button] : [],
    querySelector: selector => selector === '#alignment-result' ? output : selector === '#alignment-note' ? note : selector === '#show-alignment-result' ? history : null,
    createElement: () => node(), addEventListener() {},
  };
  const context = {localStorage, sessionStorage:localStorage, crypto:{randomUUID}, setTimeout:()=>0, clearTimeout(){}, document,
    location:{pathname:'/sources'},
  };
  context.window = context;
  vm.createContext(context); vm.runInContext(source, context);
  context.requestJSON=async (path, options={}) => {
      calls.push({path, method:options.method || 'GET'});
      if (options.method === 'POST') throw new Error('Simulated response loss');
      if (path === '/api/source-status') return {sources:[{id:'source-code',question_type:'code',running:false,recoverable:true,sync_status:'recoverable',summary_json:'{}'}]};
      return {status:'recoverable',terminal:false,runs:[]};
    };
  vm.runInContext(alignmentSource, context);
  return {context, click: listeners.click};
}

function renderPendingTableImage() {
  const node = tag => ({tag, childNodes:[], textContent:'', className:'', append(...items){this.childNodes.push(...items);},
    replaceChildren(...items){this.childNodes=[...items];}, setAttribute(){}});
  const context = {document:{createElement:node}};
  context.window = context;
  vm.createContext(context); vm.runInContext(materialsSource, context);
  const target = node('target');
  const table = {kind:'table', block_id:'table', status:'complete', attachments:[
    {kind:'media', block_id:'image', status:'pending'},
  ], structure:{rows:1, columns:1, cells:[{row:0, column:0, content:[
    {kind:'media', block_id:'image'},
  ]}]}};
  context.learningMaterials.render(target, [table, {kind:'ordered_content', nodes:[
    {kind:'table', block_id:'table'},
  ]}], blockId => `/material/${blockId}`);
  const text = item => [item.textContent, ...item.childNodes.flatMap(text)].filter(Boolean);
  return text(target);
}
const success = (index, id) => requests[index].resolve({ok:true, status:200, text:async () => JSON.stringify({id})});
async function main() {
  const a = tab('A'), b = tab('B'), key = 'learning-free-batch-code', path = '/api/free-practice/batches';
  const first = a.savedRequest(key, path, {count:1});
  const firstKey = JSON.parse(localStorage.getItem(key)).key;
  const recoveredInB = b.savedRequest(key, path, {count:1});
  assert.equal(requests[1].options.headers['Idempotency-Key'], firstKey);
  success(0, 'first-batch');
  await first;
  const next = a.savedRequest(key, path, {count:2});
  const nextKey = JSON.parse(localStorage.getItem(key)).key;
  success(1, 'first-batch');
  await recoveredInB;
  const keyAfterOlderResponse = localStorage.getItem(key);
  requests[2].reject(new Error('Simulated response lost after server acceptance'));
  const unknown = await next.catch(error => error);
  const reloaded = tab('A reloaded');
  const output = {
    oldAndNewKeysDiffer:firstKey !== nextKey,
    newerRequestWasPersisted: Boolean(nextKey),
    newerKeyStillPersistedAfterOldResponse: Boolean(keyAfterOlderResponse),
    newerFailureStillUnknown:Boolean(unknown.pending),
    reloadedTabCanRecover: Boolean(reloaded.learningStore.get(key)),
  };
  b.learningStore.set('deleted-elsewhere', 'stale memory');
  a.learningStore.remove('deleted-elsewhere');
  assert.equal(b.learningStore.get('deleted-elsewhere'), null, 'Successful reads must respect another tab deletion');
  const unavailable = {
    getItem() { return null; },
    setItem() { throw new Error('QuotaExceededError'); },
    removeItem() { throw new Error('Storage disabled'); }
  };
  const restricted = tab('Restricted', unavailable);
  for (const store of [restricted.learningStore, restricted.tabLearningStore]) {
    store.set('draft', 'Keep this in memory');
    assert.equal(store.get('draft'), 'Keep this in memory');
    assert.equal(store.available, false);
    store.remove('draft');
    assert.equal(store.get('draft'), null);
  }
  // Late terminal rejection also cannot remove a newer pending request.
  const c = tab('C'), d = tab('D'), other = 'late-rejection';
  const oldRequest = c.savedRequest(other, path, {count:1});
  const delayed = d.savedRequest(other, path, {count:1}).catch(error=>error);
  success(3, 'old-result'); await oldRequest;
  const fresh = c.savedRequest(other, path, {count:2}).catch(error=>error);
  const freshKey = c.learningStore.get(other).key;
  requests[4].resolve({ok:false,status:409,text:async()=>JSON.stringify({detail:'Rejected',request_state:'failed'})});
  await delayed;
  assert.equal(c.learningStore.get(other).key, freshKey);
  requests[5].reject(new Error('Unknown network outcome')); await fresh;
  const alignmentCalls = [], alignment = alignmentTab(alignmentCalls);
  await alignment.click();
  const pendingAlignment = alignment.context.learningStore.get('learning-alignment-source-code');
  assert.ok(pendingAlignment, 'Lost alignment response must retain the original request');
  const postsBeforeReload = alignmentCalls.filter(call => call.method === 'POST').length;
  alignmentTab(alignmentCalls);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(alignmentCalls.filter(call => call.method === 'POST').length, postsBeforeReload,
    'Reload recovery must query the accepted run without another POST');
  assert.ok(alignmentCalls.some(call => call.path.includes('/api/alignment-requests/')),
    `Reload recovery must read the original alignment request: ${JSON.stringify(alignmentCalls)}`);
  assert.ok(renderPendingTableImage().includes('表格内图片未完整归档'),
    'An incomplete image inside a table must render an explicit material gap');
  console.log(JSON.stringify({...output, quotaFallback:true, lateRejectionPreservesNewer:true,
    alignmentReadOnlyRecovery:true, nestedMaterialGap:true}, null, 2));
  assert.equal(output.reloadedTabCanRecover, true, 'Older tab response erased newer unknown request recovery');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
