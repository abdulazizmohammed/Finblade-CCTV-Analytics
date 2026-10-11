/* FinBlade AI chat panel.
 *
 * A button bottom-right and a slide-over panel. Questions go to THIS server
 * (/api/v1/chat/ask), which signs in to FinBlade and forwards; FinBlade's
 * agent then calls back into our MCP server for the actual figures. The
 * browser never sees a FinBlade credential.
 *
 * Self-hiding: it asks /api/v1/chat/status first and draws nothing at all
 * when the chat is not configured. A button that always fails is worse than
 * no button.
 *
 * Shared by the pages that want it (ops today). Include after apikey.js —
 * that wrapper is what puts the operator's key on these calls — and call
 * fbChatInit() when the page is ready.
 */
(function () {
  var SESSION = null;          // FinBlade's thread id, so follow-ups keep context
  var BUSY = false;
  var CTX = function () { return ''; };

  function el(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Image links in an answer, rendered as pictures.
  //
  // ONLY OUR OWN, and only the two routes that serve one frame: an answer
  // arrives through a third party, and <img src> pointing anywhere it liked
  // would be a way to make this page fetch from an arbitrary host. Same
  // origin as the API, and a path we recognise, or it stays a link.
  //
  // The agent sometimes prefixes a link with "MEDIA:" to mark it as something
  // to display; that marker is stripped and the link rendered.
  var IMG_PATH = /\/api\/v1\/(incidents\/[^/\s]+\/frame|search\/sightings\/[^/\s]+\/crop)(\?|$)/;

  function imageUrls(text) {
    var out = [];
    String(text || '').replace(/(?:MEDIA:)?(https?:\/\/[^\s<>"')\]]+)/gi, function (_, url) {
      // A sentence ends after the link often enough to matter.
      url = url.replace(/[.,;:!?]+$/, '');
      try {
        var u = new URL(url);
        if (u.origin === new URL(window.API_BASE).origin && IMG_PATH.test(u.pathname + u.search)) {
          out.push(url);
        }
      } catch (e) { /* not a URL we can parse: leave it as text */ }
      return url;
    });
    return out;
  }

  // The answer is plain text from an LLM. It is INSERTED AS TEXT, never as
  // HTML: it passes through FinBlade and is not ours to trust, and a chat
  // bubble is not worth a script-injection hole. Only the paragraph breaks
  // and our own image links become markup.
  function bubble(role, text, pending) {
    var wrap = document.createElement('div');
    wrap.className = 'fbc-msg fbc-' + role + (pending ? ' fbc-pending' : '');
    var imgs = role === 'ai' ? imageUrls(text) : [];
    // "MEDIA:" is a marker for the renderer, not something to read.
    var shown = String(text || '').replace(/\bMEDIA:(?=https?:\/\/)/gi, '');
    shown.split(/\n{2,}/).forEach(function (para) {
      if (!para.trim()) return;
      var p = document.createElement('p');
      p.textContent = para;
      wrap.appendChild(p);
    });
    imgs.forEach(function (url) {
      var a = document.createElement('a');
      a.href = url; a.target = '_blank'; a.rel = 'noreferrer noopener';
      a.className = 'fbc-shot';
      var img = document.createElement('img');
      img.src = url; img.alt = 'Incident frame'; img.loading = 'lazy';
      // A link that has expired must say so rather than showing a broken icon.
      img.addEventListener('error', function () {
        a.replaceWith(Object.assign(document.createElement('p'), {
          className: 'fbc-gone',
          textContent: 'That image link has expired — ask again for a fresh one.'
        }));
      });
      a.appendChild(img);
      wrap.appendChild(a);
    });
    el('fbcLog').appendChild(wrap);
    el('fbcLog').scrollTop = el('fbcLog').scrollHeight;
    return wrap;
  }

  function setBusy(on) {
    BUSY = on;
    el('fbcSend').disabled = on;
    el('fbcAsk').disabled = on;
    el('fbcPanel').classList.toggle('busy', on);
  }

  async function ask(q) {
    if (BUSY || !q.trim()) return;
    bubble('me', q);
    el('fbcAsk').value = '';
    setBusy(true);
    var thinking = bubble('ai', 'Thinking…', true);
    try {
      var r = await fetch(window.API_BASE + '/api/v1/chat/ask', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: q, session_id: SESSION, context: CTX() })
      });
      var d = await r.json().catch(function () { return {}; });
      thinking.remove();
      if (r.ok && d.answer) {
        SESSION = d.session_id || SESSION;
        bubble('ai', d.answer);
      } else if (r.status === 504 || d.pending) {
        SESSION = d.session_id || SESSION;
        bubble('ai', 'FinBlade is still working on this one. Ask again in a moment, '
                   + 'or narrow the question — a single camera or a shorter window.');
      } else {
        // The server's message is already written for an operator ("rejected
        // the username or password", "cannot reach FinBlade"); show it rather
        // than a generic failure, because it says what to fix.
        bubble('err', (d.error || ('HTTP ' + r.status))
                    + (d.detail ? '\n\n' + d.detail : ''));
      }
    } catch (e) {
      thinking.remove();
      bubble('err', 'Could not reach this server. The chat needs the CCTV API, not just the page.');
    }
    setBusy(false);
    el('fbcAsk').focus();
  }

  function draw() {
    var host = document.createElement('div');
    host.id = 'fbcHost';
    host.innerHTML =
      '<button id="fbcOpen" class="fbc-open" title="Ask FinBlade AI about this system"'
      + ' aria-label="Ask FinBlade AI">Ask AI</button>'
      + '<section id="fbcPanel" class="fbc-panel" hidden aria-label="FinBlade AI chat">'
      + '  <header class="fbc-head"><p class="eyebrow">FinBlade AI</p>'
      + '    <span class="fbc-state" id="fbcState">Online</span>'
      + '    <button class="fbc-x" id="fbcNew" title="New chat — forget this thread"'
      + '      aria-label="New chat">⟳</button>'
      + '    <button class="fbc-x" id="fbcClose" aria-label="Close">✕</button></header>'
      + '  <div class="fbc-log" id="fbcLog"></div>'
      + '  <div class="fbc-ex" id="fbcEx"></div>'
      + '  <form class="fbc-in" id="fbcForm">'
      + '    <textarea id="fbcAsk" rows="2" placeholder="Ask about cameras, zones, alerts, occupancy…"></textarea>'
      + '    <button class="fb-btn fb-btn--primary" id="fbcSend" type="submit">Ask</button>'
      + '  </form>'
      + '  <p class="fbc-foot">Answers come from this system’s own data through the '
      + '  MCP server. Figures carry the same caveats as the dashboard — check a '
      + '  number before acting on it.</p>'
      + '</section>';
    document.body.appendChild(host);

    el('fbcOpen').addEventListener('click', function () {
      var p = el('fbcPanel');
      p.hidden = !p.hidden;
      if (!p.hidden) { el('fbcAsk').focus(); }
    });
    el('fbcClose').addEventListener('click', function () { el('fbcPanel').hidden = true; });
    // A conversation accumulates context, which is what makes "and yesterday?"
    // work and also what makes a long thread drift. Dropping the id starts a
    // fresh one on the next question.
    el('fbcNew').addEventListener('click', function () {
      SESSION = null;
      el('fbcLog').innerHTML = '';
      bubble('ai', 'New chat. Previous questions are forgotten.');
      el('fbcAsk').focus();
    });
    el('fbcForm').addEventListener('submit', function (e) {
      e.preventDefault(); ask(el('fbcAsk').value);
    });
    // Enter sends, Shift+Enter is a newline — a chat box, not a form field.
    el('fbcAsk').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(el('fbcAsk').value); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !el('fbcPanel').hidden) el('fbcPanel').hidden = true;
    });

    // Openers, so the first question is never a blank page. They are the
    // questions this system can actually answer well.
    var examples = ['How many people are in the building?',
                    'Any open alerts?',
                    'Which zones are busiest right now?',
                    'Are all cameras online?'];
    el('fbcEx').innerHTML = examples.map(function (q) {
      return '<button class="fbc-eg" type="button">' + esc(q) + '</button>';
    }).join('');
    el('fbcEx').querySelectorAll('.fbc-eg').forEach(function (b) {
      b.addEventListener('click', function () { ask(b.textContent); });
    });

    bubble('ai', 'Ask me about this site — occupancy, zones, alerts, cameras, '
               + 'vehicles or a person’s description. I read the live system, '
               + 'not a copy of it.');
  }

  window.fbChatInit = async function (opts) {
    opts = opts || {};
    window.API_BASE = opts.api || window.location.origin;
    if (typeof opts.context === 'function') CTX = opts.context;
    try {
      var s = await fetch(window.API_BASE + '/api/v1/chat/status').then(function (r) {
        return r.ok ? r.json() : null;
      });
      // Not configured, or no permission to use it: draw nothing.
      if (!s || !s.enabled) return false;
      draw();
      if (s.auth && !s.auth.ok) {
        el('fbcState').textContent = 'Sign-in failed';
        el('fbcState').classList.add('bad');
        bubble('err', 'This server cannot sign in to FinBlade: ' + (s.auth.detail || '')
                    + '\n\nThe chat will not answer until that is fixed in .env.');
      } else if (s.warning) {
        el('fbcState').textContent = 'No MCP service';
        el('fbcState').classList.add('bad');
        bubble('err', s.warning);
      }
      return true;
    } catch (e) { return false; }
  };
})();
