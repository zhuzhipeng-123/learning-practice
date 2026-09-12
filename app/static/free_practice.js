(() => {
const form=document.querySelector('#free-form'),mode=document.querySelector('#free-mode'),button=document.querySelector('#create-free'),status=document.querySelector('#free-status');
let pending=null;
mode.addEventListener('change',()=>{document.querySelector('#theme-label').hidden=mode.value==='random';button.textContent=mode.value==='random'?'开始随机练习':'按我的描述选题';});
form.addEventListener('submit',async event=>{
 event.preventDefault();if(!form.reportValidity())return;button.disabled=true;
 const body={mode:mode.value,theme:document.querySelector('#theme').value,count:Number(document.querySelector('#count').value),only_new:document.querySelector('#only-new').checked};
 if(!pending || JSON.stringify(pending.body)!==JSON.stringify(body))pending={body,key:crypto.randomUUID()};
 status.textContent=body.mode==='random'?'正在随机抽题…':'正在理解你的范围并匹配题库…';
 try {
  const data=await readResponse(await fetch('/api/free-practice',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'learning-practice','Idempotency-Key':pending.key},body:JSON.stringify(body)}));
  const result=document.querySelector('#free-result');result.replaceChildren();
  status.textContent=`已安排 ${data.added} 题${data.missing?`，还缺 ${data.missing} 题。可以调整范围或更新题库。`:'，选一题开始吧。'}`;
  for(const task of data.tasks){const card=document.createElement('div');card.className='task-row';const content=document.createElement('div');const title=document.createElement('h3');title.textContent=task.prompt;const module=document.createElement('p');module.className='muted';module.textContent=task.category_path;content.append(title,module);const link=document.createElement('a');link.className='button-link';link.href=`/practice/${task.id}`;link.textContent='开始练习 →';card.append(content,link);result.append(card);}
  pending=null;
 }catch(error){status.textContent=error.message;}finally{button.disabled=false;}
});
})();