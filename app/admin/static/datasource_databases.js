(function () {
  var root = document.querySelector("[data-db-picker]");
  if (!root) return;
  var form = root.closest("form");
  var mode = root.getAttribute("data-mode");
  var refreshUrl = root.getAttribute("data-refresh-url");
  var csrf = root.getAttribute("data-csrf");
  var options = root.querySelector("[data-db-options]");
  var filter = root.querySelector("[data-db-filter]");
  var status = root.querySelector("[data-db-status]");
  var refreshButton = root.querySelector("[data-db-refresh]");
  var defaultSelect = form ? form.querySelector("[data-db-default]") : null;
  var submitButton = form ? form.querySelector('button[type="submit"]') : null;

  function boxes() {
    return Array.prototype.slice.call(
      options.querySelectorAll('input[type="checkbox"][name="databases"]')
    );
  }

  function checkedNames() {
    return boxes()
      .filter(function (box) {
        return box.checked;
      })
      .map(function (box) {
        return box.value;
      });
  }

  function setStatus(text, isError) {
    status.textContent = text;
    status.classList.toggle("error-text", Boolean(isError));
  }

  function rebuildDefault(preferred) {
    if (!defaultSelect) return;
    var names = checkedNames();
    var previous = preferred || defaultSelect.value;
    defaultSelect.replaceChildren();
    if (!names.length) {
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = window.tr("db_placeholder");
      defaultSelect.appendChild(empty);
      return;
    }
    names.forEach(function (name) {
      var option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      defaultSelect.appendChild(option);
    });
    defaultSelect.value = names.indexOf(previous) >= 0 ? previous : names[0];
  }

  function render(names, checkedSet) {
    options.replaceChildren();
    names.forEach(function (name) {
      var label = document.createElement("label");
      label.className = "db-option";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.name = "databases";
      box.value = name;
      box.checked = checkedSet.indexOf(name) >= 0;
      var text = document.createElement("span");
      text.className = "mono";
      text.textContent = name;
      label.appendChild(box);
      label.appendChild(text);
      options.appendChild(label);
    });
    if (filter) filter.value = "";
    Array.prototype.forEach.call(options.querySelectorAll(".db-option"), function (item) {
      item.hidden = false;
    });
  }

  function fail(message) {
    setStatus(message, true);
    if (mode === "edit") {
      refreshButton.disabled = true;
    }
    if (mode === "new" && submitButton) {
      submitButton.disabled = true;
    }
  }

  function refresh() {
    var data = new FormData();
    data.append("_csrf", csrf);
    if (mode === "new") {
      ["host", "port", "username", "password", "default_database"].forEach(function (name) {
        var field = form.querySelector('[name="' + name + '"]');
        data.append(name, field ? field.value : "");
      });
    }
    setStatus(window.tr("db_loading"), false);
    fetch(refreshUrl, { method: "POST", body: data, credentials: "same-origin" })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        if (!result.ok) {
          var error = result.body && result.body.error;
          fail((error && error.message) || window.tr("db_load_failed_short"));
          return;
        }
        var names = (result.body && result.body.databases) || [];
        var previous = checkedNames();
        if (mode === "new" && !previous.length) {
          var initial = defaultSelect ? defaultSelect.value : "";
          if (names.indexOf(initial) < 0) {
            var field = form.querySelector('[name="default_database"]');
            initial = field ? field.value : "";
          }
          previous = names.indexOf(initial) >= 0 ? [initial] : names.slice(0, 1);
        }
        render(names, previous);
        rebuildDefault();
        setStatus(window.tr("db_loaded", { n: names.length }), false);
        if (refreshButton) refreshButton.disabled = false;
        if (submitButton) submitButton.disabled = false;
      })
      .catch(function () {
        fail(window.tr("db_load_failed"));
      });
  }

  if (refreshButton) {
    refreshButton.addEventListener("click", refresh);
  }

  if (filter) {
    filter.addEventListener("input", function () {
      var term = filter.value.trim().toLowerCase();
      Array.prototype.forEach.call(options.querySelectorAll(".db-option"), function (item) {
        var name = item.querySelector("input").value.toLowerCase();
        item.hidden = Boolean(term) && name.indexOf(term) < 0;
      });
    });
  }

  var selectAll = root.querySelector("[data-db-all]");
  if (selectAll) {
    selectAll.addEventListener("click", function () {
      boxes().forEach(function (box) {
        if (!box.closest(".db-option").hidden) box.checked = true;
      });
      rebuildDefault();
    });
  }

  var clearAll = root.querySelector("[data-db-clear]");
  if (clearAll) {
    clearAll.addEventListener("click", function () {
      boxes().forEach(function (box) {
        if (!box.closest(".db-option").hidden) box.checked = false;
      });
      rebuildDefault();
    });
  }

  options.addEventListener("change", function (event) {
    if (event.target && event.target.name === "databases") {
      rebuildDefault();
    }
  });

  if (form) {
    form.addEventListener("submit", function (event) {
      if (!checkedNames().length) {
        event.preventDefault();
        setStatus(window.tr("db_select_first"), true);
        return;
      }
      if (defaultSelect && !defaultSelect.value) {
        event.preventDefault();
        setStatus(window.tr("db_default_required"), true);
      }
    });
  }

  if (mode === "new" && submitButton && !boxes().length) {
    submitButton.disabled = true;
  }
  rebuildDefault();
})();
