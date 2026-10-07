(function () {
  'use strict';
  var message = document.getElementById('retirement-review-status');
  if (!message) return;
  var dirty = false;
  document.querySelectorAll('#affordability-form, #lifestyle-form').forEach(function (form) {
    form.addEventListener('input', function (event) {
      if (event.target.type === 'hidden') return;
      dirty = true;
      message.hidden = false;
      var save = document.getElementById('save_plan');
      if (save) save.disabled = true;
      document.querySelectorAll('[data-reviewed-action]').forEach(function (link) {
        link.setAttribute('aria-disabled', 'true');
      });
    });
  });
  document.querySelectorAll('[data-reviewed-action]').forEach(function (link) {
    link.addEventListener('click', function (event) {
      if (!dirty) return;
      event.preventDefault();
      document.getElementById('review_lifestyle').focus();
    });
  });
})();
