import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from wahlap_bot.webapp import _bootstrap, rewrite_text

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="JavaScript checks need Node.js"
)


def run_script(source, checks):
    checks = checks.replace(
        "DOM_HARNESS", json.dumps(str(Path(__file__).with_name("webapp_dom.js")))
    )
    result = subprocess.run(
        ["node", "-e", checks.replace("SCRIPT_SOURCE", json.dumps(source))],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def inline_script(html):
    return re.search(r"<script>(.*?)</script>", html, re.DOTALL)[1]


def test_iframe_scaling_geometry_for_narrow_and_wide_windows():
    result = run_script(
        inline_script(_bootstrap("/net/")),
        """
const vm = require('node:vm');
let fixedWidth = 720, scrollbar = 0;
const stage = {clientWidth:360,clientHeight:640};
let writes = 0;
const style = new Proxy({}, {set:(target,key,value)=>{writes++;target[key]=value;return true;}});
const frame = {style};
Object.defineProperty(frame, 'offsetWidth', {get:()=>parseFloat(frame.style.width)});
const root = {};
Object.defineProperty(root, 'clientWidth', {get:()=>frame.offsetWidth-scrollbar});
Object.defineProperty(root, 'scrollWidth', {get:()=>Math.max(root.clientWidth,fixedWidth)});
frame.contentDocument = {documentElement:root};
const nodes = {stage,error:{hidden:true},'error-message':{},retry:{addEventListener(){}}};
const sandbox = {
  window:{Telegram:{WebApp:{initData:'signed',ready(){},expand(){},onEvent(){}}},addEventListener(){}},
  document:{getElementById:id=>nodes[id]},
  requestAnimationFrame(){},fetch:()=>new Promise(()=>{}),URL,console,testEntry:{node:frame}
};
vm.createContext(sandbox);
vm.runInContext(SCRIPT_SOURCE, sandbox);
const cases = [];
for (scrollbar of [0,15]) {
  for (const width of [320,360,480,800,360]) {
    stage.clientWidth = width;
    vm.runInContext('fitFrame(testEntry)',sandbox);
    const scale = Number(frame.style.transform.match(/scale\\((.*)\\)/)[1]);
    const before = writes;
    vm.runInContext('fitFrame(testEntry)',sandbox);
    cases.push({width,scrollbar,extraWrites:writes-before,renderedWidth:frame.offsetWidth*scale,
      renderedHeight:parseFloat(frame.style.height)*scale,
      innerWidth:root.clientWidth,contentWidth:root.scrollWidth});
  }
}
console.log(JSON.stringify(cases));
""",
    )
    assert len(result) == 10
    for case in result:
        assert case["renderedWidth"] == pytest.approx(case["width"])
        assert case["renderedHeight"] >= 640
        assert case["innerWidth"] >= case["contentWidth"]
        assert case["extraWrites"] == 0


def test_injected_script_proxies_fetch_xhr_and_user_navigation():
    result = run_script(
        inline_script(rewrite_text("<html><head></head></html>", "/net/p/session", is_html=True)),
        """
const vm = require('node:vm');
const calls = [], events = {};
function dispatch(type,event) {for(const fn of events[type]||[]) fn(event);}
function XMLHttpRequest() {}
XMLHttpRequest.prototype.open = function(...args) {this.args=args;};
const sandbox = {
  window:{fetch:(...args)=>{calls.push(args);return Promise.resolve();},
    addEventListener:(type,fn)=>{(events[type]||=[]).push(fn);}},
  location:{origin:'https://bot.example.com',href:'https://bot.example.com/net/p/session/maimai-mobile/home/'},
  document:{readyState:'loading',addEventListener:(type,fn)=>{(events[type]||=[]).push(fn);}},
  XMLHttpRequest,URL,Request
};
vm.createContext(sandbox);
vm.runInContext(SCRIPT_SOURCE,sandbox);
const xhr = new XMLHttpRequest();
xhr.open('POST','/maimai-mobile/data/?x=1');
sandbox.window.fetch('/maimai-mobile/a');
sandbox.window.fetch('https://outside.example/a');
sandbox.window.fetch(new Request('https://bot.example.com/maimai-mobile/post/',{method:'POST',body:'choice=1'}));
const form = {action:'https://bot.example.com/maimai-mobile/submit/',method:'post'};
dispatch('submit',{target:form});
const link = {href:'https://bot.example.com/maimai-mobile/record/',hasAttribute:()=>false,getAttribute:()=>null};
dispatch('click',{target:{closest:()=>link},button:0});
console.log(JSON.stringify({xhr:xhr.args,fetch:calls.slice(0,2).map(args=>args[0]),
  request:{url:calls[2][0].url,method:calls[2][0].method},form:form.action,link:link.href}));
""",
    )
    assert result["xhr"] == ["POST", "/net/p/session/maimai-mobile/data/?x=1"]
    assert result["fetch"] == ["/net/p/session/maimai-mobile/a", "https://outside.example/a"]
    assert result["request"] == {
        "url": "https://bot.example.com/net/p/session/maimai-mobile/post/",
        "method": "POST",
    }
    assert result["form"] == "/net/p/session/maimai-mobile/submit/"
    assert result["link"] == "/net/p/session/maimai-mobile/record/"


def check_navigation(checks):
    result = run_script(
        inline_script(_bootstrap("/net/")),
        """
const assert = require('node:assert/strict');
const {createHarness} = require(DOM_HARNESS);
(async () => {
const h = createHarness(SCRIPT_SOURCE);
await h.settle();
const initial = h.stage.children[0];
"""
        + checks
        + """
console.log(JSON.stringify({passed:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""",
    )
    assert result == {"passed": True}


def test_startup_is_silent_and_native_ready_waits_for_the_first_painted_page():
    check_navigation("""
assert.equal(h.nodes.error.hidden,true);
assert.equal(h.readyCalls(),0);
assert.equal(h.visible().length,0);
h.ready(initial);
await h.settle();
assert.deepEqual(h.visible(),[initial]);
assert.equal(h.readyCalls(),1);
assert.equal(h.entries.length,1);
""")


def test_navigation_keeps_previous_page_visible_until_styles_and_layout_are_ready():
    check_navigation("""
h.ready(initial); await h.settle();
assert.equal(h.navigate(initial,h.scope+'maimai-mobile/record/'),true);
const next = h.stage.children[1];
const css = h.stylesheet(next);
h.ready(next); await h.settle();
assert.deepEqual(h.visible(),[initial]);
assert.equal(initial.removed,undefined);
css.sheet = {}; css.dispatch('load'); await h.settle();
assert.deepEqual(h.visible(),[next]);
assert.equal(initial.removed,true);
assert.equal(h.stage.children.length,1);
assert.equal(h.readyCalls(),1);
assert.equal(h.entries.length,2);
assert.equal(h.back.visible,true);
""")


def test_consecutive_clicks_cancel_old_loads_and_ignore_late_ready_callbacks():
    check_navigation("""
h.ready(initial); await h.settle();
h.navigate(initial,h.scope+'maimai-mobile/first/');
const first = h.stage.children[1];
h.ready(first);
await Promise.resolve();
h.navigate(initial,h.scope+'maimai-mobile/second/');
const second = h.stage.children[1];
h.ready(first); await h.settle();
assert.deepEqual(h.visible(),[initial]);
h.ready(second); await h.settle();
assert.deepEqual(h.visible(),[second]);
assert.equal(first.removed,true);
assert.equal(h.entries.length,2);
assert.match(h.history.state.wahlapNet.path,/second/);
assert.equal(h.navigate(first,h.scope+'maimai-mobile/late/'),false);
assert.equal(h.navigate(second,'https://evil.example/net/p/session/'),false);
assert.equal(h.navigate(second,second.src+'#anchor'),false);
""")


def test_back_navigation_loads_recorded_page_without_adding_history_entries():
    check_navigation("""
h.ready(initial); await h.settle();
let current = initial;
for(const name of ['record','friend']) {
  h.navigate(current,h.scope+'maimai-mobile/'+name+'/');
  current=h.stage.children[1];h.ready(current);await h.settle();
}
assert.equal(h.entries.length,3);
assert.equal(h.history.state.wahlapNet.index,2);
h.back.click();
assert.deepEqual(h.visible(),[current]);
let previous = h.stage.children[1];
assert.match(previous.src,/record/);
h.ready(previous);await h.settle();
assert.deepEqual(h.visible(),[previous]);
assert.equal(h.entries.length,3);
assert.equal(h.history.state.wahlapNet.index,1);
h.back.click();previous=h.stage.children[1];
h.ready(previous);await h.settle();
assert.match(previous.src,/home/);
assert.equal(h.back.visible,false);
""")


def test_navigation_timeout_keeps_current_page_and_retry_does_not_reauthenticate():
    check_navigation("""
h.ready(initial);await h.settle();
h.navigate(initial,h.scope+'maimai-mobile/record/');
const abandoned = h.stage.children[1];
h.timeout();await h.settle();
assert.deepEqual(h.visible(),[initial]);
assert.equal(abandoned.removed,true);
assert.equal(h.nodes.error.hidden,false);
h.nodes.retry.dispatch('click');await h.settle();
const retried = h.stage.children[1];
h.ready(retried);await h.settle();
assert.match(retried.src,/record/);
assert.deepEqual(h.visible(),[retried]);
assert.equal(h.nodes.error.hidden,true);
""")


def test_native_submission_cancels_pending_get_without_replacing_current_frame():
    check_navigation("""
h.ready(initial);await h.settle();
h.navigate(initial,h.scope+'maimai-mobile/record/');
const next = h.stage.children[1];
h.window.WahlapNet.native(initial.contentWindow);
h.ready(next);await h.settle();
assert.equal(next.removed,true);
assert.deepEqual(h.visible(),[initial]);
assert.equal(h.stage.children.length,1);
""")


def test_startup_error_is_visible_and_can_retry_without_a_loading_message():
    result = run_script(
        inline_script(_bootstrap("/net/")),
        """
const assert=require('node:assert/strict');
const {createHarness}=require(DOM_HARNESS);
(async()=>{
  const h=createHarness(SCRIPT_SOURCE,{initData:''});await h.settle();
  assert.equal(h.stage.children.length,0);
  assert.equal(h.nodes.error.hidden,false);
  assert.match(h.nodes['error-message'].textContent,/Telegram/);
  assert.equal(h.readyCalls(),1);
  h.window.Telegram.WebApp.initData='signed';h.nodes.retry.dispatch('click');await h.settle();
  h.ready(h.stage.children[0]);await h.settle();
  assert.equal(h.nodes.error.hidden,true);
  assert.equal(h.readyCalls(),1);
  console.log(JSON.stringify({passed:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""",
    )
    assert result == {"passed": True}


def test_script_redirect_during_loading_cannot_reveal_an_unready_document():
    check_navigation(
        """
h.ready(initial);await h.settle();
h.navigate(initial,h.scope+'maimai-mobile/intermediate/');
const next=h.stage.children[1];
const css=h.stylesheet(next);
h.ready(next);
h.redirect(next,h.scope+'maimai-mobile/final/');
css.sheet={};css.dispatch('load');await h.settle();
assert.deepEqual(h.visible(),[initial]);
h.ready(next);await h.settle();
assert.deepEqual(h.visible(),[next]);
assert.match(h.history.state.wahlapNet.path,/final/);
"""
    )


def test_navigation_bridge_stages_get_but_preserves_native_post_and_special_clicks():
    result = run_script(
        inline_script(rewrite_text("<html><head></head></html>", "/net/p/session", is_html=True)),
        """
const vm=require('node:vm'),assert=require('node:assert/strict');
const events={},windowEvents={},navigations=[];
let nativeCalls=0,readyCalls=0;
const parent={WahlapNet:{
  navigate:(source,url)=>{navigations.push(url);return true;},
  native:()=>nativeCalls++,ready:()=>readyCalls++
}};
function XMLHttpRequest(){}
XMLHttpRequest.prototype.open=function(){};
const location=new URL('https://bot.example.com/net/p/session/maimai-mobile/home/');
const sandbox={
  window:{parent,addEventListener:(type,fn)=>{(windowEvents[type]||=[]).push(fn);}},
  location,URL,URLSearchParams,XMLHttpRequest,
  document:{readyState:'loading',addEventListener:(type,fn)=>{(events[type]||=[]).push(fn);}},
  FormData:class {
    constructor(form,button) {
      this.values=[...form.values];
      if(button?.name)this.values.push([button.name,button.value]);
    }
    [Symbol.iterator]() {return this.values[Symbol.iterator]();}
  }
};
vm.createContext(sandbox);vm.runInContext(SCRIPT_SOURCE,sandbox);
function dispatch(type,extra={}) {
  const event={button:0,defaultPrevented:false,
    preventDefault(){this.defaultPrevented=true;},...extra};
  for(const fn of events[type]||[])fn(event);
  for(const fn of windowEvents[type]||[])fn(event);
  return event;
}
function link(path,extra={}) {
  let raw=path;
  return {get href(){return new URL(raw,location).href;},set href(value){raw=value;},
    hasAttribute:()=>false,getAttribute:()=>raw,...extra};
}
dispatch('DOMContentLoaded');assert.equal(readyCalls,1);
const normal=link('/maimai-mobile/record/');
assert.equal(dispatch('click',{target:{closest:()=>normal}}).defaultPrevented,true);
assert.equal(navigations.at(-1),'https://bot.example.com/net/p/session/maimai-mobile/record/');
// Delegated handlers installed by the site after our script can still cancel.
const beforeCancel=navigations.length;
events.click.push(event=>event.preventDefault());
dispatch('click',{target:{closest:()=>normal}});
assert.equal(navigations.length,beforeCancel);
events.click.pop();
const modified=[{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},
  {button:1},{defaultPrevented:true}];
for(const extra of modified) {
  const before=navigations.length;
  dispatch('click',{target:{closest:()=>normal},...extra});
  assert.equal(navigations.length,before);
}
const specials=[link('#anchor'),link('/a',{target:'_blank'}),
  link('/a',{hasAttribute:()=>true})];
for(const special of specials) {
  assert.equal(dispatch('click',{target:{closest:()=>special}}).defaultPrevented,false);
}
const post={action:'/maimai-mobile/submit/',method:'post'};
assert.equal(dispatch('submit',{target:post}).defaultPrevented,false);
assert.equal(nativeCalls,1);
const get={action:'/maimai-mobile/search/?old=1',method:'get',
  values:[['genre','1'],['genre','2'],['search','maimai + DX']]};
const button={name:'submit',value:'1',hasAttribute:()=>false,getAttribute:()=>null};
assert.equal(dispatch('submit',{target:get,submitter:button}).defaultPrevented,true);
const target=new URL(navigations.at(-1));
assert.equal(target.pathname,'/net/p/session/maimai-mobile/search/');
assert.deepEqual(target.searchParams.getAll('genre'),['1','2']);
assert.equal(target.searchParams.get('search'),'maimai + DX');
assert.equal(target.searchParams.get('submit'),'1');
assert.equal(target.searchParams.has('old'),false);
sandbox.FormData=class{constructor(){throw new Error('unsupported');}};
assert.equal(dispatch('submit',{target:get,submitter:button}).defaultPrevented,false);
console.log(JSON.stringify({passed:true}));
""",
    )
    assert result == {"passed": True}
