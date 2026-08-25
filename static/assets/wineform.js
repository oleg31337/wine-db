/* The wine card editor: every field is user-editable.
 * Suggestions (from barcode / label AI) are only ever applied to EMPTY fields.
 */
(function () {
  "use strict";
  var W = window.WineDB;
  var el = W.el;

  var TEXT_FIELDS = [
    ["name", "Name", { required: true, maxlength: 200 }],
    ["maker", "Maker / winery", { maxlength: 200 }],
    ["country", "Country", { maxlength: 100 }],
    ["region", "Region", { maxlength: 150 }],
    ["grape", "Grape / cépage", { maxlength: 300 }],
  ];

  var NUM_FIELDS = [
    ["vintage", "Vintage", { min: 1800, max: 2200, step: 1, placeholder: "e.g. 2019" }],
    ["alcohol_pct", "Alcohol %", { min: 0, max: 100, step: 0.1, placeholder: "e.g. 13.5" }],
    ["sugar_g_l", "Sugar (g/L)", { min: 0, max: 500, step: 0.1, placeholder: "e.g. 3" }],
  ];

  /* Builds the form. Returns { node, read(), applySuggestion(obj), values }. */
  W.wineForm = function (initial) {
    var data = Object.assign(
      {
        name: "",
        maker: "",
        wine_type: "other",
        country: "",
        region: "",
        grape: "",
        vintage: "",
        alcohol_pct: "",
        sugar_g_l: "",
        aromas: "",
        barcode: "",
        acidity: null,
        sweetness: null,
        body: null,
        mouthfeel: null,
        wood: null,
      },
      initial || {}
    );

    var inputs = {};
    var node = el("div");

    TEXT_FIELDS.forEach(function (f) {
      var key = f[0], label = f[1], opts = f[2] || {};
      var input = el("input", {
        id: "wf-" + key,
        value: data[key] === null || data[key] === undefined ? "" : String(data[key]),
        maxlength: opts.maxlength,
        autocapitalize: key === "name" || key === "maker" ? "words" : "none",
      });
      inputs[key] = input;
      node.appendChild(
        el("div", { class: "field" }, [
          el("label", { for: "wf-" + key, text: label + (opts.required ? " *" : "") }),
          input,
        ])
      );
    });

    /* Type selector */
    var typeSel = el("select", { id: "wf-type" });
    W.TYPES.forEach(function (t) {
      var opt = el("option", { value: t, text: W.fmt.typeLabel(t) });
      if (t === data.wine_type) opt.selected = true;
      typeSel.appendChild(opt);
    });
    inputs.wine_type = typeSel;
    node.appendChild(
      el("div", { class: "field" }, [el("label", { for: "wf-type", text: "Type *" }), typeSel])
    );

    /* Numeric row */
    var numRow = el("div", { class: "field-row" });
    NUM_FIELDS.forEach(function (f) {
      var key = f[0], label = f[1], opts = f[2];
      var input = el("input", {
        id: "wf-" + key,
        type: "number",
        inputmode: "decimal",
        min: opts.min,
        max: opts.max,
        step: opts.step,
        placeholder: opts.placeholder,
        value: data[key] === null || data[key] === undefined ? "" : String(data[key]),
      });
      inputs[key] = input;
      numRow.appendChild(
        el("div", {}, [el("label", { for: "wf-" + key, text: label }), input])
      );
    });
    node.appendChild(numRow);

    /* Barcode */
    var barcode = el("input", {
      id: "wf-barcode",
      inputmode: "numeric",
      maxlength: 64,
      value: data.barcode || "",
      placeholder: "digits only",
    });
    inputs.barcode = barcode;
    node.appendChild(
      el("div", { class: "field" }, [el("label", { for: "wf-barcode", text: "Barcode" }), barcode])
    );

    /* Aromas */
    var aromas = el("textarea", {
      id: "wf-aromas",
      maxlength: 2000,
      style: "min-height:90px",
      placeholder: "blackcurrant, cedar, tobacco, violet…",
    });
    aromas.value = data.aromas || "";
    inputs.aromas = aromas;
    node.appendChild(
      el("div", { class: "field" }, [
        el("label", { for: "wf-aromas", text: "Aromas / taste" }),
        aromas,
      ])
    );

    /* Gauges */
    var gaugeState = {};
    W.GAUGES.forEach(function (pair) {
      gaugeState[pair[0]] = data[pair[0]] === undefined ? null : data[pair[0]];
    });
    var gaugeWrap = el("div", {}, [el("h3", { text: "Structure" })]);
    W.GAUGES.forEach(function (pair) {
      gaugeWrap.appendChild(
        W.gaugeInput(pair[0], pair[1], gaugeState[pair[0]], function (v) {
          gaugeState[pair[0]] = v;
        })
      );
    });
    node.appendChild(gaugeWrap);

    function numOrNull(input) {
      var raw = (input.value || "").trim();
      if (raw === "") return null;
      var n = Number(raw);
      return isFinite(n) ? n : null;
    }

    function textOrNull(input) {
      var raw = (input.value || "").trim();
      return raw === "" ? null : raw;
    }

    function isEmpty(key) {
      if (key === "wine_type") return false; /* always has a value */
      if (W.GAUGES.some(function (p) { return p[0] === key; })) {
        return gaugeState[key] === null || gaugeState[key] === undefined;
      }
      var input = inputs[key];
      return !input || !String(input.value || "").trim();
    }

    return {
      node: node,
      inputs: inputs,
      isEmpty: isEmpty,
      focusName: function () {
        inputs.name.focus();
      },
      /* Only fills fields the user left empty (server enforces this too). */
      applySuggestion: function (suggestion) {
        var applied = [];
        Object.keys(suggestion || {}).forEach(function (key) {
          var value = suggestion[key];
          if (value === null || value === undefined || value === "") return;
          if (key === "wine_type") {
            if (typeSel.value === "other" && W.TYPES.indexOf(value) !== -1) {
              typeSel.value = value;
              applied.push(key);
            }
            return;
          }
          if (gaugeState.hasOwnProperty(key)) {
            if (isEmpty(key)) {
              gaugeState[key] = Number(value);
              applied.push(key);
            }
            return;
          }
          if (inputs[key] && isEmpty(key)) {
            inputs[key].value = String(value);
            applied.push(key);
          }
        });
        if (applied.length) {
          /* Repaint gauge pickers so they reflect suggested values. */
          W.clear(gaugeWrap).appendChild(el("h3", { text: "Structure" }));
          W.GAUGES.forEach(function (pair) {
            gaugeWrap.appendChild(
              W.gaugeInput(pair[0], pair[1], gaugeState[pair[0]], function (v) {
                gaugeState[pair[0]] = v;
              })
            );
          });
        }
        return applied;
      },
      read: function () {
        var out = {
          name: (inputs.name.value || "").trim(),
          maker: textOrNull(inputs.maker),
          wine_type: typeSel.value,
          country: textOrNull(inputs.country),
          region: textOrNull(inputs.region),
          grape: textOrNull(inputs.grape),
          vintage: numOrNull(inputs.vintage),
          alcohol_pct: numOrNull(inputs.alcohol_pct),
          sugar_g_l: numOrNull(inputs.sugar_g_l),
          aromas: textOrNull(inputs.aromas),
          barcode: textOrNull(inputs.barcode),
        };
        if (out.vintage !== null) out.vintage = Math.round(out.vintage);
        W.GAUGES.forEach(function (pair) {
          out[pair[0]] = gaugeState[pair[0]];
        });
        return out;
      },
      validate: function () {
        var v = this.read();
        if (!v.name) return "A name is required.";
        if (v.barcode && !/^\d+$/.test(v.barcode)) return "Barcode must be digits only.";
        if (v.vintage !== null && (v.vintage < 1800 || v.vintage > 2200))
          return "Vintage looks wrong.";
        if (v.alcohol_pct !== null && (v.alcohol_pct < 0 || v.alcohol_pct > 100))
          return "Alcohol % must be between 0 and 100.";
        if (v.sugar_g_l !== null && (v.sugar_g_l < 0 || v.sugar_g_l > 500))
          return "Sugar must be between 0 and 500 g/L.";
        return null;
      },
    };
  };
})();
