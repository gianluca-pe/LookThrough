/* Progressive waiting feedback and selection. Financial values come from the server. */
(function () {
  'use strict';
  var form = document.getElementById('mc-form');
  if (form) {
    var pathCount = Number(form.dataset.pathCount);
    var status = document.getElementById('mc-run-status');
    var fields = Array.from(form.querySelectorAll('input:not([type=hidden])'));
    var original = fields.map(function (field) { return field.value; }).join('|');
    var button = document.getElementById('run_comparison');
    var buttonLabel = button.value;
    var waiting = document.createElement('div');
    waiting.id = 'mc-waiting';
    waiting.hidden = true;
    var progress = document.createElement('div');
    progress.className = 'mc-progress';
    progress.setAttribute('role', 'progressbar');
    progress.setAttribute('aria-label', 'Market paths completed for both allocations');
    progress.setAttribute('aria-valuemin', '0');
    progress.setAttribute('aria-valuemax', String(pathCount));
    progress.appendChild(document.createElement('span'));
    var elapsed = document.createElement('p');
    elapsed.className = 'hint';
    var count = document.createElement('p');
    count.setAttribute('aria-hidden', 'true');
    // Keep the ticking clock outside the live region to avoid announcing every second.
    waiting.appendChild(count);
    waiting.appendChild(progress);
    waiting.appendChild(elapsed);
    status.after(waiting);
    var running = false;
    var timer;
    var controller;
    function reset() {
      window.clearInterval(timer);
      running = false;
      waiting.hidden = true;
      button.disabled = false;
      button.value = buttonLabel;
      status.classList.remove('sr-only');
      fields.forEach(function (field) { field.readOnly = false; });
    }
    form.addEventListener('input', function () {
      if (running) return;
      var changed = fields.map(function (field) { return field.value; }).join('|') !== original;
      status.textContent = changed && document.getElementById('mc-results') ? 'Inputs changed. Results below belong to the previous run; run the comparison to update them.' : '';
    });
    form.addEventListener('submit', async function (event) {
      if (!(pathCount > 0) || !window.fetch || !window.ReadableStream || !window.TextDecoder || !window.AbortController) return;
      event.preventDefault();
      if (running) return;
      running = true;
      button.disabled = true;
      button.value = 'Calculating…';
      fields.forEach(function (field) { field.readOnly = true; });
      status.textContent = 'Starting comparison across ' + pathCount.toLocaleString('en-US') + ' market paths. Results will appear automatically.';
      status.classList.add('sr-only');
      count.textContent = 'Preparing comparison…';
      progress.removeAttribute('aria-valuenow');
      progress.firstChild.style.width = '0%';
      waiting.hidden = false;
      var started = performance.now();
      function tick() {
        var seconds = Math.floor((performance.now() - started) / 1000);
        elapsed.textContent = seconds + ' seconds elapsed. Keep this page open; larger portfolios and longer horizons take longer.';
      }
      tick();
      timer = window.setInterval(tick, 1000);
      waiting.scrollIntoView({block: 'nearest', behavior: 'instant'});
      controller = new AbortController();
      try {
        var response = await fetch(form.action, {method: 'POST', body: new FormData(form),
          headers: {'X-Monte-Carlo-Progress': '1'}, signal: controller.signal});
        if (!(response.headers.get('Content-Type') || '').includes('application/x-ndjson')) {
          // Ordinary POST preserves server-rendered validation and linked error focus.
          HTMLFormElement.prototype.submit.call(form);
          return;
        }
        // Streaming starts only after the server accepts the current inputs.
        form.querySelectorAll('.error-summary, .field-error').forEach(function (error) { error.remove(); });
        fields.forEach(function (field) {
          field.removeAttribute('aria-invalid');
          var descriptions = (field.getAttribute('aria-describedby') || '').split(' ').filter(function (id) { return !!document.getElementById(id); });
          if (descriptions.length) field.setAttribute('aria-describedby', descriptions.join(' '));
          else field.removeAttribute('aria-describedby');
        });
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var lastAnnouncement = -1;
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) throw new Error('The connection ended before results were ready. Please try again.');
          buffer += decoder.decode(chunk.value, {stream: true});
          var lines = buffer.split('\n');
          buffer = lines.pop();
          for (var line of lines) {
            if (!line) continue;
            var update = JSON.parse(line);
            if (update.error) throw new Error(update.error);
            if (update.url) {
              window.clearInterval(timer);
              status.textContent = 'Comparison complete. Opening results…';
              count.textContent = status.textContent;
              window.location.assign(update.url);
              return;
            }
            progress.setAttribute('aria-valuenow', String(update.completed));
            progress.setAttribute('aria-valuemax', String(update.total));
            var description = update.completed.toLocaleString('en-US') + ' of ' + update.total.toLocaleString('en-US') + ' market paths completed for both allocations';
            progress.setAttribute('aria-valuetext', description);
            count.textContent = description + (update.completed === update.total ? '. Preparing results…' : '.');
            progress.firstChild.style.width = (update.completed / update.total * 100) + '%';
            // Announce meaningful milestones, not every network update.
            if (Math.floor(update.completed / update.total * 10) !== lastAnnouncement) {
              lastAnnouncement = Math.floor(update.completed / update.total * 10);
              status.textContent = description + (update.completed === update.total ? '. Preparing results…' : '.');
            }
          }
        }
      } catch (error) {
        reset();
        status.textContent = error.name === 'TypeError' ? 'The connection was interrupted. Please try again.' : error.message;
        if (document.getElementById('mc-results')) status.textContent += ' Results below belong to the previous run.';
        button.focus();
      }
    });
    window.addEventListener('pageshow', function (event) {
      if (!event.persisted) return;
      reset();
      status.textContent = '';
    });
    window.addEventListener('pagehide', function () {
      window.clearInterval(timer);
      if (controller) controller.abort();
    });
  }
  var root = document.getElementById('mc-charts');
  if (!root || typeof Chart === 'undefined') return;
  var data = JSON.parse(root.dataset.values);
  var picker = document.getElementById('mc-year');
  var age = document.getElementById('mc-age');
  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var accent = getComputedStyle(document.documentElement).getPropertyValue('--color-accent').trim() || '#1a5c45';
  function select(index) {
    picker.value = index;
    age.textContent = data.allocations[0].ages[index];
    picker.setAttribute('aria-valuetext', 'Age ' + age.textContent);
    data.allocations.forEach(function (allocation, i) {
      var row = allocation.readouts[index];
      document.getElementById('mc-readout-' + i).textContent = 'Age ' + allocation.ages[index] + ' · median: ' + row.real.median + ' today / ' + row.nominal.median + ' nominal. 10th–90th: ' + row.real.p10 + '–' + row.real.p90 + ' today / ' + row.nominal.p10 + '–' + row.nominal.p90 + ' nominal.';
    });
  }
  root.querySelectorAll('canvas').forEach(function (canvas, index) {
    var allocation = data.allocations[index];
    new Chart(canvas, {
      type: 'line',
      data: {labels: allocation.ages, datasets: [
        {label: '10th percentile', data: allocation.p10.map(Number), borderWidth: 0, pointRadius: 0, backgroundColor: 'rgba(26,92,69,.14)'},
        {label: '90th percentile', data: allocation.p90.map(Number), borderWidth: 0, pointRadius: 0, fill: '-1', backgroundColor: 'rgba(26,92,69,.14)'},
        {label: 'Median', data: allocation.median.map(Number), borderColor: accent, borderWidth: 2, pointRadius: 0},
        {label: 'Final-age goal', data: allocation.ages.map(function () { return Number(data.goal); }), borderColor: '#9b7357', borderDash: [6,4], borderWidth: 2, pointRadius: 0}
      ]},
      options: {locale: 'en-US', responsive: true, maintainAspectRatio: false, animation: reduceMotion ? false : {duration: 200},
        interaction: {mode: 'index', intersect: false},
        scales: {y: {min: Number(data.minimum), max: Number(data.maximum), title: {display: true, text: 'USD · today’s money'}}, x: {title: {display: true, text: 'Age at year-end'}}},
        plugins: {legend: {display: false}, tooltip: {callbacks: {label: function (context) {
          return context.dataset.label + ': USD ' + context.parsed.y.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        }}}},
        onClick: function (event, points) { if (points.length) select(points[0].index); }
      }
    });
  });
  picker.parentElement.hidden = false;
  picker.addEventListener('input', function () { select(Number(picker.value)); });
  select(Number(picker.value));
})();
