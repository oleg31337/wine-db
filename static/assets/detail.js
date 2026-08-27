/* Wine detail sheet: photo, facts, gauges, rating, comments, favorites. */
(function () {
  "use strict";
  var W = window.WineDB;
  var el = W.el;

  function factList(wine) {
    var facts = [
      ["Type", W.fmt.typeLabel(wine.wine_type)],
      ["Vintage", wine.vintage || "–"],
      ["Maker", wine.maker || "–"],
      ["Country", wine.country || "–"],
      ["Region", wine.region || "–"],
      ["Grape", wine.grape || "–"],
      ["Alcohol", wine.alcohol_pct !== null && wine.alcohol_pct !== undefined ? wine.alcohol_pct + " %" : "–"],
      ["Sugar", wine.sugar_g_l !== null && wine.sugar_g_l !== undefined ? wine.sugar_g_l + " g/L" : "–"],
      ["Barcode", wine.barcode || "–"],
    ];
    return el(
      "dl",
      { class: "facts" },
      facts.map(function (f) {
        return el("div", { class: "fact" }, [
          el("dt", { text: f[0] }),
          el("dd", { text: String(f[1]) }),
        ]);
      })
    );
  }

  function commentNode(wine, comment, me, refresh) {
    var isMine = comment.user_id === me.id;
    var actions = [];
    if (isMine) {
      actions.push(
        el("button", {
          type: "button",
          class: "btn-sm btn-quiet",
          text: "Edit",
          onclick: function () {
            editComment(wine, comment, refresh);
          },
        }),
        el("button", {
          type: "button",
          class: "btn-sm btn-quiet",
          text: "Delete",
          onclick: function () {
            W.confirm({
              title: "Delete your comment?",
              message: "This cannot be undone.",
              okText: "Delete",
              danger: true,
            }).then(function (yes) {
              if (!yes) return;
              W.api
                .del("/api/wines/" + wine.id + "/comments/" + comment.id)
                .then(function () {
                  W.toast("Comment deleted", "ok");
                  refresh();
                })
                .catch(W.errToast);
            });
          },
        })
      );
    }
    return el("div", { class: "comment" }, [
      el("div", { class: "comment-head" }, [
        el("span", { class: "comment-author", text: comment.username + (isMine ? " (you)" : "") }),
        el("span", { class: "comment-date", text: W.fmt.date(comment.created_at) }),
      ]),
      el("div", { class: "comment-body", text: comment.body }),
      actions.length ? el("div", { class: "comment-actions" }, actions) : null,
    ]);
  }

  function upsertMyRating(ratings, me, stars) {
    var list = (ratings || []).map(function (r) { return Object.assign({}, r); });
    var found = false;
    list.forEach(function (r) {
      if (r.user_id === me.id) { r.stars = stars; found = true; }
    });
    if (!found) list.push({ user_id: me.id, username: me.username, stars: stars });
    return list;
  }

  // "Your rating" picker + per-user "Ratings" list. Returns { node, update(wine) }
  // so a rating click repaints instantly (no full-card rebuild, no waiting on
  // the server) while the API call syncs the server truth in the background.
  function ratingsBlock(wine, onPick, onClear) {
    var picker = W.starInput(wine.my_rating, onPick, onClear);

    var yourText = el("span", { class: "muted" });
    function paintYour() {
      yourText.textContent = wine.my_rating
        ? "You rated " + wine.my_rating + " / 5"
        : "You haven't rated this wine yet";
    }
    paintYour();

    var list = el("div");
    function paintList() {
      var ratings = wine.ratings || [];
      W.clear(list).appendChild(
        ratings.length
          ? el(
              "ul",
              { class: "rating-list" },
              ratings.map(function (r) {
                return el("li", { class: "rating-row" }, [
                  el("span", { class: "rating-user", text: r.username }),
                  W.stars(r.stars),
                ]);
              })
            )
          : el("p", { class: "muted", text: "No ratings yet" })
      );
    }
    paintList();

    var node = el("div", {}, [
      el("div", { class: "card", style: "margin-top:1rem" }, [
        el("h3", { text: "Your rating" }),
        el("div", { class: "pill-row" }, [picker, yourText]),
      ]),
      el("div", { class: "card", style: "margin-top:1rem" }, [
        el("h3", { text: "Ratings (" + (wine.ratings || []).length + ")" }),
        list,
      ]),
    ]);

    // Re-render from a (possibly locally-mutated) wine object, in place.
    function update(updated) {
      wine = updated;
      var fresh = W.starInput(wine.my_rating, onPick, onClear);
      picker.replaceWith(fresh);
      picker = fresh;
      paintYour();
      paintList();
    }
    return { node: node, update: update };
  }

  function commentEditor(initial, onSave) {
    var box = el("textarea", {
      maxlength: String(W.COMMENT_MAX),
      placeholder: "How did it taste? Food pairing, occasion, whether you'd buy it again…",
    });
    box.value = initial || "";
    var count = el("span", { class: "hint char-count" });
    function paint() {
      count.textContent = box.value.length + " / " + W.COMMENT_MAX + " characters";
    }
    box.addEventListener("input", paint);
    paint();

    var save = el("button", {
      type: "button",
      class: "btn-primary",
      text: "Save comment",
      onclick: function () {
        var body = box.value.trim();
        if (!body) {
          W.toast("Write something first", "err");
          return;
        }
        save.disabled = true;
        onSave(body, function () {
          save.disabled = false;
        });
      },
    });
    return { node: el("div", { class: "field" }, [box, count, el("div", { class: "pill-row", style: "margin-top:0.5rem" }, [save])]), box: box };
  }

  function editComment(wine, comment, refresh) {
    var editor = commentEditor(comment.body, function (body, done) {
      W.api
        .patch("/api/wines/" + wine.id + "/comments/" + comment.id, { json: { body: body } })
        .then(function () {
          m.close();
          W.toast("Comment updated", "ok");
          refresh();
        })
        .catch(function (err) {
          done();
          W.errToast(err);
        });
    });
    var m = W.modal({ title: "Edit your comment", body: editor.node });
  }

  function favoritesPicker(wine, refresh) {
    W.api
      .get("/api/favorites")
      .then(function (lists) {
        var body = el("div");
        if (!lists.length) {
          body.appendChild(el("p", { class: "muted", text: "No lists yet — create one below." }));
        }
        var picks = el("div", { class: "fav-pick" });
        lists.forEach(function (list) {
          var inList = (wine.favorite_list_ids || []).indexOf(list.id) !== -1;
          var cb = el("input", { type: "checkbox", id: "fav-" + list.id });
          cb.checked = inList;
          cb.addEventListener("change", function () {
            var path = "/api/favorites/" + list.id + "/wines/" + wine.id;
            var call = cb.checked ? W.api.put(path) : W.api.del(path);
            call
              .then(function () {
                W.toast(cb.checked ? "Added to " + list.name : "Removed from " + list.name, "ok");
                refresh(true);
              })
              .catch(function (err) {
                cb.checked = !cb.checked;
                W.errToast(err);
              });
          });
          picks.appendChild(
            el("label", { class: "fav-item", for: "fav-" + list.id }, [
              cb,
              el("span", { class: "fav-name", text: list.name }),
              el("span", { class: "fav-count", text: list.wine_count + " wines" }),
            ])
          );
        });
        body.appendChild(picks);

        var newName = el("input", { placeholder: "New list name", maxlength: 80 });
        body.appendChild(
          el("div", { class: "field", style: "margin-top:0.9rem" }, [
            el("label", { text: "Create a new list" }),
            el("div", { class: "pill-row" }, [
              newName,
              el("button", {
                type: "button",
                class: "btn-sm btn-primary",
                text: "Create & add",
                onclick: function () {
                  var name = (newName.value || "").trim();
                  if (!name) return;
                  W.api
                    .post("/api/favorites", { json: { name: name } })
                    .then(function (list) {
                      return W.api.put("/api/favorites/" + list.id + "/wines/" + wine.id);
                    })
                    .then(function () {
                      m.close();
                      W.toast("Added to new list", "ok");
                      refresh(true);
                    })
                    .catch(W.errToast);
                },
              }),
            ]),
          ])
        );

        var m = W.modal({ title: "Favorites lists", body: body });
      })
      .catch(W.errToast);
  }

  /* Opens the detail sheet for a wine id. onChanged() lets the caller refresh lists. */
  W.openWine = function (wineId, me, onChanged) {
    var handle = W.modal({ title: "Loading…", body: W.loadingRow("Fetching the wine card…") });

    function refresh(silent) {
      if (typeof onChanged === "function") onChanged();
      W.api
        .get("/api/wines/" + wineId)
        .then(function (wine) {
          paint(wine);
        })
        .catch(function (err) {
          if (!silent) W.errToast(err);
        });
    }

    function paint(wine) {
      var current = wine;
      var head = handle.panel.querySelector(".modal-head h2");
      head.textContent = wine.name;

      var photo = el("div", { class: "photo-frame" });
      if (wine.photo_url) {
        photo.appendChild(el("img", { src: wine.photo_url + "?v=" + Date.now(), alt: "Bottle photo of " + wine.name }));
      } else {
        photo.appendChild(el("span", { class: "placeholder", text: "🍷", "aria-hidden": "true" }));
      }

      var photoInput = el("input", { type: "file", accept: "image/*", capture: "environment", class: "sr-only", id: "detail-photo" });
      photoInput.addEventListener("change", function () {
        var file = photoInput.files && photoInput.files[0];
        if (!file) return;
        W.toast("Preparing photo…");
        // Resize in-browser so the upload clears the reverse proxy body limit.
        W.resizeImageBlob(file, 1600, 0.82).then(function (resized) {
          var fd = new FormData();
          fd.append("file", resized, file.name || "photo.jpg");
          W.toast("Uploading photo…");
          W.api
            .put("/api/wines/" + wine.id + "/photo", { form: fd })
            .then(function () {
              W.toast("Photo updated", "ok");
              refresh();
            })
            .catch(W.errToast);
        });
      });

      var ratingsBlockRef;
      function onPick(stars) {
        current = Object.assign({}, current, {
          my_rating: stars,
          ratings: upsertMyRating(current.ratings, me, stars),
        });
        if (ratingsBlockRef) ratingsBlockRef.update(current);
        W.api
          .put("/api/wines/" + wine.id + "/rating", { json: { stars: stars } })
          .then(function () { W.toast("Rated " + stars + "★", "ok"); })
          .catch(function (err) { W.errToast(err); refresh(); });
      }
      function onClear() {
        current = Object.assign({}, current, {
          my_rating: null,
          ratings: (current.ratings || []).filter(function (r) { return r.user_id !== me.id; }),
        });
        if (ratingsBlockRef) ratingsBlockRef.update(current);
        W.api
          .del("/api/wines/" + wine.id + "/rating")
          .then(function () { W.toast("Rating cleared", "ok"); })
          .catch(function (err) { W.errToast(err); refresh(); });
      }
      ratingsBlockRef = ratingsBlock(current, onPick, onClear);

      var editor = commentEditor("", function (body, done) {
        W.api
          .post("/api/wines/" + wine.id + "/comments", { json: { body: body } })
          .then(function () {
            W.toast("Comment saved", "ok");
            refresh();
          })
          .catch(function (err) {
            done();
            W.errToast(err);
          });
      });

      var body = el("div", {}, [
        el("div", { class: "detail-top" }, [
          el("div", {}, [
            photo,
            el("div", { class: "pill-row", style: "margin-top:0.5rem" }, [
              el("label", { class: "btn btn-sm", for: "detail-photo", style: "cursor:pointer", text: wine.photo_url ? "Replace photo" : "Add photo" }),
              photoInput,
              wine.photo_url
                ? el("button", {
                    type: "button",
                    class: "btn-sm btn-quiet",
                    text: "Remove",
                    onclick: function () {
                      W.api
                        .del("/api/wines/" + wine.id + "/photo")
                        .then(function () {
                          W.toast("Photo removed", "ok");
                          refresh();
                        })
                        .catch(W.errToast);
                    },
                  })
                : null,
            ]),
          ]),
          el("div", {}, [
            el("div", { class: "pill-row", style: "margin-bottom:0.5rem" }, [
              el("span", { class: "badge " + wine.wine_type, text: W.fmt.typeLabel(wine.wine_type) }),
              wine.vintage ? el("span", { class: "badge badge-year", text: String(wine.vintage) }) : null,
            ]),
            factList(wine),
            wine.aromas
              ? el("div", { style: "margin-top:0.8rem" }, [
                  el("h3", { text: "Aromas" }),
                  el("p", { class: "muted", text: wine.aromas }),
                ])
              : null,
            el("div", { style: "margin-top:0.9rem" }, [el("h3", { text: "Structure" }), W.gaugeBars(wine)]),
          ]),
        ]),

        ratingsBlockRef.node,

        el("div", { style: "margin-top:1rem" }, [
          el("h3", { text: "Tasting comments (" + (wine.comments || []).length + ")" }),
          editor.node,
          el(
            "div",
            {},
            (wine.comments || []).map(function (c) {
              return commentNode(wine, c, me, refresh);
            })
          ),
        ]),
      ]);

      var footer = [
        el("button", {
          type: "button",
          class: "btn-sm",
          text: "★ Favorites",
          onclick: function () {
            favoritesPicker(wine, refresh);
          },
        }),
        el("button", {
          type: "button",
          class: "btn-sm",
          text: "Edit fields",
          onclick: function () {
            W.editWine(wine, function () {
              refresh();
            });
          },
        }),
        el("button", {
          type: "button",
          class: "btn-sm btn-danger",
          text: "Delete wine",
          onclick: function () {
            W.confirm({
              title: "Delete “" + wine.name + "”?",
              message: "The wine, its photo, all ratings and all comments will be removed for everyone.",
              okText: "Delete wine",
              danger: true,
            }).then(function (yes) {
              if (!yes) return;
              W.api
                .del("/api/wines/" + wine.id)
                .then(function () {
                  handle.close();
                  W.toast("Wine deleted", "ok");
                  if (typeof onChanged === "function") onChanged();
                  // Always repaint the list so the removed wine disappears,
                  // even when the card wasn't opened from Browse/Favorites.
                  if (W.refreshBrowse) W.refreshBrowse();
                })
                .catch(W.errToast);
            });
          },
        }),
      ];

      W.clear(handle.body).appendChild(body);
      var foot = handle.panel.querySelector(".modal-foot");
      if (!foot) {
        foot = el("div", { class: "modal-foot" });
        handle.panel.appendChild(foot);
      }
      W.clear(foot);
      footer.forEach(function (b) {
        foot.appendChild(b);
      });
    }

    W.api
      .get("/api/wines/" + wineId)
      .then(paint)
      .catch(function (err) {
        handle.close();
        W.errToast(err);
      });
  };

  /* Edit-fields modal reusing the shared form. */
  W.editWine = function (wine, onSaved) {
    var form = W.wineForm(wine);
    var save = el("button", {
      type: "button",
      class: "btn-primary",
      text: "Save changes",
      onclick: function () {
        var problem = form.validate();
        if (problem) {
          W.toast(problem, "err");
          return;
        }
        save.disabled = true;
        W.api
          .patch("/api/wines/" + wine.id, { json: form.read() })
          .then(function () {
            m.close();
            W.toast("Wine updated", "ok");
            if (onSaved) onSaved();
          })
          .catch(function (err) {
            save.disabled = false;
            W.errToast(err);
          });
      },
    });
    var m = W.modal({
      title: "Edit wine card",
      body: form.node,
      footer: [
        el("button", { type: "button", text: "Cancel", onclick: function () { m.close(); } }),
        save,
      ],
    });
  };
})();
