/* Shared API-key handling for the FinBlade CCTV pages.
 *
 * When the API has FINBLADE_API_KEY set, every /api/v1 call needs the key. The
 * pages themselves are served without it (otherwise nothing could bootstrap),
 * so this asks for it once and keeps it in localStorage.
 *
 * It works by wrapping window.fetch rather than editing every call site: there
 * are dozens across five pages, and one missed call would look like an
 * intermittent failure rather than a missing header.
 *
 * The MJPEG feed cannot use a header at all — it is an <img src> and browsers
 * do not let you set headers on those — so fbStreamUrl() appends ?key=, which
 * the server accepts on that route only.
 */
(function () {
  var KEY = 'finblade_api_key';

  window.fbGetKey = function () {
    try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; }
  };
  window.fbSetKey = function (k) {
    try { k ? localStorage.setItem(KEY, k) : localStorage.removeItem(KEY); }
    catch (e) {}
  };

  // Append the key to a stream URL. Header auth is impossible for <img>.
  window.fbStreamUrl = function (url) {
    var k = window.fbGetKey();
    if (!k) return url;
    return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'key=' + encodeURIComponent(k);
  };

  var nativeFetch = window.fetch.bind(window);
  var prompting = false;

  window.fetch = function (input, init) {
    init = init || {};
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    var k = window.fbGetKey();
    if (k && url.indexOf('/api/') >= 0) {
      var h = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
      if (!h.has('Authorization')) h.set('Authorization', 'Bearer ' + k);
      init = Object.assign({}, init, { headers: h });
    }
    return nativeFetch(input, init).then(function (r) {
      // 401 means the key is missing or wrong. Ask once, then reload — retrying
      // silently would leave the page half-populated with stale data.
      if (r && r.status === 401 && !prompting) {
        prompting = true;
        var entered = window.prompt(
          'This FinBlade API requires a key.\nPaste it to continue:',
          window.fbGetKey());
        if (entered) { window.fbSetKey(entered.trim()); location.reload(); }
        else { prompting = false; }
      }
      return r;
    });
  };
})();
