/* Portfolio and retirement charts rendered from
   server-embedded data-* JSON islands. Progressive enhancement only — every
   chart has a table equivalent that remains fully usable without JS, and
   prefers-reduced-motion disables animation. No remote calls: Chart.js is
   vendored at static/js/vendor/chart.umd.min.js. */
(function () {
  "use strict";

  /* Chart colors follow the stylesheet's custom properties so a token change
     cannot silently diverge; the hex fallbacks match app.css:root. */
  function cssToken(name, fallback) {
    var value = window.getComputedStyle
      ? window
          .getComputedStyle(document.documentElement)
          .getPropertyValue(name)
          .trim()
      : "";
    return value || fallback;
  }

  function initCharts() {
    if (typeof Chart === "undefined") return;

    var accentColor = cssToken("--color-accent", "#1a5c45");
    var borderColor = cssToken("--color-border", "#e2ded8");

    var reduceMotion =
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function parseList(el, name) {
      try {
        return JSON.parse(el.getAttribute(name) || "[]");
      } catch (err) {
        return [];
      }
    }

    document.querySelectorAll("[data-chart='affordability']").forEach(function (el) {
      var spending = el.dataset.series === 'spending';
      var labels = parseList(el, 'data-labels');
      var prefix = spending ? 'spending' : 'capital';
      var nominalTotals = parseList(el, spending ? 'data-total' : 'data-nominal');
      var todayTotals = parseList(el, spending ? 'data-today' : 'data-capital');
      var picker = document.getElementById(prefix + '-year');
      var selectedIndex = picker ? Number(picker.value) : 0;
      function money(value) {
        return el.dataset.currency + ' ' + value.toLocaleString('en-US', {minimumFractionDigits: Number(el.dataset.moneyPlaces || 2), maximumFractionDigits: Number(el.dataset.moneyPlaces || 2)});
      }
      function showYear(index) {
        selectedIndex = index;
        document.getElementById(prefix + '-year-label').textContent = (spending ? 'Payments at age ' : 'Capital at age ') + labels[index] + (spending ? '' : ' · today’s money');
        document.getElementById(prefix + '-nominal').textContent = money(nominalTotals[index]) + (spending ? ' / year' : '');
        document.getElementById(prefix + '-today').textContent = money(todayTotals[index]) + (spending ? ' / year' : '');
        document.getElementById(prefix + '-age').textContent = labels[index];
        picker.value = index;
        picker.setAttribute('aria-valuetext', 'Age ' + labels[index]);
      }
      var datasets = spending ? [
        {label:'Core · nominal', data:parseList(el,'data-core'), backgroundColor:accentColor, pointStyle:'rect', stack:'payments', order:1},
        {label:'Flexible · nominal', data:parseList(el,'data-flexible'), backgroundColor:'#96b7a5', pointStyle:'rect', stack:'payments', order:1},
        {type:'line', label:'Same spending · today’s money', data:todayTotals, borderColor:'#9b7357', backgroundColor:'#9b7357', borderDash:[6,4], borderWidth:2, pointRadius:0, pointHoverRadius:4, pointStyle:'line', stack:'today-money', order:0}
      ] : [
        {label:'Ending capital · today’s money', data:todayTotals, borderColor:accentColor, pointRadius:0, borderWidth:2},
        {label:'Final-age goal · today’s money', data:parseList(el,'data-goal'), borderColor:'#9b7357', borderDash:[6,4], pointRadius:0, borderWidth:2}
      ];
      var chart = new Chart(el.querySelector('canvas'), {
        type:spending ? 'bar' : 'line',
        data:{labels:labels, datasets:datasets},
        plugins: [{id:'selected-age-guide', afterDatasetsDraw:function(instance) {
          var point = instance.getDatasetMeta(0).data[selectedIndex];
          if (!point) return;
          var ctx = instance.ctx;
          ctx.save(); ctx.strokeStyle = borderColor; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(point.x, instance.chartArea.top); ctx.lineTo(point.x, instance.chartArea.bottom); ctx.stroke(); ctx.restore();
        }}],
        options:{locale:'en-US', maintainAspectRatio:false, animation:reduceMotion ? false : undefined,
          interaction:{mode:'index',intersect:false},
          onClick:function(event, elements) {
            if (picker && elements.length) { showYear(elements[0].index); chart.update('none'); }
          },
          plugins:{legend:{labels:{usePointStyle:spending, sort:function(a,b) { return a.datasetIndex-b.datasetIndex; }}}, tooltip:{callbacks:{
            title:function(items) { return items.length ? 'Age ' + labels[items[0].dataIndex] : ''; },
            beforeBody:function(items) { return spending && items.length ? 'Total · nominal: ' + money(nominalTotals[items[0].dataIndex]) : ''; },
            label:function(ctx) {
              var label = ctx.dataset.label + ': ' + money(ctx.parsed.y);
              return !spending && ctx.datasetIndex === 0 ? [label, 'Ending capital · nominal: ' + money(nominalTotals[ctx.dataIndex])] : label;
            }
          }}},
          scales:{x:{stacked:spending,title:{display:true,text:spending ? 'Age at year start' : 'Age at year end'}},
                  y:{stacked:spending,beginAtZero:true,title:{display:true,text:el.dataset.currency + (spending ? ' / year' : ' · today’s money')}}}
        }
      });
      if (picker) {
        picker.parentElement.hidden = false;
        picker.addEventListener('input', function() { showYear(Number(picker.value)); chart.update('none'); });
      }
    });

    document.querySelectorAll("[data-chart='allocation'], [data-chart='bucket-reserves']").forEach(function (el) {
      var reserves = el.dataset.chart === 'bucket-reserves';
      var reservePercentages = reserves && el.dataset.unit === 'percent';
      var labels = parseList(el, "data-labels");
      var actual = parseList(el, "data-actual");
      var canvas = el.querySelector("canvas");
      if (!canvas || !labels.length || labels.length !== actual.length) return;

      var datasets = [
        {
          label: reserves && !reservePercentages ? 'Current eligible reserves' : "Current %",
          data: actual,
          backgroundColor: accentColor,
        },
      ];
      if (reserves) {
        datasets.push({label:reservePercentages ? 'Required %' : 'Required reserve', data:parseList(el,'data-required'), backgroundColor:borderColor});
        el.hidden = false;
      }
      var targetMin = parseList(el, "data-target-min");
      var targetMax = parseList(el, "data-target-max");
      if (targetMin.length === labels.length && targetMax.length === labels.length) {
        datasets.push({
          label: "Target range",
          data: targetMin.map(function (lo, i) {
            return [lo, targetMax[i]];
          }),
          backgroundColor: borderColor,
        });
      }

      new Chart(canvas, {
        type: "bar",
        data: { labels: labels, datasets: datasets },
        options: {
          locale: reserves ? 'en-US' : undefined,
          indexAxis: "y",
          animation: reduceMotion ? false : undefined,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: datasets.length > 1 },
            tooltip: {
              callbacks: {
                label: function (ctx) {
                  var value = ctx.raw;
                  if (reserves) {
                    var amounts = parseList(el, ctx.datasetIndex === 0 ? 'data-actual-amounts' : 'data-required-amounts');
                    var money = el.dataset.currency + ' ' + Number(amounts[ctx.dataIndex]).toLocaleString('en-US', {minimumFractionDigits: Number(el.dataset.moneyPlaces || 2), maximumFractionDigits: Number(el.dataset.moneyPlaces || 2)});
                    return ctx.dataset.label + ': ' + (reservePercentages ? value + '% · ' : '') + money;
                  }
                  if (Array.isArray(value)) {
                    return ctx.dataset.label + ": " + value[0] + "–" + value[1] + "%";
                  }
                  return ctx.dataset.label + ": " + value + "%";
                },
              },
            },
          },
          scales: {
            x: {
              min: 0,
              max: reserves ? (reservePercentages ? Number(el.dataset.maximum) : undefined) : 100,
              title: { display: true, text: reserves ? (reservePercentages ? '% of modelled retirement capital' : el.dataset.currency + ' · today’s money') : "% of classified included value" },
            },
          },
        },
      });
    });
    document.querySelectorAll("[data-chart='value-breakdown']").forEach(function (el) {
      var labels = parseList(el, "data-labels");
      var values = parseList(el, "data-values");
      var percentages = parseList(el, "data-percentages");
      var currency = el.getAttribute("data-currency") || "";
      var title = el.getAttribute("data-title") || "Included value";
      var canvas = el.querySelector("canvas");
      if (!canvas || !labels.length || labels.length !== values.length) return;
      var displayLabels = labels.map(function (label, i) {
        if (percentages.length !== labels.length || percentages[i] === null) {
          return label;
        }
        return label + " · " + percentages[i].toFixed(1) + "%";
      });

      new Chart(canvas, {
        type: "bar",
        data: {
          labels: displayLabels,
          datasets: [
            {
              label: "Included value",
              data: values,
              backgroundColor: accentColor,
            },
          ],
        },
        options: {
          indexAxis: "y",
          animation: reduceMotion ? false : undefined,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            title: { display: true, text: title },
            tooltip: {
              callbacks: {
                label: function (ctx) {
                  var amount =
                    currency +
                    " " +
                    ctx.parsed.x.toLocaleString("en-US", {
                      minimumFractionDigits: Number(el.dataset.moneyPlaces || 2),
                      maximumFractionDigits: Number(el.dataset.moneyPlaces || 2),
                    });
                  if (
                    percentages.length === labels.length &&
                    percentages[ctx.dataIndex] !== null
                  ) {
                    return (
                      "Included value: " +
                      amount +
                      " (" +
                      percentages[ctx.dataIndex].toFixed(1) +
                      "%)"
                    );
                  }
                  return "Included value: " + amount;
                },
              },
            },
          },
          scales: {
            x: { title: { display: true, text: "Included value (" + currency + ")" } },
          },
        },
      });
    });
    document.querySelectorAll("[data-chart='retirement-spending']").forEach(function (el) {
      var labels = parseList(el, "data-labels");
      var desired = parseList(el, "data-desired");
      var core = parseList(el, "data-core");
      var flexible = parseList(el, "data-flexible");
      var currency = el.getAttribute("data-currency") || "";
      var canvas = el.querySelector("canvas");
      if (
        !canvas ||
        !labels.length ||
        labels.length !== desired.length ||
        labels.length !== core.length ||
        labels.length !== flexible.length
      ) return;

      /* Paid amounts stack; the desired total is a reference line, so a Core
         shortfall year can never read as fully paid. */
      function moneyLabel(label, value) {
        return (
          label +
          ": " +
          currency +
          " " +
          value.toLocaleString("en-US", {
            minimumFractionDigits: Number(el.dataset.moneyPlaces || 2),
            maximumFractionDigits: Number(el.dataset.moneyPlaces || 2),
          })
        );
      }

      new Chart(canvas, {
        data: {
          labels: labels,
          datasets: [
            {
              type: "line",
              label: "Desired total",
              data: desired,
              borderColor: cssToken("--color-text-muted", "#6b675f"),
              borderDash: [4, 3],
              pointRadius: 0,
              tension: 0,
            },
            {
              type: "bar",
              label: "Core paid",
              data: core,
              backgroundColor: accentColor,
              stack: "paid",
            },
            {
              type: "bar",
              label: "Flexible paid",
              data: flexible,
              backgroundColor: cssToken("--color-border-strong", "#c9c3b9"),
              stack: "paid",
            },
          ],
        },
        options: {
          animation: reduceMotion ? false : undefined,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                title: function (items) {
                  return "Age " + items[0].label;
                },
                label: function (ctx) {
                  return moneyLabel(ctx.dataset.label, ctx.parsed.y);
                },
              },
            },
          },
          scales: {
            x: { stacked: true, title: { display: true, text: "Age" } },
            y: {
              stacked: true,
              min: 0,
              title: { display: true, text: "Annual spending (" + currency + ")" },
            },
          },
        },
      });
    });
    document.querySelectorAll("[data-chart='scenario-ending']").forEach(function (el) {
      var labels = parseList(el, "data-labels");
      var baseline = parseList(el, "data-baseline");
      var stress = parseList(el, "data-stress");
      var currency = el.getAttribute("data-currency") || "";
      var canvas = el.querySelector("canvas");
      if (
        !canvas ||
        !labels.length ||
        labels.length !== baseline.length ||
        labels.length !== stress.length
      ) return;

      /* Ending capital under the saved constant returns versus the entered
         early-return path. The annual tables carry every
         plotted figure; neither line is coloured as good or bad. */
      new Chart(canvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Baseline — saved constant returns",
              data: baseline,
              borderColor: cssToken("--color-text-muted", "#6b675f"),
              backgroundColor: cssToken("--color-text-muted", "#6b675f"),
              borderDash: [4, 3],
              tension: 0,
              pointRadius: 0,
            },
            {
              label: "Entered return path",
              data: stress,
              borderColor: accentColor,
              backgroundColor: accentColor,
              tension: 0,
              pointRadius: 2,
            },
          ],
        },
        options: {
          animation: reduceMotion ? false : undefined,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              callbacks: {
                title: function (items) {
                  return "Age " + items[0].label;
                },
                label: function (ctx) {
                  return (
                    ctx.dataset.label +
                    ": " +
                    currency +
                    " " +
                    ctx.parsed.y.toLocaleString("en-US", {
                      minimumFractionDigits: Number(el.dataset.moneyPlaces || 2),
                      maximumFractionDigits: Number(el.dataset.moneyPlaces || 2),
                    })
                  );
                },
              },
            },
          },
          scales: {
            x: { title: { display: true, text: "Age" } },
            y: {
              min: 0,
              title: { display: true, text: "Ending capital (" + currency + ")" },
            },
          },
        },
      });
    });
    document.querySelectorAll("[data-chart='retirement']").forEach(function (el) {
      var labels = parseList(el, "data-labels");
      var values = parseList(el, "data-values");
      var currency = el.getAttribute("data-currency") || "";
      var canvas = el.querySelector("canvas");
      if (!canvas || !labels.length || labels.length !== values.length) return;

      new Chart(canvas, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Ending value",
              data: values,
              borderColor: accentColor,
              backgroundColor: accentColor,
              tension: 0,
              pointRadius: 2,
            },
          ],
        },
        options: {
          animation: reduceMotion ? false : undefined,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                title: function (items) {
                  return "Age " + items[0].label;
                },
                label: function (ctx) {
                  return (
                    "Ending value: " +
                    currency +
                    " " +
                    ctx.parsed.y.toLocaleString("en-US", {
                      minimumFractionDigits: Number(el.dataset.moneyPlaces || 2),
                      maximumFractionDigits: Number(el.dataset.moneyPlaces || 2),
                    })
                  );
                },
              },
            },
          },
          scales: {
            x: { title: { display: true, text: "Age" } },
            y: {
              min: 0,
              title: { display: true, text: "Ending value (" + currency + ")" },
            },
          },
        },
      });
    });
  }

  /* Run once the document is parsed, regardless of where the tag sits or
     whether it still carries `defer`. */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCharts);
  } else {
    initCharts();
  }
})();
