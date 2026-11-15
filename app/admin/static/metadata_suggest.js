(function () {
  "use strict";

  var roots = document.querySelectorAll("[data-metadata-suggest]");
  if (!roots.length) {
    return;
  }

  var DEBOUNCE_MS = 180;
  var TRUNCATED_NOTE = window.tr("suggest_truncated");
  var menuSeq = 0;

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function init(root) {
    var kind = root.getAttribute("data-metadata-suggest");
    var form = root.closest("form");
    if (!form) {
      return;
    }
    var input = root.querySelector("input");
    var menu = root.querySelector(".metadata-menu");
    var datasourceId = form.getAttribute("data-datasource-id");
    if (!input || !menu || !datasourceId) {
      return;
    }
    var dbInput = form.querySelector('[name="database"]');
    var tableInput = form.querySelector('input[name="table"]');
    var columnInput = form.querySelector('input[name="column"]');

    menuSeq += 1;
    menu.id = "metadata-menu-" + menuSeq;
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", menu.id);

    var items = [];
    var active = -1;
    var timer = null;
    var seq = 0;
    var controller = null;

    function openMenu() {
      if (document.activeElement !== input || !menu.hidden) {
        return;
      }
      menu.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function closeMenu() {
      if (menu.hidden) {
        return;
      }
      menu.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function showNote(text, className) {
      menu.textContent = "";
      menu.appendChild(make("span", "metadata-note " + (className || ""), text));
    }

    function setActive(index) {
      var options = menu.querySelectorAll(".metadata-option");
      if (!options.length) {
        return;
      }
      if (index < 0) {
        index = options.length - 1;
      }
      if (index >= options.length) {
        index = 0;
      }
      options.forEach(function (option, position) {
        option.classList.toggle("active", position === index);
        option.setAttribute("aria-selected", position === index ? "true" : "false");
      });
      active = index;
      input.setAttribute("aria-activedescendant", options[index].id);
      options[index].scrollIntoView({ block: "nearest" });
    }

    function renderItems(list, truncated) {
      items = Array.isArray(list) ? list : [];
      menu.textContent = "";
      active = -1;
      if (!items.length) {
        showNote(
          window.tr(
            kind === "table" ? "suggest_empty_table" : "suggest_empty_column"
          ),
          "muted"
        );
        openMenu();
        return;
      }
      items.forEach(function (item, index) {
        var option = make("span", "metadata-option");
        option.id = menu.id + "-" + index;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.appendChild(
          make("code", "metadata-option-name", kind === "table" ? item.table : item.column)
        );
        var meta = "";
        if (kind === "table") {
          if (!dbInput || !dbInput.value.trim()) {
            meta = item.database || "";
          }
        } else {
          meta = item.data_type || "";
        }
        if (meta) {
          option.appendChild(make("span", "metadata-option-meta", meta));
        }
        menu.appendChild(option);
      });
      if (truncated) {
        menu.appendChild(make("span", "metadata-note muted", TRUNCATED_NOTE));
      }
      openMenu();
    }

    function load() {
      seq += 1;
      var current = seq;
      if (controller) {
        controller.abort();
        controller = null;
      }
      items = [];

      var database = dbInput ? dbInput.value.trim() : "";
      var table = tableInput ? tableInput.value.trim() : "";
      if (kind === "column" && !database) {
        showNote(window.tr("suggest_need_database"), "muted");
        openMenu();
        return;
      }
      if (kind === "column" && !table) {
        showNote(window.tr("suggest_need_table"), "muted");
        openMenu();
        return;
      }

      var params = ["q=" + encodeURIComponent(input.value.trim())];
      if (database) {
        params.push("database=" + encodeURIComponent(database));
      }
      if (kind === "column") {
        params.push("table=" + encodeURIComponent(table));
      }
      var path = kind === "table" ? "tables" : "columns";
      var url =
        "/admin/datasources/" +
        encodeURIComponent(datasourceId) +
        "/metadata/" +
        path +
        "?" +
        params.join("&");

      controller = typeof AbortController === "function" ? new AbortController() : null;
      showNote(window.tr("suggest_loading"), "muted");
      openMenu();
      fetch(url, {
        headers: { Accept: "application/json" },
        signal: controller ? controller.signal : undefined,
      })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("HTTP " + response.status);
          }
          return response.json();
        })
        .then(function (data) {
          if (current !== seq) {
            return;
          }
          renderItems(
            kind === "table" ? data.tables : data.columns,
            Boolean(data.truncated)
          );
        })
        .catch(function (error) {
          if (current !== seq || (error && error.name === "AbortError")) {
            return;
          }
          items = [];
          showNote(window.tr("suggest_failed"), "alert-error");
          openMenu();
        });
    }

    function scheduleLoad() {
      if (timer) {
        window.clearTimeout(timer);
      }
      timer = window.setTimeout(function () {
        timer = null;
        load();
      }, DEBOUNCE_MS);
    }

    function choose(index) {
      var item = items[index];
      if (!item) {
        return;
      }
      if (kind === "table") {
        var previous = input.value.trim();
        var table = String(item.table || "");
        var database = String(item.database || "");
        if (database && dbInput && !dbInput.value.trim()) {
          dbInput.value = database;
        }
        input.value = table;
        if (columnInput && columnInput.value.trim() && previous !== table) {
          columnInput.value = "";
        }
      } else {
        input.value = String(item.column || "");
      }
      closeMenu();
    }

    input.addEventListener("focus", load);
    input.addEventListener("input", scheduleLoad);
    input.addEventListener("blur", function () {
      window.setTimeout(closeMenu, 120);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (menu.hidden) {
          load();
        } else if (items.length) {
          setActive(active + 1);
        }
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (menu.hidden) {
          load();
        } else if (items.length) {
          setActive(active - 1);
        }
        return;
      }
      if (event.key === "Enter") {
        if (!menu.hidden && active >= 0) {
          event.preventDefault();
          choose(active);
        } else if (!menu.hidden) {
          closeMenu();
        }
        return;
      }
      if (event.key === "Escape" && !menu.hidden) {
        event.preventDefault();
        closeMenu();
      }
      if (event.key === "Tab") {
        closeMenu();
      }
    });

    menu.addEventListener("mousedown", function (event) {
      event.preventDefault();
    });
    menu.addEventListener("click", function (event) {
      var target = event.target.closest(".metadata-option");
      if (!target) {
        return;
      }
      var options = menu.querySelectorAll(".metadata-option");
      choose(Array.prototype.indexOf.call(options, target));
    });
    menu.addEventListener("mousemove", function (event) {
      var target = event.target.closest(".metadata-option");
      if (!target) {
        return;
      }
      var options = menu.querySelectorAll(".metadata-option");
      setActive(Array.prototype.indexOf.call(options, target));
    });

    document.addEventListener("click", function (event) {
      if (!root.contains(event.target)) {
        closeMenu();
      }
    });

    if (kind === "table" && dbInput) {
      dbInput.addEventListener("input", function () {
        if (!menu.hidden) {
          scheduleLoad();
        }
      });
    }
    if (kind === "column" && tableInput) {
      tableInput.addEventListener("input", function () {
        if (columnInput && columnInput.value) {
          columnInput.value = "";
        }
      });
    }
  }

  roots.forEach(init);
})();
