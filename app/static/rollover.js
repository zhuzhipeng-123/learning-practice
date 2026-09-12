(() => {
let dirty=false,checking=false;
document.addEventListener('input',event=>{if(event.target.matches('input,textarea,select'))dirty=true;});
async function checkDay(){
 if(checking || document.hidden)return;checking=true;
 try{const data=await readResponse(await fetch('/api/day-status',{cache:'no-store'}));if(data.date!==document.body.dataset.localDate){if(dirty)document.querySelector('#new-day-notice').hidden=false;else location.reload();}}catch{}finally{checking=false;}
}
window.checkLearningDay=checkDay;
setInterval(checkDay,60000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkDay();});
})();