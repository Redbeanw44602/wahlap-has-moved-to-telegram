// A small DOM/event-loop harness for executing the actual gateway script.
// Layout metrics are modeled; this is not a real rendering engine.
const vm = require('node:vm');

class Target {
  constructor() { this.listeners = {}; this.attributes = {}; this.hidden = false; }
  addEventListener(type, fn, options) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type, detail = {}) { for (const fn of this.listeners[type] || []) fn(detail); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
}

function createHarness(source, options = {}) {
  const rafs = [], timers = new Map();
  let timerId = 0, readyCalls = 0;
  const window = new Target();
  const stage = new Target();
  stage.clientWidth = 360;
  stage.clientHeight = 640;
  stage.children = [];
  stage.appendChild = frame => stage.children.push(frame);
  const nodes = {stage,error:new Target(),'error-message':new Target(),retry:new Target()};
  nodes.error.hidden = true;
  const back = {show(){this.visible=true;},hide(){this.visible=false;},onClick(fn){this.click=fn;}};
  const scope = '/net/p/session/';
  const location = new URL('https://bot.example.com/net/');
  const entries = [{state:null}];
  let historyIndex = 0;
  const history = {
    get state() { return entries[historyIndex].state; },
    replaceState(state) { entries[historyIndex].state = structuredClone(state); },
    pushState(state) { entries.splice(++historyIndex); entries.push({state:structuredClone(state)}); },
    back() { if (historyIndex) window.dispatch('popstate',{state:entries[--historyIndex].state}); }
  };
  function createFrame() {
    const frame = new Target();
    frame.style = {};
    frame.remove = () => {frame.removed=true;stage.children=stage.children.filter(item=>item!==frame);};
    const doc = new Target();
    const root = {};
    Object.defineProperty(frame,'offsetWidth',{get:()=>parseFloat(frame.style.width)||0});
    Object.defineProperty(root,'clientWidth',{get:()=>frame.offsetWidth});
    Object.defineProperty(root,'scrollWidth',{get:()=>Math.max(frame.offsetWidth,720)});
    doc.documentElement = root;
    doc.stylesheets = [];
    doc.querySelectorAll = () => doc.stylesheets;
    frame.contentDocument = doc;
    frame.contentWindow = {location:new URL(location),matchMedia:()=>({matches:true})};
    Object.defineProperty(frame,'src',{
      get:()=>frame.contentWindow.location.href,
      set:value=>{frame.contentWindow.location=new URL(value,location);}
    });
    return frame;
  }
  const response = {ok:true,body:{scope,path:scope+'maimai-mobile/home/'}};
  window.Telegram = {WebApp:{
    initData:options.initData ?? 'signed',ready(){readyCalls++;},expand(){},onEvent(){},BackButton:back
  }};
  const sandbox = {
    window,location,history,URL,console,
    document:{getElementById:id=>nodes[id],createElement:createFrame},
    requestAnimationFrame:fn=>rafs.push(fn),
    setTimeout:fn=>{const id=++timerId;timers.set(id,fn);return id;},
    clearTimeout:id=>timers.delete(id),
    MutationObserver:class {observe(){} disconnect(){}},
    fetch:async()=>({ok:response.ok,json:async()=>response.body}),
  };
  vm.createContext(sandbox);
  vm.runInContext(source,sandbox);
  async function settle() {
    for(let i=0;i<8;i++) {
      await Promise.resolve();
      const callbacks = rafs.splice(0);
      callbacks.forEach(fn=>fn());
    }
  }
  function stylesheet(frame) {
    const link = new Target();
    link.sheet = null;
    frame.contentDocument.stylesheets.push(link);
    return link;
  }
  function redirect(frame,path) {
    const doc=new Target();
    doc.documentElement=frame.contentDocument.documentElement;
    doc.stylesheets=[];
    doc.querySelectorAll=()=>doc.stylesheets;
    frame.contentDocument=doc;
    frame.contentWindow.location=new URL(path,location);
  }
  return {
    window,stage,nodes,scope,back,history,entries,response,settle,stylesheet,redirect,
    readyCalls:()=>readyCalls,
    visible:()=>stage.children.filter(frame=>frame.style.visibility==='visible'),
    ready:frame=>window.WahlapNet.ready(frame.contentWindow),
    timeout:()=>{[...timers.values()].forEach(fn=>fn());},
    navigate:(frame,path)=>window.WahlapNet.navigate(frame.contentWindow,path),
  };
}
module.exports = {createHarness};
