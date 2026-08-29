/* Camera capture + AI label reading -> new wine card. */
(function () {
  "use strict";
  var W = window.WineDB;
  var el = W.el;

  var stream = null;
  var facing = "environment";
  var capturedBlob = null;
  var manualMode = false;
  var isBackLabel = false;
  var refreshHook = null;
  // The in-progress new-wine form. Kept at module scope so a back-label scan
  // can MERGE its data into the same card instead of opening a fresh one and
  // silently dropping what the front label already filled in.
  var currentForm = null;
  // Raw text read from the back label, persisted with the wine on save.
  var pendingBackText = "";

  function $(id) {
    return W.$(id);
  }

  function hint(text) {
    var node = $("#scan-hint");
    if (!text) {
      node.classList.add("hidden");
      return;
    }
    node.classList.remove("hidden");
    node.textContent = text;
  }

  // Phone camera photos are often several MB; the reverse proxy in front of
  // the app usually caps request bodies well below that (nginx defaults to
  // 1 MB). Resize/transcode the image in the browser before upload so it
  // clears typical body-size limits and uploads fast. (Shared helper in core.js.)
  function resizeImageBlob(blob, maxDim, quality) {
    return W.resizeImageBlob(blob, maxDim, quality);
  }

  function stopCamera() {
    if (stream) {
      stream.getTracks().forEach(function (t) {
        t.stop();
      });
      stream = null;
    }
    $("#scan-video").classList.add("hidden");
    $("#scan-reticle").classList.add("hidden");
    $("#btn-capture").classList.add("hidden");
    $("#btn-cam-stop").classList.add("hidden");
    $("#btn-cam-flip").classList.add("hidden");
    $("#btn-cam-start").classList.remove("hidden");
    $("#scan-idle").classList.remove("hidden");
    hint("");
  }

  function secureContext() {
    // getUserMedia is only exposed on secure origins: HTTPS, or localhost /
    // 127.0.0.1. A plain-HTTP LAN address (e.g. http://10.0.x.x:port)
    // is NOT secure, so navigator.mediaDevices is undefined and the live
    // camera can never open from there. "Choose a photo" still works because
    // it goes through the OS camera picker via <input capture>, not getUserMedia.
    return (
      window.isSecureContext === true ||
      location.hostname === "localhost" ||
      location.hostname === "127.0.0.1" ||
      location.hostname === "[::1]"
    );
  }

  function cameraSupported() {
    return secureContext() && !!navigator.mediaDevices && !!navigator.mediaDevices.getUserMedia;
  }

  function startCamera() {
    var idle = $("#scan-idle");
    function showIdle(msg) {
      if (idle) {
        idle.textContent = msg;
        idle.classList.remove("hidden");
      }
    }
    if (!cameraSupported()) {
      var msg = secureContext()
        ? "Camera not available in this browser — use “Choose a photo” instead."
        : "Live camera needs a secure (HTTPS) connection. Open the app through your HTTPS address, or use “Choose a photo”.";
      showIdle(msg);
      W.toast(msg, "err");
      return;
    }
    stopCamera();
    navigator.mediaDevices
      .getUserMedia({
        video: { facingMode: facing, width: { ideal: 1280 }, height: { ideal: 1706 } },
        audio: false,
      })
      .then(function (s) {
        stream = s;
        var video = $("#scan-video");
        video.srcObject = s;
        video.play();
        video.classList.remove("hidden");
        $("#scan-preview").classList.add("hidden");
        $("#scan-idle").classList.add("hidden");
        $("#scan-reticle").classList.remove("hidden");
        $("#btn-capture").classList.remove("hidden");
        $("#btn-cam-stop").classList.remove("hidden");
        $("#btn-cam-flip").classList.remove("hidden");
        $("#btn-cam-start").classList.add("hidden");
        hint("Capture the label — or choose a photo below.");
      })
      .catch(function (err) {
        var msg =
          err && err.name === "NotAllowedError"
            ? "Camera permission was denied — use “Choose a photo” instead."
            : "Camera could not start — use “Choose a photo” instead.";
        showIdle(msg);
        W.toast(msg, "err");
      });
  }

  function grabFrame() {
    var video = $("#scan-video");
    if (!video || video.readyState < 2) return Promise.resolve(null);
    var canvas = document.createElement("canvas");
    var w = video.videoWidth || 1280;
    var h = video.videoHeight || 1706;
    var scale = Math.min(1, 1600 / Math.max(w, h));
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    return new Promise(function (resolve) {
      canvas.toBlob(
        function (blob) {
          resolve(blob);
        },
        "image/jpeg",
        0.88
      );
    });
  }

  function showPreview(blob) {
    var img = $("#scan-preview");
    if (img.src && img.src.indexOf("blob:") === 0) URL.revokeObjectURL(img.src);
    img.src = URL.createObjectURL(blob);
    img.classList.remove("hidden");
    $("#scan-video").classList.add("hidden");
    $("#scan-idle").classList.add("hidden");
  }

  /* Attach a photo in manual-add mode WITHOUT any AI enrichment. Just resize
     it in-browser, keep it as the capturedBlob (uploaded on save), and show a
     thumbnail. The user fills the card's text fields themselves. */
  function attachManualPhoto(file, thumb, pill) {
    W.toast("Attaching photo…", "ok", 1500);
    resizeImageBlob(file, 1600, 0.82)
      .then(function (resized) {
        if (!resized) resized = file;
        capturedBlob = resized;
        if (thumb) {
          if (thumb.src && thumb.src.indexOf("blob:") === 0) URL.revokeObjectURL(thumb.src);
          thumb.src = URL.createObjectURL(resized);
          thumb.classList.remove("hidden");
        }
        if (pill) pill.textContent = "🖼 Change photo";
        W.toast("Photo attached", "ok");
      })
      .catch(function () {
        W.toast("Could not read that photo", "err");
      });
  }

  /* Send the photo to the server for vision + internet enrichment. */
  function analyze(blob, manual) {
    // The back label is scanned for its TEXT only; the wine picture in the
    // database must stay the FRONT label, so never let a back-label capture
    // overwrite the captured photo.
    if (!isBackLabel) capturedBlob = blob;
    showPreview(blob);
    var panel = $("#suggest-panel");
    panel.classList.remove("hidden");
    W.clear(panel).appendChild(
      el("div", { class: "card" }, [W.loadingRow("Reading the label with the AI model…")])
    );

    // Shrink the image in-browser so it clears the reverse proxy's
    // request-body limit (and uploads quickly). The original may be several MB.
    resizeImageBlob(blob, 1600, 0.82)
      .then(function (resized) {
        if (!isBackLabel) capturedBlob = resized;
        if (resized && resized.size > 10 * 1024 * 1024) {
          W.clear(panel).appendChild(
            el("div", { class: "card" }, [
              el("p", {
                class: "muted",
                text:
                  "That image is still too large to upload. Reduce its size or use a smaller photo.",
              }),
              el("button", {
                type: "button",
                class: "btn-primary",
                text: "Fill the card by hand",
                onclick: function () {
                  openCardWithSuggestion({ suggestion: {}, messages: [], sources: [] }, manual);
                },
              }),
            ])
          );
          return;
        }
        var fd = new FormData();
        fd.append("file", resized, "label.jpg");
        fd.append("is_back_label", isBackLabel ? "true" : "false");

        return W.api.post("/api/scan/label", { form: fd })
          .then(function (result) {
            openCardWithSuggestion(result, manual);
          });
      })
      .catch(function (err) {
        W.clear(panel).appendChild(
          el("div", { class: "card" }, [
            el("p", { class: "muted", text: "The label could not be analysed: " + err.message }),
            el("button", {
              type: "button",
              class: "btn-primary",
              text: "Fill the card by hand",
              onclick: function () {
                openCardWithSuggestion({ suggestion: {}, messages: [], sources: [] });
              },
            }),
          ])
        );
      });
  }

  /* Renders the "this wine might already exist" banner when the vision model read
     a name that matches an existing wine in the collection (matched on name/maker,
     never the vintage/year). Each match links to the real, full wine card. */
  function duplicateBanner(result, manual) {
    var matches = result.existing_matches || [];
    if (!matches.length) return null;
    var list = el("div", { class: "dup-list" });
    matches.forEach(function (w) {
      var sub = [w.maker || "Unknown maker"];
      if (w.vintage) sub.push(String(w.vintage));
      if (w.region) sub.push(w.region);
      var left = el("div", { class: "dup-info" }, [
        el("div", { class: "dup-name", text: w.name }),
        el("div", { class: "muted", text: sub.join(" · ") }),
      ]);
      var open = el("button", {
        type: "button",
        class: "btn-sm btn-primary",
        text: "Open existing card",
        onclick: function () {
          // Close this scan modal and open the real wine detail sheet.
          W.$$(".modal-backdrop").forEach(function (m) { m.remove(); });
          if (W.openWine) W.openWine(w.id, W.me, function () { if (refreshHook) refreshHook(); });
          else W.toast("Could not open that wine", "err");
        },
      });
      list.appendChild(el("div", { class: "dup-row" }, [left, open]));
    });

    var title =
      matches.length === 1
        ? "This wine may already be in your collection"
        : matches.length + " wines like this may already be in your collection";
    return el("div", { class: "dup-banner" }, [
      el("p", { class: "dup-head", text: title }),
      el("p", { class: "muted", text: "Matching is by name only — the vintage is ignored. Open one to add your tasting notes to the existing card instead of creating a duplicate." }),
      list,
    ]);
  }

  function suggestionSummary(result, form, manual) {
    var box = el("div", { class: "suggest-box" });
    var keys = Object.keys(result.suggestion || {});
    box.appendChild(
      el("h3", {
        text: keys.length
          ? "Suggested for the empty fields (" + keys.length + ")"
          : "Nothing could be filled in automatically",
      })
    );
    (result.messages || []).forEach(function (m) {
      box.appendChild(el("p", { class: "muted", text: m }));
    });
    // Surface potential duplicates at the TOP of the suggestion panel, right
    // after the heading, so the user sees them before the "fill the card" controls.
    if (!manual && (result.existing_matches || []).length) {
      var banner = duplicateBanner(result, manual);
      if (banner) {
        // box.children[0] is the <h3> heading; insert the banner after it.
        box.insertBefore(banner, box.children[1] || null);
      }
    }
    if (result.sources && result.sources.length) {
      box.appendChild(
        el("p", { class: "hint", text: "Sources: " + result.sources.slice(0, 4).join(", ") })
      );
    }
    if (keys.length) {
      box.appendChild(
        el("p", { class: "hint", text: "Applied to empty fields only — review and correct anything wrong." })
      );
    }
    // When adding manually, "Select photo" only ATTACHES the image - it must
    // not trigger AI label reading / web enrichment. The whole point of manual
    // add is that the user fills the card themselves.
    if (manual) {
      var thumb = el("img", {
        id: "manual-thumb",
        alt: "Selected wine label photo",
        class: "manual-thumb hidden",
      });
      var photoPill = el("label", {
        class: "btn-sm btn-primary",
        for: "manual-photo",
        style: "cursor:pointer",
        text: "🖼 Select photo",
      });
      var photoInput = el("input", {
        id: "manual-photo",
        type: "file",
        accept: "image/*",
        class: "sr-only",
      });
      box.appendChild(
        el("div", { class: "field", style: "margin-top:0.5rem" }, [thumb, photoPill, photoInput])
      );
      photoInput.addEventListener("change", function (ev) {
        var file = ev.target.files && ev.target.files[0];
        if (!file) return;
        if (file.size > 12 * 1024 * 1024) {
          W.toast("That image is too large (max ~8 MB after processing).", "err");
          return;
        }
        attachManualPhoto(file, thumb, photoPill);
        ev.target.value = "";
      });
    } else if (!isBackLabel) {
      // Always offer to also scan the back label: it usually carries the region,
      // grape, alcohol and sugar that the front rarely lists. Skipped when we are
      // already processing a back label.
      box.appendChild(
        el("div", { class: "pill-row", style: "margin-top:0.5rem" }, [
          el("button", {
            type: "button",
            class: "btn-sm btn-primary",
            text: "📷 Scan the back label",
            onclick: function () { startBackLabelScan(form); },
          }),
        ])
      );
    } else if (result.back_label_text) {
      box.appendChild(
        el("p", { class: "hint", text: "Back label scanned and merged into the empty fields." })
      );
    }
    return box;
  }

  /* Re-point the camera at the back label and merge its reading into the
     same card the user is building, so front-label data is preserved.
     Switching to the scan view auto-starts the camera (see W.switchView). */
  function startBackLabelScan(form) {
    currentForm = form;
    isBackLabel = true;
    W.$$(".modal-backdrop").forEach(function (m) { m.remove(); });
    W.switchView("scan");
    W.toast("Point the camera at the BACK label", "ok");
  }

  /* Opens the new-wine card, pre-filled with the suggestion. When a back-label
     scan returns, the SAME form (currentForm) is reused and the back suggestion
     is merged into its still-empty fields - front-label data is never lost. */
  function openCardWithSuggestion(result, manual) {
    var panel = $("#suggest-panel");
    panel.classList.add("hidden");
    W.clear(panel);

    var form;
    var applied;
    if (currentForm && isBackLabel) {
      // Merge the back label's reading into the card we already built.
      form = currentForm;
      applied = form.applySuggestion(result.suggestion || {});
      pendingBackText = result.back_label_text || pendingBackText || "";
    } else {
      currentForm = null;
      pendingBackText = "";
      var seed = {};
      form = W.wineForm(seed);
      applied = form.applySuggestion(result.suggestion || {});
      currentForm = form;
    }

    var body = el("div", {}, [suggestionSummary(result, form, manual), el("div", { style: "height:0.9rem" }), form.node]);

    var save = el("button", {
      type: "button",
      class: "btn-primary",
      text: "Save wine",
      onclick: function () {
        var problem = form.validate();
        if (problem) {
          W.toast(problem, "err");
          return;
        }
        save.disabled = true;
        var payload = form.read();
        if (pendingBackText) payload.back_label_text = pendingBackText;
        W.api
          .post("/api/wines", { json: payload })
          .then(function (wine) {
            if (!capturedBlob) return wine;
            var fd = new FormData();
            fd.append("file", capturedBlob, "label.jpg");
            return W.api
              .put("/api/wines/" + wine.id + "/photo", { form: fd })
              .then(function () { return wine; })
              .catch(function () {
                W.toast("Wine saved, but the photo upload failed", "err");
                return wine;
              });
          })
          .then(function (wine) {
            m.close();
            reset();
            W.toast("“" + wine.name + "” added", "ok");
            if (refreshHook) refreshHook();
            // Always repaint the list so the new wine shows up, even when the
            // card wasn't opened from Browse/Favorites (Manual Add / scan).
            if (W.refreshBrowse) W.refreshBrowse();
            // Return to Browse so the new wine shows up in the list.
            W.switchView("browse");
          })
          .catch(function (err) {
            save.disabled = false;
            W.errToast(err);
          });
      },
    });

    var m = W.modal({
      title: "New wine card",
      body: body,
      footer: [
        el("button", { type: "button", text: "Cancel", onclick: function () { m.close(); } }),
        save,
      ],
    });
    if (applied.length) W.toast("Filled " + applied.length + " empty field(s)", "ok");
    if (!form.inputs.name.value) form.focusName();
  }

  function reset() {
    capturedBlob = null;
    isBackLabel = false;
    currentForm = null;
    pendingBackText = "";
    manualMode = false;
    $("#scan-preview").classList.add("hidden");
    $("#suggest-panel").classList.add("hidden");
    stopCamera();
  }

  W.initScan = function (onChanged) {
    refreshHook = onChanged;

    var startBtn = W.$("#btn-cam-start");
    if (!cameraSupported()) {
      // On a plain-HTTP LAN address the live camera can never work, so don't
      // advertise a button that always fails. The user can still take a photo
      // via "Choose a photo". The button stays visible but disabled + explained.
      startBtn.disabled = true;
      startBtn.title = secureContext()
        ? "Camera unavailable in this browser."
        : "Live camera needs HTTPS — open the app via your secure address, or use 'Choose a photo'.";
      startBtn.textContent = "Start camera (needs HTTPS)";
      startBtn.classList.add("btn-disabled");
    }
    startBtn.addEventListener("click", startCamera);
    $("#btn-cam-stop").addEventListener("click", stopCamera);
    $("#btn-cam-flip").addEventListener("click", function () {
      facing = facing === "environment" ? "user" : "environment";
      startCamera();
    });
    $("#btn-capture").addEventListener("click", function () {
      grabFrame().then(function (blob) {
        if (!blob) {
          W.toast("Could not capture a frame", "err");
          return;
        }
        stopCamera();
        analyze(blob);
      });
    });

    $("#file-input").addEventListener("change", function (ev) {
      var file = ev.target.files && ev.target.files[0];
      if (!file) return;
      if (file.size > 12 * 1024 * 1024) {
        W.toast("That image is too large (max ~8 MB after processing).", "err");
        return;
      }
      analyze(file);
      ev.target.value = "";
    });

    $("#btn-manual-add").addEventListener("click", function () {
      if (W.manualAdd) W.manualAdd();
    });

    W.stopCamera = stopCamera;
    W.startCamera = startCamera;
    W.manualAdd = function () {
      reset();
      manualMode = true;
      openCardWithSuggestion({ suggestion: {}, messages: [], sources: [] }, true);
    };
  };
})();
