/* Core: API client, CSRF, toasts, modal, small helpers. */
(function () {
  "use strict";

  var W = (window.WineDB = window.WineDB || {});

  function cookie(name) {
    var parts = ("; " + document.cookie).split("; " + name + "=");
    return parts.length === 2 ? decodeURIComponent(parts.pop().split(";").shift()) : "";
  }

  function csrf() {
    return cookie("winedb_csrf");
  }

  var listeners = { unauth: [] };
  function onUnauthorized(fn) {
    listeners.unauth.push(fn);
  }

  function request(method, path, options) {
    options = options || {};
    var headers = { Accept: "application/json" };
    var body = null;

    if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(options.json);
    } else if (options.form) {
      body = options.form;
    }
    if (method !== "GET" && method !== "HEAD") {
      headers["X-CSRF-Token"] = csrf();
    }

    return fetch(path, {
      method: method,
      headers: headers,
      body: body,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    }).then(function (resp) {
      if (resp.status === 401 && !options.allowUnauthorized) {
        listeners.unauth.forEach(function (fn) {
          fn();
        });
      }
      if (options.raw) {
        if (!resp.ok) return failure(resp);
        return resp.blob();
      }
      if (resp.status === 204) return null;
      var ctype = resp.headers.get("Content-Type") || "";
      if (ctype.indexOf("application/json") === -1) {
        if (!resp.ok) return failure(resp);
        return resp.text();
      }
      return resp.json().then(function (data) {
        if (!resp.ok) {
          var err = new Error(messageFor(data, resp.status));
          err.status = resp.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function failure(resp) {
    return resp.text().then(function (text) {
      var message;
      if (resp.status === 413) {
        message =
          "Upload too large — your server or reverse proxy rejected the request. " +
          "Raise its request-body limit (nginx client_max_body_size / caddy max_body_size) " +
          "or use a smaller photo.";
      } else if (text && /<[a-z][\s\S]*>/i.test(text)) {
        // The body is an HTML error page (typically from a reverse proxy),
        // not a JSON API error. Strip the tags so we don't show raw markup.
        message = (text.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim()) ||
          ("Request failed (" + resp.status + ")");
      } else {
        message = text || "Request failed (" + resp.status + ")";
      }
      var err = new Error(message);
      err.status = resp.status;
      throw err;
    });
  }

  function messageFor(data, status) {
    if (!data) return "Request failed (" + status + ")";
    if (typeof data.detail === "string" && !data.errors) return data.detail;
    if (data.errors && data.errors.length) {
      return data.errors
        .map(function (e) {
          return (e.field ? e.field + ": " : "") + e.message;
        })
        .join("; ");
    }
    if (typeof data.detail === "string") return data.detail;
    return "Request failed (" + status + ")";
  }

  W.api = {
    get: function (p, o) {
      return request("GET", p, o);
    },
    post: function (p, o) {
      return request("POST", p, o);
    },
    put: function (p, o) {
      return request("PUT", p, o);
    },
    patch: function (p, o) {
      return request("PATCH", p, o);
    },
    del: function (p, o) {
      return request("DELETE", p, o);
    },
    onUnauthorized: onUnauthorized,
    csrf: csrf,
  };

  /* ---------- DOM helpers ---------- */
  W.$ = function (sel, root) {
    return (root || document).querySelector(sel);
  };
  W.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };
  W.el = function (tag, attrs, kids) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.textContent = v; /* never inject markup */
      else if (k.indexOf("on") === 0 && typeof v === "function")
        node.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.keys(v).forEach(function (d) { node.dataset[d] = v[d]; });
      else node.setAttribute(k, v === true ? "" : v);
    });
    (kids || []).forEach(function (kid) {
      if (kid === null || kid === undefined || kid === false) return;
      node.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    });
    return node;
  };
  W.clear = function (node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  };

  /* ---------- toasts ---------- */
  W.toast = function (message, kind, ms) {
    var host = W.$("#toasts");
    if (!host) return;
    var node = W.el("div", { class: "toast " + (kind || ""), text: String(message) });
    host.appendChild(node);
    setTimeout(function () {
      node.remove();
    }, ms || (kind === "err" ? 6000 : 3200));
  };
  W.errToast = function (err) {
    W.toast((err && err.message) || "Something went wrong", "err");
  };

  /* ---------- modal ---------- */
  var openModals = [];
  W.modal = function (opts) {
    var root = W.$("#modal-root");
    var body = W.el("div", { class: "modal-body" }, opts.body ? [opts.body] : []);
    var foot = W.el("div", { class: "modal-foot" }, opts.footer || []);
    var closeBtn = W.el("button", {
      class: "btn-quiet btn-sm",
      type: "button",
      "aria-label": "Close",
      text: "✕",
      onclick: function () {
        close();
      },
    });
    var panel = W.el("div", { class: "modal", role: "dialog", "aria-modal": "true" }, [
      W.el("div", { class: "modal-head" }, [
        W.el("h2", { text: opts.title || "" }),
        closeBtn,
      ]),
      body,
      opts.footer && opts.footer.length ? foot : null,
    ]);
    var backdrop = W.el("div", { class: "modal-backdrop" }, [panel]);
    backdrop.addEventListener("mousedown", function (ev) {
      if (ev.target === backdrop && opts.dismissable !== false) close();
    });

    function onKey(ev) {
      if (ev.key === "Escape" && opts.dismissable !== false) close();
    }

    function close() {
      document.removeEventListener("keydown", onKey);
      backdrop.remove();
      openModals = openModals.filter(function (m) {
        return m !== handle;
      });
      // Only release the page scroll when the LAST modal closes.
      if (!openModals.length) document.body.classList.remove("modal-open");
      if (typeof opts.onClose === "function") opts.onClose();
    }

    document.addEventListener("keydown", onKey);
    root.appendChild(backdrop);
    document.body.classList.add("modal-open");
    var focusable = panel.querySelector("input, select, textarea, button.btn-primary");
    if (focusable) setTimeout(function () { focusable.focus(); }, 30);

    var handle = { close: close, panel: panel, body: body };
    openModals.push(handle);
    return handle;
  };

  W.confirm = function (opts) {
    return new Promise(function (resolve) {
      var settled = false;
      function settle(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      var m;
      var cancel = W.el("button", {
        type: "button",
        text: opts.cancelText || "Cancel",
        onclick: function () {
          m.close();
          settle(false);
        },
      });
      var ok = W.el("button", {
        type: "button",
        class: opts.danger ? "btn-danger" : "btn-primary",
        text: opts.okText || "Confirm",
        onclick: function () {
          settle(true);
          m.close();
        },
      });
      m = W.modal({
        title: opts.title || "Are you sure?",
        body: W.el("p", { class: "muted", text: opts.message || "" }),
        footer: [cancel, ok],
        onClose: function () {
          settle(false);
        },
      });
    });
  };

  /* ---------- formatting ---------- */
  W.fmt = {
    date: function (iso) {
      if (!iso) return "";
      var d = new Date(iso);
      if (isNaN(d.getTime())) return "";
      return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    },
    typeLabel: function (t) {
      return { red: "Red", white: "White", rose: "Rosé", sparkling: "Sparkling", other: "Other" }[t] || "Other";
    },
    num: function (v, suffix) {
      if (v === null || v === undefined || v === "") return "";
      return String(v) + (suffix || "");
    },
  };

  W.debounce = function (fn, ms) {
    var timer;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn.apply(self, args);
      }, ms);
    };
  };

  W.GAUGES = [
    ["acidity", "Acidity"],
    ["sweetness", "Sweetness"],
    ["body", "Body"],
    ["mouthfeel", "Mouthfeel"],
    ["wood", "Wood / oak"],
  ];
  W.TYPES = ["red", "white", "rose", "sparkling", "other"];
  W.COMMENT_MAX = 4000;

  /* Downscale/transcode an image blob in-browser so uploads clear the reverse
     proxy's request-body limit (nginx 1 MB / caddy 10 MB defaults) and go fast.
     Returns a JPEG blob of at most maxDim px on its longest side. Used by both
     the scan flow and the manual photo upload on the detail card. */
  W.resizeImageBlob = function (blob, maxDim, quality) {
    maxDim = maxDim || 1600;
    quality = quality || 0.82;
    return new Promise(function (resolve) {
      if (!window.createImageBitmap || !window.HTMLCanvasElement) {
        resolve(blob); // can't resize; send as-is
        return;
      }
      var url = URL.createObjectURL(blob);
      createImageBitmap(blob)
        .then(function (bitmap) {
          URL.revokeObjectURL(url);
          var w = bitmap.width, h = bitmap.height;
          var scale = Math.min(1, maxDim / Math.max(w, h));
          var cw = Math.max(1, Math.round(w * scale));
          var ch = Math.max(1, Math.round(h * scale));
          var canvas = document.createElement("canvas");
          canvas.width = cw;
          canvas.height = ch;
          var ctx = canvas.getContext("2d");
          ctx.drawImage(bitmap, 0, 0, cw, ch);
          bitmap.close && bitmap.close();
          canvas.toBlob(
            function (out) { resolve(out || blob); },
            "image/jpeg",
            quality
          );
        })
        .catch(function () {
          URL.revokeObjectURL(url);
          resolve(blob); // decoding failed; send as-is
        });
    });
  };
})();
