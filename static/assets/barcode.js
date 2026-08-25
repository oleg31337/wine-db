/* Barcode scanning.
 *
 * Uses the browser's native BarcodeDetector (Chrome/Edge/Android, Safari 17+ on
 * iOS behind the same API) so no third-party library is loaded - that keeps the
 * CSP strict with no external origins. When the API is missing we simply fall
 * back to the manual barcode field, which is always available.
 */
(function () {
  "use strict";

  var FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e", "code_128", "itf"];

  function supported() {
    return typeof window.BarcodeDetector === "function";
  }

  function createDetector() {
    if (!supported()) return null;
    try {
      return new window.BarcodeDetector({ formats: FORMATS });
    } catch (err) {
      try {
        return new window.BarcodeDetector();
      } catch (err2) {
        return null;
      }
    }
  }

  /* Digits only, plausible retail barcode length. */
  function plausible(raw) {
    if (!raw) return null;
    var digits = String(raw).replace(/\D/g, "");
    if (digits.length < 8 || digits.length > 14) return null;
    return digits;
  }

  /* Poll a <video> element until a barcode is seen or stop() is called. */
  function watch(video, onFound, intervalMs) {
    var detector = createDetector();
    if (!detector) return { stop: function () {}, supported: false };

    var stopped = false;
    var busy = false;

    var timer = setInterval(function () {
      if (stopped || busy || video.readyState < 2) return;
      busy = true;
      detector
        .detect(video)
        .then(function (codes) {
          busy = false;
          if (stopped || !codes || !codes.length) return;
          for (var i = 0; i < codes.length; i++) {
            var value = plausible(codes[i].rawValue);
            if (value) {
              onFound(value, codes[i].format);
              return;
            }
          }
        })
        .catch(function () {
          busy = false;
        });
    }, intervalMs || 400);

    return {
      supported: true,
      stop: function () {
        stopped = true;
        clearInterval(timer);
      },
    };
  }

  /* One-shot detection on a still image (Blob or ImageBitmapSource). */
  function detectInBlob(blob) {
    var detector = createDetector();
    if (!detector) return Promise.resolve(null);
    var loader =
      typeof createImageBitmap === "function"
        ? createImageBitmap(blob)
        : Promise.reject(new Error("no createImageBitmap"));
    return loader
      .then(function (bitmap) {
        return detector.detect(bitmap);
      })
      .then(function (codes) {
        if (!codes) return null;
        for (var i = 0; i < codes.length; i++) {
          var value = plausible(codes[i].rawValue);
          if (value) return value;
        }
        return null;
      })
      .catch(function () {
        return null;
      });
  }

  window.WineBarcode = {
    supported: supported,
    watch: watch,
    detectInBlob: detectInBlob,
    plausible: plausible,
  };
})();
