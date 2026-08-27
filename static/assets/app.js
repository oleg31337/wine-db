/* App shell: auth, tabs, search, favorites, backup, account. */
(function () {
  "use strict";
  var W = window.WineDB;
  var el = W.el;
  var $ = W.$;

  var state = {
    q: "",
    types: [],
    country: "",
    region: "",
    minRating: "",
    ratingScope: "average",
    unratedByMe: false,
    sort: "created",
    order: "desc",
    offset: 0,
    limit: 24,
    total: 0,
    loading: false,
    facets: { countries: [], regions: [] },
    currentList: null,
  };

  /* ---------------- views ---------------- */
  W.switchView = function (name) {
    // The Data tab (backup + user management) is admin-only.
    if (name === "data" && !W.isAdmin) {
      W.toast("Only the administrator can open Data", "err");
      return;
    }
    W.$$(".view").forEach(function (v) {
      v.classList.toggle("active", v.id === "view-" + name);
    });
    W.$$(".tabbar button").forEach(function (b) {
      b.setAttribute("aria-selected", b.dataset.view === name ? "true" : "false");
    });
    if (name !== "scan" && W.stopCamera) W.stopCamera();
    if (name === "favorites") loadFavoriteLists();
    if (name === "data") loadUsers();
    window.scrollTo({ top: 0, behavior: "instant" });
  };

  /* ---------------- auth ---------------- */
  function showAuth(message) {
    $("#app-screen").classList.add("hidden");
    $("#auth-screen").classList.remove("hidden");
    if (message) $("#auth-error").textContent = message;
  }

  function showApp(user) {
    W.me = user;
    W.isAdmin = !!user.is_admin;
    $("#auth-screen").classList.add("hidden");
    $("#app-screen").classList.remove("hidden");
    $("#account-name").textContent = user.username;
    $("#account-info").textContent =
      "Signed in as " + user.username + (user.display_name ? " (" + user.display_name + ")" : "") + ".";
    // The Data tab (backup + user management) is admin-only.
    W.$$('.tabbar button[data-admin="1"]').forEach(function (b) {
      b.classList.toggle("hidden", !W.isAdmin);
    });
    $("#admin-badge").classList.toggle("hidden", !W.isAdmin);
    resetSearch();
    loadFacets();
  }

  function bindAuth() {
    $("#login-form").addEventListener("submit", function (ev) {
      ev.preventDefault();
      $("#auth-error").textContent = "";
      W.api
        .post("/api/auth/login", {
          allowUnauthorized: true,
          json: {
            username: $("#login-username").value.trim(),
            password: $("#login-password").value,
          },
        })
        .then(function (user) {
          $("#login-password").value = "";
          showApp(user);
        })
        .catch(function (err) {
          $("#auth-error").textContent = err.message;
        });
    });

    W.api.onUnauthorized(function () {
      W.$$(".modal-backdrop").forEach(function (m) { m.remove(); });
      showAuth("Your session ended — please sign in again.");
    });
  }

  /* ---------------- search ---------------- */
  function buildQuery() {
    var p = new URLSearchParams();
    if (state.q) p.set("q", state.q);
    state.types.forEach(function (t) { p.append("wine_type", t); });
    if (state.country) p.set("country", state.country);
    if (state.region) p.set("region", state.region);
    if (state.minRating) {
      p.set("min_rating", state.minRating);
      p.set("rating_scope", state.ratingScope);
    }
    if (state.unratedByMe) p.set("unrated_by_me", "true");
    p.set("sort", state.sort);
    p.set("order", state.order);
    p.set("limit", String(state.limit));
    p.set("offset", String(state.offset));
    return p.toString();
  }

  function renderResults(items, append) {
    var host = $("#wine-results");
    if (!append) W.clear(host);
    items.forEach(function (wine) {
      host.appendChild(
        W.miniCard(wine, function (id) {
          W.openWine(id, W.me, function () {
            refreshCurrent();
          });
        })
      );
    });
  }

  function loadWines(append) {
    if (state.loading) return;
    state.loading = true;
    var status = $("#results-status");
    W.clear(status).appendChild(W.loadingRow("Searching…"));

    W.api
      .get("/api/wines?" + buildQuery())
      .then(function (page) {
        state.total = page.total;
        W.clear(status);
        if (!page.items.length && !append) {
          $("#wine-results").innerHTML = "";
          status.appendChild(
            W.emptyState(
              "🍇",
              state.q || state.types.length || state.country || state.minRating
                ? "No wines match these filters."
                : "No wines yet — tap “Add wine” to scan your first bottle."
            )
          );
        } else {
          renderResults(page.items, append);
        }
        $("#result-count").textContent =
          page.total + (page.total === 1 ? " wine" : " wines");
        var shown = state.offset + page.items.length;
        $("#btn-load-more").classList.toggle("hidden", shown >= page.total);
        state.offset = shown;
      })
      .catch(function (err) {
        W.clear(status);
        W.errToast(err);
      })
      .then(function () {
        state.loading = false;
      });
  }

  function resetSearch() {
    state.offset = 0;
    loadWines(false);
  }

  function refreshCurrent() {
    var keep = state.offset;
    state.offset = 0;
    state.limit = Math.max(24, keep);
    loadWines(false);
    state.limit = 24;
    loadFacets();
    if (state.currentList) openFavoriteList(state.currentList);
  }

  // Guaranteed list refresh, callable from any handler (create / delete / edit)
  // regardless of how the wine was opened. Repaints Browse and, when a
  // favorites list is open, that list too.
  W.refreshBrowse = refreshCurrent;

  function loadFacets() {
    W.api
      .get("/api/wines/facets")
      .then(function (facets) {
        state.facets = facets;
        var cSel = $("#f-country");
        var current = cSel.value;
        W.clear(cSel).appendChild(el("option", { value: "", text: "Any country" }));
        facets.countries.forEach(function (c) {
          cSel.appendChild(el("option", { value: c.name, text: c.name + " (" + c.count + ")" }));
        });
        cSel.value = current;
        paintRegions();
      })
      .catch(function () {});
  }

  function paintRegions() {
    var rSel = $("#f-region");
    var current = rSel.value;
    W.clear(rSel).appendChild(el("option", { value: "", text: "Any region" }));
    (state.facets.regions || [])
      .filter(function (r) {
        return !state.country || r.country === state.country;
      })
      .forEach(function (r) {
        rSel.appendChild(el("option", { value: r.name, text: r.name + " (" + r.count + ")" }));
      });
    rSel.value = current;
  }

  function bindSearch() {
    var debounced = W.debounce(function () {
      state.q = $("#search-q").value.trim();
      resetSearch();
    }, 320);
    $("#search-q").addEventListener("input", debounced);
    $("#search-q").addEventListener("search", debounced);

    $("#btn-filters").addEventListener("click", function () {
      $("#filter-panel").classList.toggle("hidden");
    });

    W.$$("#type-chips .chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var t = chip.dataset.type;
        var idx = state.types.indexOf(t);
        if (idx === -1) state.types.push(t);
        else state.types.splice(idx, 1);
        chip.setAttribute("aria-pressed", idx === -1 ? "true" : "false");
        resetSearch();
      });
    });

    $("#f-country").addEventListener("change", function () {
      state.country = $("#f-country").value;
      state.region = "";
      paintRegions();
      resetSearch();
    });
    $("#f-region").addEventListener("change", function () {
      state.region = $("#f-region").value;
      resetSearch();
    });
    $("#f-rating").addEventListener("change", function () {
      state.minRating = $("#f-rating").value;
      resetSearch();
    });
    $("#f-sort").addEventListener("change", function () {
      var parts = $("#f-sort").value.split(":");
      state.sort = parts[0];
      state.order = parts[1];
      resetSearch();
    });
    $("#chip-rating-scope").addEventListener("click", function () {
      var on = state.ratingScope === "mine";
      state.ratingScope = on ? "average" : "mine";
      this.setAttribute("aria-pressed", on ? "false" : "true");
      if (state.minRating) resetSearch();
    });
    $("#chip-unrated").addEventListener("click", function () {
      state.unratedByMe = !state.unratedByMe;
      this.setAttribute("aria-pressed", state.unratedByMe ? "true" : "false");
      resetSearch();
    });
    $("#btn-clear-filters").addEventListener("click", function () {
      state.types = [];
      state.country = "";
      state.region = "";
      state.minRating = "";
      state.unratedByMe = false;
      state.ratingScope = "average";
      state.q = "";
      $("#search-q").value = "";
      $("#f-country").value = "";
      $("#f-region").value = "";
      $("#f-rating").value = "";
      W.$$("#type-chips .chip").forEach(function (c) { c.setAttribute("aria-pressed", "false"); });
      $("#chip-unrated").setAttribute("aria-pressed", "false");
      $("#chip-rating-scope").setAttribute("aria-pressed", "false");
      resetSearch();
    });

    $("#btn-load-more").addEventListener("click", function () {
      loadWines(true);
    });
  }

  /* ---------------- favorites ---------------- */
  function loadFavoriteLists() {
    var host = $("#fav-lists");
    W.clear(host).appendChild(W.loadingRow("Loading lists…"));
    W.api
      .get("/api/favorites")
      .then(function (lists) {
        W.clear(host);
        if (!lists.length) {
          host.appendChild(W.emptyState("★", "No favorites lists yet — create one to start grouping wines."));
          return;
        }
        lists.forEach(function (list) {
          host.appendChild(
            el("button", { type: "button", class: "fav-item", onclick: function () { openFavoriteList(list); } }, [
              el("span", { text: "★", "aria-hidden": "true" }),
              el("span", {}, [
                el("span", { class: "fav-name", text: list.name }),
                list.description ? el("span", { class: "muted", text: " — " + list.description }) : null,
              ]),
              el("span", { class: "fav-count", text: list.wine_count + " wines" }),
            ])
          );
        });
      })
      .catch(function (err) {
        W.clear(host);
        W.errToast(err);
      });
  }

  function openFavoriteList(list) {
    state.currentList = list;
    $("#fav-lists").classList.add("hidden");
    $("#fav-detail").classList.remove("hidden");
    $("#fav-detail-name").textContent = list.name;
    var host = $("#fav-wines");
    W.clear(host).appendChild(W.loadingRow("Loading wines…"));
    W.api
      .get("/api/favorites/" + list.id + "/wines?limit=100")
      .then(function (page) {
        W.clear(host);
        if (!page.items.length) {
          host.appendChild(W.emptyState("🍷", "This list is empty. Open a wine and use ★ Favorites to add it."));
          return;
        }
        page.items.forEach(function (wine) {
          host.appendChild(
            W.miniCard(wine, function (id) {
              W.openWine(id, W.me, function () { openFavoriteList(list); });
            })
          );
        });
      })
      .catch(function (err) {
        W.clear(host);
        W.errToast(err);
      });
  }

  function bindFavorites() {
    $("#btn-fav-back").addEventListener("click", function () {
      state.currentList = null;
      $("#fav-detail").classList.add("hidden");
      $("#fav-lists").classList.remove("hidden");
      loadFavoriteLists();
    });

    $("#btn-new-list").addEventListener("click", function () {
      var name = el("input", { maxlength: 80, placeholder: "e.g. Weeknight reds" });
      var desc = el("input", { maxlength: 300, placeholder: "Optional description" });
      var m = W.modal({
        title: "New favorites list",
        body: el("div", {}, [
          el("div", { class: "field" }, [el("label", { text: "Name" }), name]),
          el("div", { class: "field" }, [el("label", { text: "Description" }), desc]),
          el("p", { class: "hint", text: "Lists are shared with everyone using this database." }),
        ]),
        footer: [
          el("button", { type: "button", text: "Cancel", onclick: function () { m.close(); } }),
          el("button", {
            type: "button",
            class: "btn-primary",
            text: "Create list",
            onclick: function () {
              var payload = { name: (name.value || "").trim() };
              if (!payload.name) { W.toast("Give the list a name", "err"); return; }
              var d = (desc.value || "").trim();
              if (d) payload.description = d;
              W.api
                .post("/api/favorites", { json: payload })
                .then(function () {
                  m.close();
                  W.toast("List created", "ok");
                  loadFavoriteLists();
                })
                .catch(W.errToast);
            },
          }),
        ],
      });
    });

    $("#btn-rename-list").addEventListener("click", function () {
      if (!state.currentList) return;
      var name = el("input", { maxlength: 80, value: state.currentList.name });
      var m = W.modal({
        title: "Rename list",
        body: el("div", { class: "field" }, [el("label", { text: "Name" }), name]),
        footer: [
          el("button", { type: "button", text: "Cancel", onclick: function () { m.close(); } }),
          el("button", {
            type: "button",
            class: "btn-primary",
            text: "Save",
            onclick: function () {
              W.api
                .patch("/api/favorites/" + state.currentList.id, { json: { name: (name.value || "").trim() } })
                .then(function (list) {
                  m.close();
                  state.currentList = list;
                  $("#fav-detail-name").textContent = list.name;
                  W.toast("List renamed", "ok");
                })
                .catch(W.errToast);
            },
          }),
        ],
      });
    });

    $("#btn-delete-list").addEventListener("click", function () {
      if (!state.currentList) return;
      var list = state.currentList;
      W.confirm({
        title: "Delete “" + list.name + "”?",
        message: "The list is removed for everyone. The wines themselves are kept.",
        okText: "Delete list",
        danger: true,
      }).then(function (yes) {
        if (!yes) return;
        W.api
          .del("/api/favorites/" + list.id)
          .then(function () {
            state.currentList = null;
            $("#fav-detail").classList.add("hidden");
            $("#fav-lists").classList.remove("hidden");
            W.toast("List deleted", "ok");
            loadFavoriteLists();
          })
          .catch(W.errToast);
      });
    });
  }

  /* ---------------- data / account (admin) ---------------- */
  function loadUsers() {
    var host = $("#user-list");
    W.clear(host);
    host.appendChild(el("p", { class: "muted", text: "Loading…" }));
    W.api
      .get("/api/auth/users")
      .then(function (users) {
        W.clear(host);
        if (!users.length) {
          host.appendChild(el("p", { class: "muted", text: "No other accounts yet." }));
          return;
        }
        users.forEach(function (u) {
          var row = el("div", { class: "user-row" }, [
            el("div", {}, [
              el("span", { class: "user-name", text: u.username }),
              u.display_name ? el("span", { class: "muted", text: " (" + u.display_name + ")" }) : null,
              u.is_admin ? el("span", { class: "badge badge-admin", text: "admin" }) : null,
            ]),
          ]);
          if (!u.is_admin) {
            row.appendChild(
              el("button", {
                type: "button",
                class: "btn-sm btn-quiet",
                text: "Remove",
                onclick: function () { removeUser(u); },
              })
            );
            row.appendChild(
              el("button", {
                type: "button",
                class: "btn-sm btn-quiet",
                text: "Reset password",
                onclick: function () { openResetPasswordModal(u); },
              })
            );
          }
          host.appendChild(row);
        });
      })
      .catch(function () { W.clear(host); });
  }

  function removeUser(user) {
    W.confirm({
      title: "Remove “" + user.username + "”?",
      message: "The account is deleted for everyone. Their ratings and comments stay attached to the wines.",
      okText: "Remove user",
      danger: true,
    }).then(function (yes) {
      if (!yes) return;
      W.api
        .del("/api/auth/users/" + user.id)
        .then(function () {
          W.toast("User removed", "ok");
          loadUsers();
        })
        .catch(W.errToast);
    });
  }

  function bindData() {
    $("#btn-export").addEventListener("click", function () {
      var btn = $("#btn-export");
      btn.disabled = true;
      W.api
        .get("/api/backup/export", { raw: true })
        .then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = el("a", { href: url, download: "wine-db-backup.zip" });
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
          W.toast("Backup downloaded", "ok");
        })
        .catch(W.errToast)
        .then(function () { btn.disabled = false; });
    });

    $("#restore-file").addEventListener("change", function (ev) {
      var file = ev.target.files && ev.target.files[0];
      $("#restore-name").textContent = file ? file.name : "";
      $("#btn-import").disabled = !file;
    });

    $("#btn-import").addEventListener("click", function () {
      var file = $("#restore-file").files && $("#restore-file").files[0];
      if (!file) return;
      var mode = $("#restore-mode").value;
      W.confirm({
        title: mode === "replace" ? "Replace all data?" : "Merge backup into the database?",
        message:
          mode === "replace"
            ? "Every wine, rating, comment and list currently in the database will be deleted and replaced by the backup."
            : "Wines, ratings, comments and lists from the backup will be added to what is already here.",
        okText: mode === "replace" ? "Replace everything" : "Merge",
        danger: mode === "replace",
      }).then(function (yes) {
        if (!yes) return;
        var fd = new FormData();
        fd.append("file", file, file.name);
        fd.append("mode", mode);
        var btn = $("#btn-import");
        btn.disabled = true;
        W.toast("Restoring…");
        W.api
          .post("/api/backup/import", { form: fd })
          .then(function (result) {
            var n = result.imported || {};
            W.toast("Restored " + (n.wines || 0) + " wines, " + (n.comments || 0) + " comments", "ok", 6000);
            $("#restore-file").value = "";
            $("#restore-name").textContent = "";
            resetSearch();
            loadFacets();
          })
          .catch(W.errToast)
          .then(function () { btn.disabled = false; });
      });
    });

    $("#btn-add-user").addEventListener("click", function () {
      var username = $("#new-username").value.trim();
      var password = $("#new-password").value;
      var display = $("#new-display").value.trim();
      if (!username || !password) { W.toast("Enter a username and a password", "err"); return; }
      var payload = { username: username, password: password };
      if (display) payload.display_name = display;
      W.api
        .post("/api/auth/users", { json: payload })
        .then(function (u) {
          $("#new-username").value = "";
          $("#new-display").value = "";
          $("#new-password").value = "";
          W.toast("User “" + u.username + "” added", "ok");
          loadUsers();
        })
        .catch(W.errToast);
    });

    $("#btn-change-pw").addEventListener("click", function () {
      var current = $("#pw-current").value;
      var next = $("#pw-new").value;
      if (!current || !next) { W.toast("Fill both password fields", "err"); return; }
      W.api
        .post("/api/auth/password", { json: { current_password: current, new_password: next } })
        .then(function () {
          $("#pw-current").value = "";
          $("#pw-new").value = "";
          W.toast("Password changed", "ok");
        })
        .catch(W.errToast);
    });

    $("#btn-logout").addEventListener("click", function () {
      W.api
        .post("/api/auth/logout")
        .catch(function () {})
        .then(function () {
          location.reload();
        });
    });

    $("#btn-account").addEventListener("click", function () {
      if (W.isAdmin) {
        W.switchView("data");
        return;
      }
      // Non-admins have no Data page; the gear opens an account sheet with
      // sign-out and (now) password change so they are never locked out of
      // either action.
      var m = W.modal({
        title: "Account",
        body: el("div", {}, [
          el("p", { class: "muted", text: "Signed in as " + (W.me ? W.me.username : "") + "." }),
        ]),
        footer: [
          el("button", {
            type: "button",
            text: "Change password",
            onclick: function () {
              m.close();
              openChangePasswordModal();
            },
          }),
          el("button", { type: "button", text: "Close", onclick: function () { m.close(); } }),
          el("button", {
            type: "button",
            class: "btn-quiet",
            text: "Sign out",
            onclick: function () {
              m.close();
              W.api.post("/api/auth/logout").catch(function () {}).then(function () { location.reload(); });
            },
          }),
        ],
      });
    });
  }

  /* Self-service password change (available to every signed-in user). */
  function openChangePasswordModal() {
    var cur = el("input", {
      id: "cpw-current", type: "password",
      autocomplete: "current-password", maxlength: 128,
    });
    var neu = el("input", {
      id: "cpw-new", type: "password",
      autocomplete: "new-password", maxlength: 128,
    });
    var m = W.modal({
      title: "Change your password",
      body: el("div", {}, [
        el("div", { class: "field" }, [
          el("label", { for: "cpw-current", text: "Current password" }),
          cur,
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "cpw-new", text: "New password" }),
          neu,
        ]),
        el("p", {
          class: "hint",
          text: "At least 12 characters, mixing 3 of: lower, upper, digit, symbol.",
        }),
      ]),
      footer: [
        el("button", { type: "button", text: "Cancel", onclick: function () { m.close(); } }),
        el("button", {
          type: "button", class: "btn-primary", text: "Change password",
          onclick: function () {
            var payload = { current_password: cur.value, new_password: neu.value };
            if (!payload.current_password || !payload.new_password) {
              W.toast("Enter both passwords", "err");
              return;
            }
            m.close();
            W.api
              .post("/api/auth/password", payload)
              .then(function () { W.toast("Password changed", "ok"); })
              .catch(W.errToast);
          },
        }),
      ],
    });
  }

  /* Admin reset of another user's password (no current-password check). */
  function openResetPasswordModal(user) {
    var neu = el("input", {
      id: "rpw-new", type: "password",
      autocomplete: "new-password", maxlength: 128,
    });
    var m = W.modal({
      title: "Reset password for “" + user.username + "”",
      body: el("div", {}, [
        el("div", { class: "field" }, [
          el("label", { for: "rpw-new", text: "New password" }),
          neu,
        ]),
        el("p", {
          class: "hint",
          text: "At least 12 characters, mixing 3 of: lower, upper, digit, symbol. Their existing sessions will be signed out.",
        }),
      ]),
      footer: [
        el("button", { type: "button", text: "Cancel", onclick: function () { m.close(); } }),
        el("button", {
          type: "button", class: "btn-primary", text: "Reset password",
          onclick: function () {
            var pw = neu.value;
            if (!pw) {
              W.toast("Enter a new password", "err");
              return;
            }
            m.close();
            W.api
              .put("/api/auth/users/" + user.id + "/password", { json: { new_password: pw } })
              .then(function () { W.toast("Password reset for “" + user.username + "”", "ok"); })
              .catch(W.errToast);
          },
        }),
      ],
    });
  }

  /* ---------------- boot ---------------- */
  function boot() {
    bindAuth();
    bindSearch();
    bindFavorites();
    bindData();
    W.initScan(function () { refreshCurrent(); });

    W.$$(".tabbar button").forEach(function (b) {
      b.addEventListener("click", function () { W.switchView(b.dataset.view); });
    });

    W.api
      .get("/api/auth/me", { allowUnauthorized: true })
      .then(function (user) { showApp(user); })
      .catch(function () { showAuth(""); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
