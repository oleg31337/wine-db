/* Rendering: stars, gauges, mini cards. */
(function () {
  "use strict";
  var W = window.WineDB;
  var el = W.el;

  /* Read-only star display. */
  W.stars = function (value) {
    var v = Math.round(Number(value) || 0);
    var kids = [];
    for (var i = 1; i <= 5; i++) {
      kids.push(el("span", { class: "s" + (i <= v ? " on" : ""), text: "★", "aria-hidden": "true" }));
    }
    return el(
      "span",
      { class: "stars", role: "img", "aria-label": v ? v + " out of 5 stars" : "not rated" },
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

  /* Read-only gauge bars. Scale is 0-3 (0 = "no such taste", shown as all
   * empty cells; no number/dash is printed, per request). Renders as a row of
   * 3 segmented cells that fill left to right. */
  W.gaugeBars = function (wine) {
    var rows = W.GAUGES.map(function (pair) {
      var key = pair[0];
      var raw = wine[key];
      var has = raw !== null && raw !== undefined;
      var level = has ? Number(raw) : 0;

      var cells = [];
      for (var i = 1; i <= 3; i++) {
        cells.push(
          el("span", {
            class: "g-cell" + (has && i <= level ? " on" : ""),
            "aria-hidden": "true",
          })
        );
      }
      return el("div", { class: "gauge-row" }, [
        el("span", { class: "g-label", text: pair[1] }),
        el(
          "div",
          {
            class: "gauge-track",
            role: "meter",
            "aria-valuemin": "0",
            "aria-valuemax": "3",
            "aria-valuenow": has ? String(level) : "",
            "aria-label": pair[1] + (has ? ": " + level + " of 3" : ": not assessed"),
          },
          cells
        ),
      ]);
    });
    return el("div", { class: "gauges" }, rows);
  };

  /* 0-3 gauge picker used in the edit form. Renders as a 3-segment progress
   * bar: tap a segment to set that level, tap the leftmost (0) to clear to
   * "no such taste". Tapping the current level again clears to unassessed.
   */
  W.gaugeInput = function (key, label, current, onChange) {
    var value = current === null || current === undefined ? null : Number(current);
    var row = el("div", { class: "field" });
    row.appendChild(el("label", { text: label }));
    var group = el("div", {
      class: "gauge-pick",
      role: "group",
      "aria-label": label,
      dataset: { key: key },
    });

    function paint() {
      W.$$(".seg", group).forEach(function (seg) {
        var n = Number(seg.dataset.val);
        var on = value !== null && n <= value;
        seg.classList.toggle("on", on);
        seg.setAttribute("aria-pressed", on ? "true" : "false");
      });
    }

    for (var i = 0; i <= 3; i++) {
      (function (n) {
        group.appendChild(
          el("button", {
            type: "button",
            class: "seg",
            dataset: { val: String(n) },
            "aria-label": n === 0 ? "No " + label.toLowerCase() : label + " " + n,
            onclick: function () {
              // Tap the active level (or 0 when set) to clear -> unassessed.
              if (value === n) {
                value = null;
              } else {
                value = n;
              }
              paint();
              onChange(value);
            },
          })
        );
      })(i);
    }
    paint();
    row.appendChild(group);
    row.appendChild(
      el("p", {
        class: "hint",
        text: "0 = no such taste · tap the same segment to clear.",
      })
    );
    return row;
  };

  /* Scrollable search result card. */
  W.miniCard = function (wine, onOpen) {
    var thumb = el("div", { class: "mini-thumb" });
    if (wine.photo_url) {
      // Cache-bust the thumb so a re-uploaded photo actually re-renders. The
      // backend serves photos with a 1-day Cache-Control, and the URL itself
      // never changes (it's /api/wines/{id}/photo), so without this the browser
      // keeps showing the old image. updated_at changes on every save, including
      // a photo swap.
      var v = wine.updated_at ? String(wine.updated_at).replace(/[^0-9]/g, "") : Date.now();
      thumb.appendChild(
        el("img", { src: wine.photo_url + "?v=" + v, alt: "", loading: "lazy", decoding: "async" })
      );
    } else {
      thumb.appendChild(el("span", { text: "🍷", "aria-hidden": "true" }));
    }

    var place = [wine.region, wine.country].filter(Boolean).join(", ");
    var foot = [
      el("span", { class: "badge " + wine.wine_type, text: W.fmt.typeLabel(wine.wine_type) }),
      wine.vintage ? el("span", { class: "badge badge-year", text: String(wine.vintage) }) : null,
      // Combined average rating across all users (no numeric value after stars).
      W.stars(wine.average_rating || 0),
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

  W.loadingRow = function (label, big) {
    return el("div", { class: "loading-row" }, [
      el("span", { class: "spinner" + (big ? " spinner-lg" : ""), "aria-hidden": "true" }),
      el("span", { text: label || "Loading…" }),
    ]);
  };
})();
