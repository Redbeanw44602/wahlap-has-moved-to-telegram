const app = window.Telegram?.WebApp;
const stage = document.getElementById('stage');
const errorBox = document.getElementById('error');
const errorMessage = document.getElementById('error-message');
const retry = document.getElementById('retry');
let active = null;
let pending = null;
let sessionScope = null;
let retryPath = null;
let appReady = false;
app?.expand();

function readyApp() {
  if (!appReady) { appReady = true; app?.ready(); }
}

function setStyle(node, key, value) {
  if (node.style[key] !== value) node.style[key] = value;
}

function fitFrame(entry) {
  const frame = entry.node;
  const width = stage.clientWidth;
  const height = stage.clientHeight;
  const doc = frame.contentDocument;
  const root = doc?.documentElement;
  if (!root || !width || !height) return;
  // Measure again at the window width only for a new document or window width.
  // Transforms do not affect scrollWidth, so resetting them is unnecessary.
  if (entry.fitDocument !== doc || entry.viewportWidth !== width) {
    entry.fitDocument = doc;
    entry.viewportWidth = width;
    setStyle(frame, 'width', width + 'px');
    setStyle(frame, 'height', height + 'px');
  }
  const scrollbar = Math.max(0, frame.offsetWidth - root.clientWidth);
  const contentWidth = Math.max(width, root.scrollWidth + scrollbar);
  const scale = Math.min(1, width / contentWidth);
  setStyle(frame, 'width', contentWidth + 'px');
  setStyle(frame, 'height', Math.ceil(height / scale) + 'px');
  setStyle(frame, 'transform', 'scale(' + scale + ')');
}

function scheduleFit(entry) {
  if (entry.fitPending || entry.removed) return;
  entry.fitPending = true;
  requestAnimationFrame(() => {
    entry.fitPending = false;
    if (!entry.removed) fitFrame(entry);
  });
}

function removeFrame(entry) {
  if (!entry) return;
  entry.removed = true;
  clearTimeout(entry.timer);
  entry.observer?.disconnect();
  entry.node.remove();
}

function updateBackButton() {
  const back = app?.BackButton;
  if (back) {
    if (history.state?.wahlapNet?.index > 0) back.show();
    else back.hide();
  }
}

function remember(path, mode) {
  const state = history.state || {};
  const index = state.wahlapNet?.index || 0;
  if (mode === 'push') {
    history.pushState({...state, wahlapNet:{path,index:index + 1}}, '');
  } else if (mode !== 'traverse') {
    history.replaceState({...state, wahlapNet:{path,index}}, '');
  }
  updateBackButton();
}

function showError(message, path = null) {
  removeFrame(pending);
  pending = null;
  retryPath = path;
  stage.removeAttribute('aria-busy');
  errorMessage.textContent = message;
  errorBox.hidden = false;
  readyApp();
}

function ownUrl(value) {
  const url = new URL(value, location.href);
  return url.origin === location.origin && sessionScope && url.pathname.startsWith(sessionScope)
    ? url : null;
}

function observeDocument(entry, doc) {
  if (entry.observedDocument === doc) return;
  entry.observedDocument = doc;
  entry.observer?.disconnect();
  doc.addEventListener('load', () => scheduleFit(entry), true);
  entry.observer = new MutationObserver(() => scheduleFit(entry));
  entry.observer.observe(doc.documentElement, {childList:true,subtree:true});
}

function pageReady(source) {
  const entry = [pending, active].find(item => item?.node.contentWindow === source);
  if (!entry || entry.removed) return;
  let doc;
  try {
    doc = entry.node.contentDocument;
    if (!doc?.documentElement || !ownUrl(source.location.href)) return;
  } catch {
    if (entry === pending) showError('页面加载失败，请重试。', entry.url);
    return;
  }
  observeDocument(entry, doc);
  if (entry === active) { scheduleFit(entry); return; }
  if (entry.revealingDocument === doc) return;
  entry.revealingDocument = doc;
  // DOMContentLoaded may precede stylesheet loading. Keep the old document
  // visible until the new document has its render-blocking stylesheets.
  const styles = [...doc.querySelectorAll('link[rel~="stylesheet"]')].filter(link =>
    !link.sheet && !link.disabled && (!link.media || source.matchMedia(link.media).matches));
  Promise.all(styles.map(link => new Promise(resolve => {
    link.addEventListener('load', resolve, {once:true});
    link.addEventListener('error', resolve, {once:true});
  }))).then(() => {
    if (pending !== entry || entry.removed || entry.node.contentDocument !== doc) return;
    fitFrame(entry);
    // Give layout and painting a frame before replacing the visible page.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (pending !== entry || entry.removed || entry.node.contentDocument !== doc) return;
      const previous = active;
      active = entry;
      pending = null;
      clearTimeout(entry.timer);
      entry.node.style.visibility = 'visible';
      entry.node.removeAttribute('aria-hidden');
      stage.removeAttribute('aria-busy');
      errorBox.hidden = true;
      remember(source.location.href, entry.historyMode);
      removeFrame(previous);
      readyApp();
    }));
  });
}

function loadPage(url, historyMode = 'push') {
  removeFrame(pending);
  errorBox.hidden = true;
  retryPath = null;
  if (active && historyMode === 'push') remember(active.node.contentWindow.location.href, 'replace');
  const frame = document.createElement('iframe');
  frame.className = 'net-frame';
  frame.title = 'maimai DX NET';
  frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads allow-popups');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.setAttribute('aria-hidden', 'true');
  frame.style.width = stage.clientWidth + 'px';
  frame.style.height = stage.clientHeight + 'px';
  frame.src = url;
  const entry = {node:frame, url, historyMode};
  pending = entry;
  frame.addEventListener('load', () => pageReady(frame.contentWindow));
  entry.timer = setTimeout(() => {
    if (pending === entry) showError('页面加载超时，请重试。', url);
  }, 35000);
  stage.setAttribute('aria-busy', 'true');
  stage.appendChild(frame);
}

window.WahlapNet = {
  ready: pageReady,
  native(source) {
    if (source === active?.node.contentWindow) {
      removeFrame(pending);
      pending = null;
      stage.removeAttribute('aria-busy');
    }
  },
  navigate(source, value) {
    if (source !== active?.node.contentWindow) return false;
    const url = ownUrl(value);
    if (!url) return false;
    const current = new URL(source.location.href);
    if (url.hash && url.pathname === current.pathname && url.search === current.search) return false;
    loadPage(url.href);
    return true;
  }
};

function resize() {
  if (active) scheduleFit(active);
  if (pending) scheduleFit(pending);
}
window.addEventListener('resize', resize);
window.addEventListener('popstate', event => {
  const path = event.state?.wahlapNet?.path;
  if (path && ownUrl(path)) loadPage(path, 'traverse');
});
app?.onEvent('viewportChanged', resize);
app?.BackButton?.onClick(() => history.back());

async function openNet() {
  errorBox.hidden = true;
  try {
    if (!app?.initData) throw new Error('请从 Telegram 机器人按钮打开此页面。');
    const response = await fetch(SESSION_ENDPOINT, {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({init_data:app.initData})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '暂时无法打开，请稍后重试。');
    sessionScope = result.scope;
    loadPage(result.path, 'initial');
  } catch (error) {
    showError(error.message);
  }
}
retry.addEventListener('click', () => {
  if (retryPath && ownUrl(retryPath)) loadPage(retryPath);
  else openNet();
});
openNet();
