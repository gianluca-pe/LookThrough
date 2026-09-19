/* LookThrough progressive enhancement. Every feature here has a complete
   no-JavaScript path; this file only shortens interactions.
   Loaded with `defer`; safe to fail silently. */
(function () {
  "use strict";

  /* Type-ahead for long selects.
     The select stays the submitted field; an <input list> filters its options
     and writes the chosen value back. Without JS the plain select remains. */
  function enhanceTypeahead(select) {
    if (!select.options.length || select.options.length < 4) return;

    /* The visible input takes over the select's id, so the label's `for`,
       error-summary anchors (e.g. #instrument_id), and aria-describedby
       targets keep resolving to a focusable element. The hidden select keeps
       the name attribute, so form submission is unchanged. */
    var fid = select.id;
    var input = document.createElement("input");
    input.type = "text";
    input.id = fid;
    select.id = fid + "-native";
    input.className = select.className;
    input.setAttribute("list", fid + "-list");
    input.setAttribute("autocomplete", "off");
    // Names and tickers are identifiers, not prose: keep the browser from
    // "correcting" them mid-entry.
    input.setAttribute("autocorrect", "off");
    input.setAttribute("autocapitalize", "none");
    input.setAttribute("spellcheck", "false");
    if (select.required) input.required = true;
    // Error and hint wiring rendered by the wt_field macro moves to the
    // visible control with it.
    if (select.getAttribute("aria-invalid")) {
      input.setAttribute("aria-invalid", select.getAttribute("aria-invalid"));
    }
    if (select.getAttribute("aria-describedby")) {
      input.setAttribute(
        "aria-describedby",
        select.getAttribute("aria-describedby")
      );
    }

    var datalist = document.createElement("datalist");
    datalist.id = fid + "-list";
    var byLabel = {};
    Array.prototype.forEach.call(select.options, function (option) {
      var item = document.createElement("option");
      item.value = option.text;
      datalist.appendChild(item);
      byLabel[option.text] = option.value;
    });

    function syncFromSelect() {
      var chosen = select.options[select.selectedIndex];
      input.value = chosen && select.value !== "" ? chosen.text : "";
    }

    /* Accept typed text without a mouse pick: an exact label, or a unique
       substring (a ticker inside "Name (TICKER)" resolves to that option).
       Ambiguous or unknown text resolves to nothing. */
    function resolve(text) {
      if (byLabel[text] !== undefined) return byLabel[text];
      var query = text.trim().toLowerCase();
      if (query === "") return undefined;
      var match;
      var ambiguous = false;
      Object.keys(byLabel).forEach(function (label) {
        if (label.toLowerCase().indexOf(query) === -1) return;
        if (match === undefined) match = byLabel[label];
        else if (match !== byLabel[label]) ambiguous = true;
      });
      return ambiguous ? undefined : match;
    }

    // The select starts on its first option; select-all on focus so typing
    // replaces the prefilled text instead of appending to it.
    input.addEventListener("focus", function () {
      input.select();
    });

    input.addEventListener("input", function () {
      var value = resolve(input.value);
      if (value !== undefined) {
        select.value = value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    input.addEventListener("change", function () {
      var value = resolve(input.value);
      if (value !== undefined) {
        // Tab-away accepts: apply the match and show its full label.
        select.value = value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
      // Either way the visible text stays honest with the submitted value.
      syncFromSelect();
    });

    var hadAutofocus = select.hasAttribute("autofocus");

    select.hidden = true;
    select.removeAttribute("required"); // validation moves to the visible input
    select.removeAttribute("autofocus");
    select.parentNode.insertBefore(input, select);
    select.parentNode.insertBefore(datalist, select);
    syncFromSelect();
    // Preserve the first-error/first-empty focus the select carried.
    if (hadAutofocus) input.focus();
  }

  /* Open the nearest disclosure when "Add a new instrument" (the __new__
     sentinel) is chosen and move focus to its first field, so keyboard entry
     continues where the user is. Collapsing back never steals focus. */
  function enhanceNewInstrument(select) {
    select.addEventListener("change", function () {
      var form = select.closest("form");
      if (!form) return;
      var details = form.querySelector("details.disclosure");
      if (!details) return;
      details.open = select.value === "__new__";
      if (details.open) {
        var first = details.querySelector("input, select, textarea");
        if (first) first.focus();
      }
    });
  }

  /* Dividend outcome: choosing a reinvestment outcome
     opens the reinvestment details disclosure so its dependent fields are
     visible; choosing Added to cash closes it. Focus never moves. Without JS
     the disclosure is an ordinary native <details> and re-opens server-side
     on any submitted reinvestment outcome. */
  function enhanceReinvestmentDetails(details) {
    var form = details.closest("form");
    if (!form) return;
    function sync() {
      var chosen = form.querySelector('input[name="outcome"]:checked');
      details.open = !!chosen && chosen.value !== "cash";
    }
    Array.prototype.forEach.call(
      form.querySelectorAll('input[name="outcome"][type="radio"]'),
      function (radio) { radio.addEventListener("change", sync); }
    );
  }

  /* After redirect-after-post, focus lands on the success panel.
     Without JS the panel simply sits at the top of
     the page, so the no-JS path loses convenience, never capability. */
  function focusSuccessPanel() {
    var panel = document.querySelector(".success-panel[tabindex]");
    if (panel) panel.focus();
  }

  /* Live trade preview: re-POSTs the trade form to the
     server preview endpoint on field change and swaps the server-rendered
     #preview-region. The server stays the only calculator; the Preview
     button remains the complete no-JS path. */
  function enhanceLivePreview(form) {
    var region = form.querySelector("#preview-region");
    var previewButton = form.querySelector("[formaction]");
    if (!region || !previewButton || !window.fetch || !window.DOMParser) return;
    var url = previewButton.getAttribute("formaction");
    var timer = null;
    var sequence = 0;

    function refresh() {
      var requestId = ++sequence;
      var data = new FormData(form);
      data.set(previewButton.name || "preview_trade", previewButton.value || "Preview");
      fetch(url, { method: "POST", body: data, credentials: "same-origin" })
        .then(function (response) { return response.text(); })
        .then(function (html) {
          if (requestId !== sequence) return; // a newer request superseded this one
          var doc = new DOMParser().parseFromString(html, "text/html");
          var fresh = doc.querySelector("#preview-region");
          // No preview in the response means invalid/incomplete input:
          // clear the region rather than show stale numbers.
          region.innerHTML = fresh ? fresh.innerHTML : "";
        })
        .catch(function () { /* keep the last server-rendered state */ });
    }

    form.addEventListener("input", function (event) {
      if (event.target.type === "hidden") return;
      clearTimeout(timer);
      timer = setTimeout(refresh, 400);
    });
    form.addEventListener("change", function () {
      clearTimeout(timer);
      timer = setTimeout(refresh, 150);
    });
  }

  /* As-of date pickers re-request the page on change; the Apply button
     remains the complete no-JS path. */
  function enhanceAutoSubmit(form) {
    var input = form.querySelector('input[type="date"]');
    if (!input) return;
    input.addEventListener("change", function () {
      if (input.value) form.submit();
    });
  }

  /* Classification editor convenience: show only the role input selected by
     Role format and show a live entered total. Display only — the server
     remains the only calculator and validator; without JS both role inputs
     stay visible and the server-side total error is the complete path. */
  function enhanceClassificationForm(form) {
    var mode = form.querySelector('select[name="classification_mode"]');
    var primary = form.querySelector("[data-primary-role-section]");
    var advanced = form.querySelector("[data-advanced-roles]");
    if (!mode || !primary || !advanced) return;
    var total = form.querySelector("[data-role-total]");

    function syncMode() {
      primary.hidden = mode.value === "advanced";
      advanced.hidden = mode.value !== "advanced";
    }

    function syncTotal() {
      if (!total) return;
      var sum = 0;
      var any = false;
      Array.prototype.forEach.call(
        form.querySelectorAll('input[name^="role_"][name$="_percent"]'),
        function (input) {
          var value = parseFloat(input.value);
          if (!isNaN(value)) { sum += value; any = true; }
        }
      );
      total.hidden = !any;
      total.textContent =
        "Entered total: " + Math.round(sum * 1000000) / 1000000 +
        "% — must total 100% to save.";
    }

    mode.addEventListener("change", syncMode);
    form.addEventListener("input", function (event) {
      var name = event.target.name || "";
      if (name.indexOf("role_") === 0 && name.indexOf("_percent") !== -1) syncTotal();
    });
    syncMode();
    syncTotal();
  }

  /* Spending-approach select reveals the optional adjustment-rule fields
    : choosing "Apply my spending adjustment rules" opens the
     native disclosure, choosing the full budget closes it. Display only —
     focus never moves, and without JS the disclosure keeps its
     server-rendered state and every approach remains saveable. No initial
     sync, so a server-reopened disclosure (e.g. with field errors) is
     never closed on load. */
  function enhanceSpendingRules(details) {
    var form = details.closest("form");
    if (!form) return;
    var select = form.querySelector('select[name="spending_policy"]');
    if (!select) return;
    select.addEventListener("change", function () {
      details.open = select.value === "guardrails";
    });
  }

  /* Return-path row editor (retirement scenario entry): the
     labelled textarea remains the submitted field and the complete
     no-JavaScript path. With JS, four inputs per year mirror each textarea
     line "Equity,Income,Liquidity,Alternatives". Sync only joins what the
     owner typed — no zero is ever filled in — and a stored line that does
     not split into exactly four parts (e.g. an error recovery with a blank
     line) leaves the plain textarea untouched. The server stays the only
     validator. */
  function enhanceReturnPath(textarea) {
    var roles, baseline;
    try {
      roles = JSON.parse(textarea.getAttribute("data-roles") || "[]");
      baseline = JSON.parse(textarea.getAttribute("data-baseline") || "[]");
    } catch (err) {
      return;
    }
    if (roles.length !== 4) return;

    var rows = [];
    if (textarea.value.trim() !== "") {
      var lines = textarea.value.replace(/\r\n/g, "\n").split("\n");
      while (lines.length && lines[lines.length - 1].trim() === "") lines.pop();
      for (var i = 0; i < lines.length; i++) {
        var parts = lines[i].split(",");
        if (parts.length !== 4) return; // keep the plain textarea for recovery
        rows.push(parts.map(function (part) { return part.trim(); }));
      }
    }
    if (!rows.length) rows.push(["", "", "", ""]);

    var fid = textarea.id;
    var editor = document.createElement("div");
    editor.className = "return-path-editor";
    var grid = document.createElement("div");
    grid.className = "rp-grid";
    editor.appendChild(grid);

    function cell(text, className) {
      var el = document.createElement("span");
      el.className = className;
      el.textContent = text;
      return el;
    }

    grid.appendChild(cell("", "rp-head"));
    roles.forEach(function (role, index) {
      var head = cell(role, "rp-head");
      if (baseline.length === 4) {
        var plan = document.createElement("span");
        plan.className = "rp-baseline";
        plan.textContent = "plan " + baseline[index] + "%";
        head.appendChild(plan);
      }
      grid.appendChild(head);
    });
    grid.appendChild(cell("", "rp-head"));

    var rowInputs = [];

    function renumber() {
      rowInputs.forEach(function (row, index) {
        row.label.textContent = "Year " + (index + 1);
        row.inputs.forEach(function (input, roleIndex) {
          input.setAttribute(
            "aria-label",
            "Year " + (index + 1) + " " + roles[roleIndex] + " return (%)"
          );
          // Keep the label and error-summary target on the current first row,
          // including after the original first row has been removed.
          if (index === 0 && roleIndex === 0) {
            input.id = fid;
            ["aria-invalid", "aria-describedby"].forEach(function (attribute) {
              if (textarea.hasAttribute(attribute)) {
                input.setAttribute(attribute, textarea.getAttribute(attribute));
              }
            });
          } else {
            input.removeAttribute("id");
            input.removeAttribute("aria-invalid");
            input.removeAttribute("aria-describedby");
          }
        });
        row.remove.setAttribute("aria-label", "Remove year " + (index + 1));
        row.remove.disabled = rowInputs.length === 1;
      });
    }

    function sync() {
      var values = rowInputs.map(function (row) {
        return row.inputs.map(function (input) { return input.value.trim(); });
      });
      // Ignore only unused trailing rows. An empty year before an entered
      // year must reach validation, never move that later return earlier.
      while (values.length && values[values.length - 1].every(function (value) {
        return value === "";
      })) values.pop();
      textarea.value = values.map(function (row) { return row.join(","); }).join("\n");
    }

    var addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "btn btn-plain rp-add";
    addButton.textContent = "Add another year";

    function addRow(values, shouldFocus) {
      var label = cell("", "rp-year");
      grid.appendChild(label);
      var inputs = roles.map(function (role, roleIndex) {
        var input = document.createElement("input");
        input.type = "text";
        input.setAttribute("inputmode", "decimal");
        input.setAttribute("autocomplete", "off");
        input.value = values[roleIndex];
        input.addEventListener("input", sync);
        grid.appendChild(input);
        return input;
      });
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "rp-remove";
      remove.textContent = "Remove";
      grid.appendChild(remove);
      var row = { label: label, inputs: inputs, remove: remove };
      remove.addEventListener("click", function () {
        var index = rowInputs.indexOf(row);
        grid.removeChild(row.label);
        row.inputs.forEach(function (input) { grid.removeChild(input); });
        grid.removeChild(row.remove);
        rowInputs.splice(index, 1);
        renumber();
        sync();
        var neighbour = rowInputs[Math.min(index, rowInputs.length - 1)];
        (neighbour ? neighbour.inputs[0] : addButton).focus();
      });
      rowInputs.push(row);
      renumber();
      if (shouldFocus) inputs[0].focus();
    }

    rows.forEach(function (values) { addRow(values, false); });

    addButton.addEventListener("click", function () {
      addRow(["", "", "", ""], true);
    });
    editor.appendChild(addButton);

    /* The label, error-summary anchor, and aria wiring rendered for the
       textarea move to the first row's first input, which stays focusable;
       the textarea keeps its name so submission is unchanged. */
    var first = rowInputs[0].inputs[0];
    var hadAutofocus = textarea.hasAttribute("autofocus");

    textarea.id = fid + "-native";
    textarea.hidden = true;
    textarea.removeAttribute("autofocus");
    textarea.parentNode.insertBefore(editor, textarea);
    sync();
    if (textarea.form) textarea.form.addEventListener("submit", sync);
    if (hadAutofocus) first.focus();
  }

  document.addEventListener("DOMContentLoaded", function () {
    var datasetMenu = document.querySelector(".dataset-menu");
    if (datasetMenu) {
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && datasetMenu.open) {
          datasetMenu.open = false;
          datasetMenu.querySelector("summary").focus();
        }
      });
      document.addEventListener("click", function (event) {
        if (!datasetMenu.contains(event.target)) datasetMenu.open = false;
      });
    }
    document.querySelectorAll("select[data-typeahead]").forEach(enhanceTypeahead);
    document.querySelectorAll("select[data-new-instrument]").forEach(enhanceNewInstrument);
    document.querySelectorAll("form[data-live-preview]").forEach(enhanceLivePreview);
    document.querySelectorAll("details[data-reinvestment-details]").forEach(enhanceReinvestmentDetails);
    document.querySelectorAll("details[data-spending-rules-details]").forEach(enhanceSpendingRules);
    document.querySelectorAll("form[data-autosubmit]").forEach(enhanceAutoSubmit);
    document.querySelectorAll("form[data-classification-form]").forEach(enhanceClassificationForm);
    document.querySelectorAll("textarea[data-return-path]").forEach(enhanceReturnPath);
    focusSuccessPanel();
  });
})();
