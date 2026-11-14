(function () {
  var card = document.getElementById('progress-card');
  if (!card) return;
  var id = card.dataset.reportId;
  var phaseEl = document.getElementById('phase-line');
  var detailEl = document.getElementById('detail-line');
  var fill = document.getElementById('bar-fill');
  var WIDTHS = { starting: 5, searching: 25, thinking: 55, writing: 85, done: 100 };
  var es = new EventSource('/reports/' + id + '/stream');
  es.addEventListener('phase', function (ev) {
    var d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    var phase = d.phase || 'starting';
    var line;
    if (phase === 'searching') {
      line = 'Searching\u2026 ' + (d.tool_calls || 0) + ' tool call' + ((d.tool_calls || 0) === 1 ? '' : 's');
      if (d.searches || d.fetches) {
        line = 'Searching\u2026 ' + (d.searches || 0) + ' searches, ' + (d.fetches || 0) +
               ' page fetches (' + (d.tool_calls || 0) + ' tool calls)';
      }
    } else if (phase === 'thinking') {
      line = 'Thinking\u2026 (deep reasoning over the sources)';
    } else if (phase === 'writing') {
      line = 'Writing report\u2026 ' + (d.text_chars || 0) + ' characters so far';
    } else {
      line = 'Starting deep search\u2026';
    }
    phaseEl.textContent = line;
    if (d.thinking_head) {
      detailEl.textContent = d.thinking_head;
    } else if (d.queries && d.queries.length) {
      detailEl.textContent = 'Queries: ' + d.queries.join(' \u00b7 ');
    }
    fill.style.width = (WIDTHS[phase] || 10) + '%';
  });
  es.addEventListener('done', function () {
    es.close();
    location.reload();
  });
  es.onerror = function () { /* EventSource reconnects automatically */ };
  // Safety net: a deep report should finish within ~15 minutes; catch stragglers.
  setTimeout(function () { location.reload(); }, 15 * 60 * 1000);
})();
