(function() {
  const scope = PROXY_SCOPE;
  function proxyUrl(value) {
    const url = new URL(value, location.href);
    if (url.origin === location.origin && url.pathname !== scope && !url.pathname.startsWith(scope + '/')) {
      return scope + url.pathname + url.search + url.hash;
    }
    return value;
  }
  const open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    return open.call(this, method, proxyUrl(url), ...rest);
  };
  const fetch = window.fetch?.bind(window);
  if (fetch) window.fetch = function(input, options) {
    if (typeof input === 'string' || input instanceof URL) return fetch(proxyUrl(input), options);
    return fetch(new Request(new URL(proxyUrl(input.url), location.href), input), options);
  };

  function bridge() {
    try { return window.parent !== window ? window.parent.WahlapNet : null; }
    catch { return null; }
  }
  function notifyReady() { bridge()?.ready(window); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', notifyReady, {once:true});
  } else {
    Promise.resolve().then(notifyReady);
  }

  document.addEventListener('submit', event => {
    const form = event.target;
    if (form.action) form.action = proxyUrl(form.action);
    const button = event.submitter;
    if (button?.hasAttribute('formaction')) button.formAction = proxyUrl(button.formAction);
  }, true);
  document.addEventListener('click', event => {
    const link = event.target.closest?.('a[href]');
    if (link && !link.getAttribute('href')?.startsWith('#')) link.href = proxyUrl(link.href);
  }, true);

  // Let the site's validation and click handlers run before staging a normal
  // GET navigation. POST forms, downloads and explicit new windows stay native.
  window.addEventListener('click', event => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    if (link.getAttribute('href')?.startsWith('#')) return;
    if (bridge()?.navigate(window, link.href)) event.preventDefault();
  });
  window.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const form = event.target;
    const button = event.submitter;
    const target = button?.getAttribute('formtarget') ?? form.target;
    if (target && target !== '_self') return;
    const method = (button?.getAttribute('formmethod') || form.method || 'get').toLowerCase();
    if (method !== 'get') { bridge()?.native(window); return; }
    try {
      const action = button?.getAttribute('formaction') ? button.formAction : form.action;
      const url = new URL(proxyUrl(action), location.href);
      const data = new FormData(form, button || undefined);
      const query = new URLSearchParams();
      for (const [key, value] of data) query.append(key, typeof value === 'string' ? value : value.name);
      url.search = query.toString();
      if (bridge()?.navigate(window, url.href)) event.preventDefault();
    } catch {
      // Older WebViews may not support FormData's submitter argument.
      // Keep the original browser submission in that case.
    }
  });
})();
