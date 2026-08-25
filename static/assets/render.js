/* Rendering: stars, gauges, mini cards. */
(function () {
  "use strict";
  var W = window.WineDB;
  var el = W.el;

  /* Read-only star display. */
  W.stars = function (value, big) {
    var v = Math.round(Number(value) || 0);
    var kids = [];
    for (var i = 1; i <= 5; i++) {
      kids.push(el("span", { class: "s" + (i <= v ? " on" : ""), text: "★", "aria-hidden": "true" }));
    }
    return el(
      "span",
      { class: "stars" + (big ? " stars-lg" : ""), role: "img", "aria-label": v ? v + " out of 5 stars" : "not rated" },
      kids
    );
  };

  /* Interactive star picker. onPick(stars) / onClear(). */
  W.starInput = function (current, onPick, onClear) {
    var wrap = el("span", { class: "star-input", role: "group", "aria-label": "Your rating" });
    var value = Number(current) || 0;

    function paint() {
      W.$$("button", wrap).forEach(function (b, idx) {
        var on = idx + 1 <= value;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }

    for (var i = 1; i <= 5; i++) {
      (function (n) {
        wrap.appendChild(
          el("button", {
            type: "button",
            text: "★",
            "aria-label": n + " star" + (n > 1 ? "s" : ""),
            onclick: function () {
              if (value === n && onClear) {
                value = 0;
                paint();
                onClear();
                return;
              }
              value = n;
              paint();
              onPick(n);
            },
          })
        );
      })(i);
    }
    paint();
    return wrap;
  };

  /* Read-only gauge bars. */
  W.gaugeBars = function (wine) {
    var rows = W.GAUGES.map(function (pair) {
      var key = pair[0];
      var raw = wine[key];
      var has = raw !== null && raw !== undefined;
      var pct = has ? (Number(raw) / 5) * 100 : 0;
      return el("div", { class: "gauge-row" }, [
        el("span", { class: "g-label", text: pair[1] }),
        el(
          "div",
          {
            class: "gauge-track",
            role: "meter",
            "aria-valuemin": "0",
            "aria-valuemax": "5",
            "aria-valuenow": has ? String(raw) : "0",
            "aria-label": pair[1],
          },
          [el("div", { class: "gauge-fill", style: "width:" + pct + "%" })]
        ),
        el("span", { class: "g-val", text: has ? raw + "/5" : "–" }),
      ]);
    });
    return el("div", { class: "gauges" }, rows);
  };

  /* 0-5 gauge picker used in the edit form. */
  W.gaugeInput = function (key, label, current, onChange) {
    var value = current === null || current === undefined ? null : Number(current);
    var row = el("div", { class: "field" });
    row.appendChild(el("label", { text: label }));
    var group = el("div", { class: "gauge-input", role: "group", "aria-label": label });

    function paint() {
      W.$$("button", group).forEach(function (b) {
        var on = b.dataset.val === String(value);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }

    for (var i = 0; i <= 5; i++) {
      (function (n) {
        group.appendChild(
          el("button", {
            type: "button",
            text: String(n),
            dataset: { val: String(n) },
            onclick: function () {
              value = value === n ? null : n;
              paint();
              onChange(value);
            },
          })
        );
      })(i);
    }
    paint();
    row.appendChild(group);
    row.appendChild(el("p", { class: "hint", text: "Tap the same number again to clear." }));
    return row;
  };

  /* Scrollable search result card. */
  W.miniCard = function (wine, onOpen) {
    var thumb = el("div", { class: "mini-thumb" });
    if (wine.photo_url) {
      thumb.appendChild(
        el("img", { src: wine.photo_url, alt: "", loading: "lazy", decoding: "async" })
      );
    } else {
      thumb.appendChild(el("span", { text: "🍷", "aria-hidden": "true" }));
    }

    var place = [wine.region, wine.country].filter(Boolean).join(", ");
    var foot = [
      el("span", { class: "badge " + wine.wine_type, text: W.fmt.typeLabel(wine.wine_type) }),
      wine.vintage ? el("span", { class: "badge badge-year", text: String(wine.vintage) }) : null,
      W.stars(wine.my_rating || wine.average_rating || 0),
      wine.rating_count
        ? el("span", {
            class: "rating-count",
            text: (wine.average_rating || 0).toFixed(1) + " · " + wine.rating_count,
          })
        : null,
    ];

    return el(
      "button",
      {
        type: "button",
        class: "mini",
        onclick: function () {
          onOpen(wine.id);
        },
      },
      [
        thumb,
        el("span", { class: "mini-body" }, [
          el("span", { class: "mini-name", text: wine.name }),
          wine.maker ? el("span", { class: "mini-maker", text: wine.maker }) : null,
          place ? el("span", { class: "mini-place", text: place }) : null,
          el("span", { class: "mini-foot" }, foot),
        ]),
      ]
    );
  };

  W.emptyState = function (icon, message) {
    return el("div", { class: "empty" }, [
      el("span", { class: "big", text: icon, "aria-hidden": "true" }),
      el("span", { text: message }),
    ]);
  };

  W.loadingRow = function (label) {
    return el("div", { class: "loading-row" }, [
      el("span", { class: "spinner", "aria-hidden": "true" }),
      el("span", { text: label || "Loading…" }),
    ]);
  };
})();
