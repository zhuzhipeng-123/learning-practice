// Run the actual browser request helper in two isolated JS globals sharing storage.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {randomUUID} = require('node:crypto');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/static/http.js'), 'utf8');
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
  console.log(JSON.stringify({...output, quotaFallback:true, lateRejectionPreservesNewer:true}, null, 2));
  assert.equal(output.reloadedTabCanRecover, true, 'Older tab response erased newer unknown request recovery');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
