/* Verify cancellation ownership and failures using the production composer controller.
   Actual layout, inspector selection and reply cancellation are checked in the browser. */
const assert = require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
class Element {
  constructor(dataset={}) {this.dataset=dataset;this.handlers={};this.attributes={};this.hidden=false;this.disabled=false;this.children=[];this.textContent='';this.classList={contains:()=>true};}
  addEventListener(name,fn) {this.handlers[name]=fn;}
  setAttribute(name,value) {this.attributes[name]=value;}
  closest() {return wrap;}
  querySelector(selector) {return selector==='button'?secondaryButton:this.children.find(e=>e.className==='composer-control-status')||null;}
  appendChild(node) {this.children.push(node);}
}
const app=new Element({conversationMode:'work'}),header=new Element({runId:'run-A',canCancel:'true'}),primary=new Element(),send=new Element(),secondary=new Element(),secondaryButton=new Element(),secondarySend=new Element(),wrap=new Element();
const nodes={app,'run-header':header,chatStop:primary,chatSend:send,runComposerStopForm:secondary};
const listeners={},document={getElementById:id=>nodes[id]||null,querySelector:()=>secondarySend,createElement:()=>new Element(),addEventListener:(name,fn)=>listeners[name]=fn};
let failure=true,requests=[],release;
const context={document,window:{},Date,URL,fetch:async(url,options)=>{
  requests.push({url,options});
  if (url.endsWith('/cancel')) {if(failure)return {ok:false,url:'http://localhost'+url};await new Promise(resolve=>release=resolve);return {ok:true,url:'http://localhost'+url};}
  return {ok:true,json:async()=>({can_cancel:false})};
}};
vm.runInNewContext(fs.readFileSync(process.argv[2],'utf8'),context);
function event(name,detail) {listeners[name]({detail});}
function click() {primary.handlers.click({currentTarget:primary,preventDefault(){}});}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  assert.equal(primary.dataset.stopRun,'run-A');assert.equal(send.hidden,true);
  event('saberops:run-action-state',{run_id:'run-B',can_cancel:false});
  assert.equal(primary.dataset.stopRun,'run-A','Another run cannot change this control');
  event('saberops:chat-refreshed',{runs:[{run_id:'run-B'}]});
  assert.equal(primary.dataset.stopRun,'run-A','Run-page stop remains bound to the displayed run');
  app.dataset.conversationMode='chat';event('saberops:conversation-mode',{});
  assert.equal(primary.hidden,true);click();assert.equal(requests.length,0,'Chat never cancels a run');
  context.window.SaberOpsComposerControls.replyState(true);
  assert.equal(primary.attributes['aria-label'],'Stop reply');assert.equal(primary.dataset.stopRun,'');click();assert.equal(requests.length,0);
  app.dataset.conversationMode='work';event('saberops:conversation-mode',{});click();await flush();
  assert.equal(requests[0].url,'/runs/run-A/cancel');assert.equal(requests[0].options.method,'POST');assert.equal(requests[0].options.credentials,'same-origin');
  assert(wrap.children[0].textContent.includes('Could not stop'));assert.equal(primary.hidden,false);assert.equal(primary.disabled,false,'A rejected stop remains retryable');
  failure=false;click();await flush();click();assert.equal(requests.filter(r=>r.url.endsWith('/cancel')).length,2,'Only one stop is pending');release();await flush();await flush();
  assert.equal(primary.dataset.stopRun,'');assert.equal(primary.attributes['aria-label'],'Stop reply','Stopping work preserves an independently running reply');
  context.window.SaberOpsComposerControls.replyState(false);assert.equal(primary.hidden,true);assert.equal(send.hidden,false,'Finished work restores Send');
  assert.equal(secondary.hidden,true);assert.equal(secondarySend.hidden,false);
  console.log('Composer cancellation scope, Chat separation, errors and duplicate-click checks passed');
})().catch(error=>{console.error(error);process.exitCode=1});
