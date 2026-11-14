(function () {
  "use strict";

  var root = document.querySelector("[data-scope-picker]");
  if (!root) {
    return;
  }
  var tr = window.tr;

  var form = root.closest("form");
  var payloadInput = root.querySelector('input[name="scope_payload"]');
  var csrfToken = root.getAttribute("data-csrf") || "";
  var defaultMode =
    root.getAttribute("data-default-mode") === "specified" ? "specified" : "all";
  var currentTokenId = root.getAttribute("data-token-id") || "";

  var TABLE_PAGE = 50;
  var COLUMN_PAGE = 200;
  var IMPORT_LIMIT = 2000;
  var LIMITS = {
    datasources: 1000,
    tables: 10000,
    columns: 50000,
    payload: 2000000,
  };
  var TEMPLATE_KEY = "dbgw.scope.templates.v1";
  var SEP = "\u0000";

  var dsListEl = root.querySelector("[data-ds-list]");
  var dsBulkEl = root.querySelector("[data-ds-bulk]");
  var summaryTextEl = root.querySelector("[data-scope-summary-text]");
  var countsEl = root.querySelector("[data-scope-counts]");
  var warningEl = root.querySelector("[data-scope-warning]");
  var allNoteEl = root.querySelector("[data-scope-all-note]");
  var limitsEl = root.querySelector("[data-scope-limits]");
  var modeInputs = root.querySelectorAll('input[name="scope_ds_mode"]');

  var dialogDepth = 0;
  var metaList = [];
  var metaById = new Map();

  var state = {
    mode: "all",
    sources: new Map(),
  };

  var initialPayload = { datasources: [], tables: [], columns: [] };
  var initialKey = "";
  var submitting = false;

  // ---------------------------------------------------------------- helpers

  function make(tag, className, text) {
    var element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== undefined && text !== null) {
      element.textContent = String(text);
    }
    return element;
  }

  function note(text, className) {
    return make("p", "scope-note " + (className || ""), text);
  }

  function tableKey(db, table) {
    return String(db) + SEP + String(table);
  }

  function columnKey(name) {
    return String(name).toLowerCase();
  }

  function sortNumbers(values) {
    return values.sort(function (left, right) {
      return left - right;
    });
  }

  function compareArrays(left, right) {
    var length = Math.min(left.length, right.length);
    for (var index = 0; index < length; index += 1) {
      if (left[index] < right[index]) {
        return -1;
      }
      if (left[index] > right[index]) {
        return 1;
      }
    }
    return left.length - right.length;
  }

  function newSource(selected) {
    return {
      selected: Boolean(selected),
      tableMode: "all",
      tables: new Map(),
      unrestrictedDatabases: new Set(),
    };
  }

  function cloneTables(tables) {
    var out = new Map();
    tables.forEach(function (entry, key) {
      out.set(key, {
        db: entry.db,
        table: entry.table,
        columnsMode: entry.columnsMode,
        columns: new Map(entry.columns),
      });
    });
    return out;
  }

  function newTableEntry(db, table) {
    return {
      db: String(db),
      table: String(table),
      columnsMode: "all",
      columns: new Map(),
    };
  }

  function ensureSource(id) {
    var source = state.sources.get(id);
    if (!source) {
      source = newSource(true);
      state.sources.set(id, source);
    }
    return source;
  }

  function checkJson(response) {
    return response
      .json()
      .catch(function () {
        return {};
      })
      .then(function (data) {
        if (!response.ok) {
          var message =
            (data && data.error && data.error.message) ||
            "HTTP " + response.status;
          throw new Error(message);
        }
        return data;
      });
  }

  function requestJson(url, options) {
    var settings = options || {};
    settings.headers = Object.assign(
      { Accept: "application/json" },
      settings.headers || {}
    );
    return fetch(url, settings).then(checkJson);
  }

  function pushDialog() {
    dialogDepth += 1;
    document.body.classList.add("scope-dialog-open");
  }

  function popDialog() {
    dialogDepth = Math.max(0, dialogDepth - 1);
    if (!dialogDepth) {
      document.body.classList.remove("scope-dialog-open");
    }
  }

  function trapFocus(event, container) {
    var nodes = container.querySelectorAll(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    var list = Array.prototype.filter.call(nodes, function (element) {
      return element.offsetParent !== null;
    });
    if (!list.length) {
      return;
    }
    var first = list[0];
    var last = list[list.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openDialog(options) {
    var overlay = make("div", "scope-overlay");
    var dialog = make(
      "div",
      "scope-dialog " +
        (options.variant === "modal" ? "scope-modal" : "scope-drawer")
    );
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    var head = make("div", "scope-dialog-head");
    var titleBox = make("div", "scope-dialog-title");
    titleBox.appendChild(make("h4", "", options.title));
    if (options.subtitle) {
      titleBox.appendChild(make("p", "muted", options.subtitle));
    }
    var count = make("span", "scope-dialog-count", options.count || "");
    var closeButton = make("button", "btn btn-small btn-ghost", tr("scope.close"));
    closeButton.type = "button";
    head.appendChild(titleBox);
    head.appendChild(count);
    head.appendChild(closeButton);

    var body = make("div", "scope-dialog-body");
    var foot = make("div", "scope-dialog-foot");
    dialog.appendChild(head);
    dialog.appendChild(body);
    dialog.appendChild(foot);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    pushDialog();

    var previousFocus = document.activeElement;
    var closed = false;

    function onKey(event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      } else if (event.key === "Tab") {
        trapFocus(event, dialog);
      }
    }

    function close() {
      if (closed) {
        return;
      }
      closed = true;
      document.removeEventListener("keydown", onKey);
      if (overlay.parentNode) {
        overlay.parentNode.removeChild(overlay);
      }
      popDialog();
      if (previousFocus && previousFocus.focus) {
        previousFocus.focus();
      }
      if (options.onClose) {
        options.onClose();
      }
    }

    document.addEventListener("keydown", onKey);
    closeButton.addEventListener("click", close);
    overlay.addEventListener("mousedown", function (event) {
      if (event.target === overlay) {
        close();
      }
    });

    var focusTarget = dialog.querySelector(
      "input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button"
    );
    if (focusTarget) {
      focusTarget.focus();
    }

    return {
      overlay: overlay,
      dialog: dialog,
      body: body,
      foot: foot,
      close: close,
      setCount: function (text) {
        count.textContent = text || "";
      },
    };
  }

  function addFootButton(handle, label, className, onClick) {
    var button = make("button", "btn " + className, label);
    button.type = "button";
    button.addEventListener("click", onClick);
    handle.foot.appendChild(button);
    return button;
  }

  // -------------------------------------------------------------- hydration

  function readMeta() {
    metaList = [];
    metaById = new Map();
    root.querySelectorAll(".scope-ds").forEach(function (element) {
      var id = Number(element.getAttribute("data-ds-id"));
      if (!Number.isInteger(id) || id <= 0) {
        return;
      }
      var meta = {
        id: id,
        name: element.getAttribute("data-ds-name") || tr("scope.datasource_number", { id: id }),
        defaultDb: element.getAttribute("data-default-database") || "",
        enabled: element.getAttribute("data-enabled") !== "0",
        missing: element.getAttribute("data-missing") === "1",
        el: element,
      };
      metaList.push(meta);
      metaById.set(id, meta);
    });
  }

  function appendMissingRow(id) {
    var row = make("div", "scope-ds scope-ds-missing");
    row.setAttribute("data-ds-id", String(id));
    row.setAttribute("data-ds-name", tr("scope.datasource_number", { id: id }));
    row.setAttribute("data-default-database", "");
    row.setAttribute("data-enabled", "0");
    row.setAttribute("data-missing", "1");

    var line = make("div", "scope-ds-row");
    var label = make("label", "scope-check scope-ds-label");
    var checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "scope-ds-checkbox";
    checkbox.setAttribute("data-ds", String(id));
    label.appendChild(checkbox);
    label.appendChild(
      make("span", "scope-ds-name", tr("scope.datasource_number", { id: id }))
    );
    line.appendChild(label);
    line.appendChild(make("span", "scope-ds-meta", tr("scope.missing_meta")));
    line.appendChild(make("span", "badge badge-error", tr("scope.needs_attention")));
    line.appendChild(make("span", "scope-ds-mode", ""));
    line.appendChild(make("div", "scope-ds-actions"));
    row.appendChild(line);
    row.appendChild(
      make("div", "scope-ds-note", tr("scope.missing_note"))
    );

    if (dsListEl) {
      dsListEl.appendChild(row);
    }
    var meta = {
      id: id,
      name: tr("scope.datasource_number", { id: id }),
      defaultDb: "",
      enabled: false,
      missing: true,
      el: row,
    };
    metaList.push(meta);
    metaById.set(id, meta);
    return meta;
  }

  function hydrate(raw) {
    var sources = new Map();
    var rawDatasources = Array.isArray(raw && raw.datasources)
      ? raw.datasources
      : [];
    var rawTables = Array.isArray(raw && raw.tables) ? raw.tables : [];
    var rawColumns = Array.isArray(raw && raw.columns) ? raw.columns : [];
    var rawUnrestricted =
      raw && Array.isArray(raw.unrestricted_tables)
        ? raw.unrestricted_tables
        : null;
    var rawUnrestrictedDatabases =
      raw && Array.isArray(raw.unrestricted_databases)
        ? raw.unrestricted_databases
        : [];

    var databaseGrants = new Map();
    rawUnrestrictedDatabases.forEach(function (item) {
      if (!Array.isArray(item) || item.length !== 2) {
        return;
      }
      var id = Number(item[0]);
      if (!Number.isInteger(id) || id <= 0) {
        return;
      }
      if (!databaseGrants.has(id)) {
        databaseGrants.set(id, []);
      }
      databaseGrants.get(id).push(String(item[1]));
    });

    var explicitTables = new Map();
    rawTables.forEach(function (item) {
      if (!Array.isArray(item) || item.length !== 3) {
        return;
      }
      var id = Number(item[0]);
      if (!Number.isInteger(id) || id <= 0) {
        return;
      }
      if (!explicitTables.has(id)) {
        explicitTables.set(id, []);
      }
      explicitTables.get(id).push(item);
    });

    var ownerIds = [];
    function addOwner(value) {
      var id = Number(value);
      if (Number.isInteger(id) && id > 0 && ownerIds.indexOf(id) === -1) {
        ownerIds.push(id);
      }
    }
    rawDatasources.forEach(addOwner);
    explicitTables.forEach(function (items, id) {
      addOwner(id);
    });
    databaseGrants.forEach(function (items, id) {
      addOwner(id);
    });
    rawColumns.forEach(function (item) {
      if (Array.isArray(item)) {
        addOwner(item[0]);
      }
    });

    ownerIds.forEach(function (id) {
      var source = newSource(true);
      var tables = explicitTables.get(id) || [];
      var grants = databaseGrants.get(id) || [];
      if (rawUnrestricted) {
        if (rawUnrestricted.indexOf(id) !== -1) {
          source.tableMode = "all";
        } else if (tables.length || grants.length) {
          source.tableMode = "list";
        } else {
          source.tableMode = "none";
        }
      } else {
        source.tableMode = tables.length ? "list" : "all";
      }
      if (source.tableMode !== "all") {
        source.unrestrictedDatabases = new Set(grants);
      }
      tables.forEach(function (item) {
        var db = String(item[1]);
        var table = String(item[2]);
        source.tables.set(tableKey(db, table), newTableEntry(db, table));
      });
      sources.set(id, source);
    });

    rawColumns.forEach(function (item) {
      if (!Array.isArray(item) || item.length !== 4) {
        return;
      }
      var id = Number(item[0]);
      if (!Number.isInteger(id) || id <= 0) {
        return;
      }
      var source = sources.get(id);
      if (!source) {
        source = newSource(true);
        source.tableMode = "list";
        sources.set(id, source);
      }
      if (source.tableMode === "all") {
        source.tableMode = "list";
      }
      var key = tableKey(item[1], item[2]);
      var entry = source.tables.get(key);
      if (!entry) {
        entry = newTableEntry(item[1], item[2]);
        source.tables.set(key, entry);
      }
      entry.columnsMode = "list";
      entry.columns.set(columnKey(item[3]), String(item[3]));
    });

    var restricted = Boolean(
      rawDatasources.length ||
        rawTables.length ||
        rawColumns.length ||
        (rawUnrestricted && rawUnrestricted.length) ||
        rawUnrestrictedDatabases.length
    );
    return { mode: restricted ? "specified" : "all", sources: sources };
  }

  // ---------------------------------------------------------------- collect

  function collectScope() {
    if (state.mode === "all") {
      return { datasources: [], tables: [], columns: [] };
    }
    var datasources = [];
    var unrestricted = [];
    var unrestrictedDatabases = [];
    var tables = [];
    var columns = [];

    state.sources.forEach(function (source, id) {
      if (!source.selected) {
        return;
      }
      datasources.push(id);
      if (source.tableMode === "all") {
        unrestricted.push(id);
        return;
      }
      if (source.tableMode !== "list") {
        return;
      }
      source.unrestrictedDatabases.forEach(function (db) {
        unrestrictedDatabases.push([id, db]);
      });
      source.tables.forEach(function (entry) {
        tables.push([id, entry.db, entry.table]);
        if (entry.columnsMode === "list" && entry.columns.size) {
          Array.from(entry.columns.values())
            .sort()
            .forEach(function (name) {
              columns.push([id, entry.db, entry.table, name]);
            });
        }
      });
    });

    sortNumbers(datasources);
    sortNumbers(unrestricted);
    unrestrictedDatabases.sort(compareArrays);
    tables.sort(compareArrays);
    columns.sort(compareArrays);
    return {
      datasources: datasources,
      tables: tables,
      columns: columns,
      unrestricted_tables: unrestricted,
      unrestricted_databases: unrestrictedDatabases,
    };
  }

  function parsePermissions(payload) {
    var unrestricted = Array.isArray(payload.unrestricted_tables)
      ? payload.unrestricted_tables
      : [];
    var unrestrictedDatabases = Array.isArray(payload.unrestricted_databases)
      ? payload.unrestricted_databases
      : [];
    var restricted = Boolean(
      payload.datasources.length ||
        payload.tables.length ||
        payload.columns.length ||
        unrestricted.length ||
        unrestrictedDatabases.length
    );
    var tables = new Map();
    payload.tables.forEach(function (item) {
      tables.set(item[0] + SEP + item[1] + SEP + item[2], null);
    });
    payload.columns.forEach(function (item) {
      var key = item[0] + SEP + item[1] + SEP + item[2];
      var set = tables.get(key);
      if (!(set instanceof Set)) {
        set = new Set();
        tables.set(key, set);
      }
      set.add(String(item[3]).toLowerCase());
    });
    var allDatabases = new Set();
    unrestrictedDatabases.forEach(function (item) {
      if (Array.isArray(item) && item.length === 2) {
        allDatabases.add(item[0] + SEP + item[1]);
      }
    });
    return {
      all: !restricted,
      datasources: new Set(payload.datasources),
      allTables: new Set(unrestricted),
      allDatabases: allDatabases,
      tables: tables,
    };
  }

  function isExpansion(previous, next) {
    if (previous.all) {
      return false;
    }
    if (next.all) {
      return true;
    }
    var expanded = false;
    next.datasources.forEach(function (id) {
      if (!previous.datasources.has(id)) {
        expanded = true;
      }
    });
    next.allTables.forEach(function (id) {
      if (!previous.allTables.has(id)) {
        expanded = true;
      }
    });
    next.allDatabases.forEach(function (_value, key) {
      if (!previous.allDatabases.has(key)) {
        expanded = true;
      }
    });
    next.tables.forEach(function (columns, key) {
      if (expanded) {
        return;
      }
      var parts = String(key).split(SEP);
      var id = Number(parts[0]);
      var databaseKey = id + SEP + parts[1];
      if (next.allTables.has(id) || next.allDatabases.has(databaseKey)) {
        return;
      }
      // The original scope granted the whole database: any table or column there was already accessible, so this is not a widening.
      if (previous.allDatabases.has(databaseKey)) {
        return;
      }
      if (!previous.tables.has(key)) {
        expanded = true;
        return;
      }
      var previousColumns = previous.tables.get(key);
      if (previousColumns === null || columns === null) {
        return;
      }
      columns.forEach(function (name) {
        if (!previousColumns.has(name)) {
          expanded = true;
        }
      });
    });
    return expanded;
  }

  function validatePayload(payload) {
    if (state.mode === "specified" && !payload.datasources.length) {
      return tr("scope.validate_no_ds");
    }
    if (payload.datasources.length > LIMITS.datasources) {
      return tr("scope.validate_ds_limit", { limit: LIMITS.datasources });
    }
    if (payload.tables.length > LIMITS.tables) {
      return tr("scope.validate_table_limit", { limit: LIMITS.tables });
    }
    if (payload.columns.length > LIMITS.columns) {
      return tr("scope.validate_column_limit", { limit: LIMITS.columns });
    }
    if (JSON.stringify(payload).length > LIMITS.payload) {
      return tr("scope.validate_payload");
    }
    return null;
  }

  // ----------------------------------------------------------------- render

  function syncModeRadios() {
    Array.prototype.forEach.call(modeInputs, function (input) {
      input.checked = input.value === state.mode;
    });
  }

  function renderSourceRow(meta) {
    var source = state.sources.get(meta.id);
    var selected = Boolean(source && source.selected);
    var row = meta.el;
    row.classList.toggle(
      "scope-ds-selected",
      selected && state.mode === "specified"
    );
    row.classList.toggle(
      "scope-ds-restricted",
      Boolean(source && selected && source.tableMode === "none")
    );

    var checkbox = row.querySelector(".scope-ds-checkbox");
    if (checkbox) {
      checkbox.checked = selected;
    }
    var modeEl = row.querySelector("[data-ds-mode]");
    var noteEl = row.querySelector("[data-ds-note]");
    var buttons = row.querySelectorAll(
      "[data-ds-all], [data-ds-pick], [data-ds-import]"
    );

    var usable = state.mode === "specified" && selected && !meta.missing;
    Array.prototype.forEach.call(buttons, function (button) {
      button.disabled = !usable;
    });

    if (meta.missing) {
      if (modeEl) {
        modeEl.textContent = tr("scope.missing_mode");
        modeEl.className = "scope-ds-mode scope-ds-mode-error";
      }
      return;
    }

    var modeText = "";
    var noteText = "";
    var noteClass = "scope-ds-note";
    var modeClass = "scope-ds-mode";

    if (!selected || state.mode !== "specified") {
      modeText = tr("scope.mode_unselected");
    } else if (source.tableMode === "all") {
      modeText = tr("scope.mode_all_tables");
      noteText = tr("scope.mode_all_tables_note");
    } else if (source.tableMode === "none") {
      modeText = tr("scope.mode_no_tables");
      modeClass += " scope-ds-mode-error";
      noteText = tr("scope.mode_no_tables_note");
      noteClass += " scope-note-warning";
    } else {
      var grantCount = source.unrestrictedDatabases.size;
      modeText = grantCount
        ? tr("scope.whole_and_list", { db: grantCount, tables: source.tables.size })
        : tr("scope.list_only", { tables: source.tables.size });
      var fieldTables = 0;
      source.tables.forEach(function (entry) {
        if (entry.columnsMode === "list" && entry.columns.size) {
          fieldTables += 1;
        }
      });
      noteText = grantCount
        ? tr("scope.whole_list_note", { tables: fieldTables || 0 })
        : fieldTables
          ? tr("scope.field_note", { tables: fieldTables })
          : tr("scope.fields_open_note");
    }

    if (modeEl) {
      modeEl.textContent = modeText;
      modeEl.className = modeClass;
    }
    if (noteEl) {
      noteEl.textContent = noteText;
      noteEl.className = noteClass;
    }
    var allButton = row.querySelector("[data-ds-all]");
    if (allButton) {
      allButton.disabled = !usable || source.tableMode === "all";
    }
  }

  function renderSummary(payload) {
    if (state.mode === "all") {
      summaryTextEl.textContent = tr("scope.summary_all");
      countsEl.textContent = tr("scope.summary_all_note");
      warningEl.hidden = true;
      return;
    }

    var chosen = payload.datasources.length;
    var allCount = 0;
    var databaseCount = 0;
    var listCount = 0;
    var noneCount = 0;
    var fieldTableCount = 0;
    payload.datasources.forEach(function (id) {
      var source = state.sources.get(id);
      if (!source) {
        return;
      }
      if (source.tableMode === "all") {
        allCount += 1;
      } else if (source.tableMode === "none") {
        noneCount += 1;
      } else {
        listCount += 1;
        databaseCount += source.unrestrictedDatabases.size;
        source.tables.forEach(function (entry) {
          if (entry.columnsMode === "list" && entry.columns.size) {
            fieldTableCount += 1;
          }
        });
      }
    });

    summaryTextEl.textContent = chosen
      ? tr("scope.summary_selected", { n: chosen })
      : tr("scope.summary_none");
    countsEl.textContent = tr("scope.counts", {
      all: allCount,
      dbs: databaseCount,
      tables: payload.tables.length,
      list_ds: listCount,
      none: noneCount,
      cols: payload.columns.length,
      field_tables: fieldTableCount,
    });

    var warnings = [];
    if (!chosen) {
      warnings.push(tr("scope.warn_no_ds"));
    }
    if (noneCount) {
      warnings.push(tr("scope.warn_none", { n: noneCount }));
    }
    var missingSelected = 0;
    payload.datasources.forEach(function (id) {
      var meta = metaById.get(id);
      if (meta && meta.missing) {
        missingSelected += 1;
      }
    });
    if (missingSelected) {
      warnings.push(tr("scope.warn_missing", { n: missingSelected }));
    }
    warningEl.textContent = warnings.join(" ");
    warningEl.hidden = !warnings.length;
  }

  function renderLimits(payload) {
    if (!limitsEl) {
      return;
    }
    var text = tr("scope.limits", {
      ds: LIMITS.datasources.toLocaleString(),
      tables: LIMITS.tables.toLocaleString(),
      columns: LIMITS.columns.toLocaleString(),
    });
    if (
      payload.tables.length > LIMITS.tables * 0.8 ||
      payload.columns.length > LIMITS.columns * 0.8
    ) {
      text += tr("scope.limits_current", {
        tables: payload.tables.length,
        columns: payload.columns.length,
      });
    }
    limitsEl.textContent = text;
  }

  function render() {
    syncModeRadios();
    var specified = state.mode === "specified";
    if (dsListEl) {
      dsListEl.hidden = !specified;
    }
    if (dsBulkEl) {
      dsBulkEl.hidden = !specified;
    }
    if (allNoteEl) {
      allNoteEl.hidden = specified;
    }
    metaList.forEach(renderSourceRow);
    var payload = collectScope();
    renderSummary(payload);
    renderLimits(payload);
    payloadInput.value = JSON.stringify(payload);
  }

  // -------------------------------------------------------------- bulk edits

  function setMode(next) {
    if (next === state.mode) {
      syncModeRadios();
      return;
    }
    if (next === "all") {
      var payload = collectScope();
      var hasRestriction = Boolean(
        payload.datasources.length ||
          payload.tables.length ||
          payload.columns.length ||
          payload.unrestricted_databases.length
      );
      if (
        hasRestriction &&
        !window.confirm(tr("scope.confirm_all"))
      ) {
        syncModeRadios();
        return;
      }
      state.mode = "all";
    } else {
      state.mode = "specified";
    }
    render();
  }

  function toggleSource(meta, checked) {
    var source = ensureSource(meta.id);
    if (checked) {
      source.selected = true;
      if (
        !source.tables.size &&
        !source.unrestrictedDatabases.size &&
        source.tableMode !== "none"
      ) {
        source.tableMode = "all";
      }
    } else {
      if (
        (source.tables.size || source.unrestrictedDatabases.size) &&
        !window.confirm(tr("scope.confirm_unselect", { name: meta.name }))
      ) {
        return false;
      }
      source.selected = false;
      source.tableMode = "all";
      source.tables = new Map();
      source.unrestrictedDatabases = new Set();
    }
    render();
    return true;
  }

  function setAllTables(meta) {
    var source = ensureSource(meta.id);
    source.selected = true;
    if (
      source.tableMode === "all" &&
      !source.unrestrictedDatabases.size &&
      !source.tables.size
    ) {
      render();
      return;
    }
    if (
      (source.tables.size || source.unrestrictedDatabases.size) &&
      !window.confirm(tr("scope.confirm_all_tables", { n: source.tables.size }))
    ) {
      return;
    }
    source.tableMode = "all";
    source.tables = new Map();
    source.unrestrictedDatabases = new Set();
    render();
  }

  function bulkSelectAllSources() {
    metaList.forEach(function (meta) {
      if (meta.missing) {
        return;
      }
      var source = ensureSource(meta.id);
      source.selected = true;
      if (source.tableMode === "none") {
        source.tableMode = "all";
      }
    });
    render();
  }

  function bulkInvertSources() {
    var dropsTables = false;
    metaList.forEach(function (meta) {
      if (meta.missing) {
        return;
      }
      var source = state.sources.get(meta.id);
      if (
        source &&
        source.selected &&
        (source.tables.size || source.unrestrictedDatabases.size)
      ) {
        dropsTables = true;
      }
    });
    if (
      dropsTables &&
      !window.confirm(tr("scope.confirm_invert"))
    ) {
      return;
    }
    metaList.forEach(function (meta) {
      if (meta.missing) {
        return;
      }
      var source = ensureSource(meta.id);
      if (source.selected) {
        source.selected = false;
        source.tableMode = "all";
        source.tables = new Map();
        source.unrestrictedDatabases = new Set();
      } else {
        source.selected = true;
        source.tableMode = "all";
      }
    });
    render();
  }

  function bulkClearSources() {
    var payload = collectScope();
    if (
      payload.datasources.length &&
      !window.confirm(tr("scope.confirm_clear"))
    ) {
      return;
    }
    state.sources.forEach(function (source) {
      source.selected = false;
      source.tableMode = "all";
      source.tables = new Map();
      source.unrestrictedDatabases = new Set();
    });
    render();
  }

  // ------------------------------------------------------------ table picker

  function openTablePicker(sourceId) {
    var meta = metaById.get(sourceId);
    if (!meta) {
      return;
    }
    var source = ensureSource(sourceId);
    if (!source.selected) {
      source.selected = true;
      render();
    }

    var draft = cloneTables(source.tables);
    var draftMode = source.tableMode === "all" ? "all" : "list";
    var selectedStale = true;
    var view = {
      list: [],
      total: 0,
      offset: 0,
      hasMore: false,
      q: "",
      database: "",
      match: "table",
      loading: false,
      error: "",
      tab: "results",
      selectedShown: TABLE_PAGE,
      controller: null,
      timer: null,
    };

    var handle = openDialog({
      title: tr("scope.table_picker_title", { name: meta.name }),
      subtitle: tr("scope.table_picker_subtitle", {
        db: meta.defaultDb || tr("scope.not_set"),
      }),
      count: "",
    });
    handle.dialog.classList.add("scope-dialog-wide");

    var modeBar = make("div", "scope-mode");
    var allLabel = make("label", "scope-mode-option");
    var allRadio = document.createElement("input");
    allRadio.type = "radio";
    allRadio.name = "scope_table_mode";
    allRadio.value = "all";
    allRadio.checked = draftMode === "all";
    allLabel.appendChild(allRadio);
    allLabel.appendChild(make("span", "", tr("scope.mode_all_tables")));
    allLabel.appendChild(make("small", "", tr("scope.mode_all_tables_short")));

    var listLabel = make("label", "scope-mode-option");
    var listRadio = document.createElement("input");
    listRadio.type = "radio";
    listRadio.name = "scope_table_mode";
    listRadio.value = "list";
    listRadio.checked = draftMode === "list";
    listLabel.appendChild(listRadio);
    listLabel.appendChild(make("span", "", tr("scope.mode_list_tables")));
    listLabel.appendChild(make("small", "", tr("scope.mode_list_tables_short")));

    modeBar.appendChild(allLabel);
    modeBar.appendChild(listLabel);
    handle.body.appendChild(modeBar);

    var toolbar = make("div", "scope-toolbar");
    var searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.className = "scope-input scope-search";
    searchInput.placeholder = tr("scope.search_tables_placeholder");
    toolbar.appendChild(searchInput);

    var databaseSelect = document.createElement("select");
    databaseSelect.className = "scope-input scope-select";
    toolbar.appendChild(databaseSelect);

    var matchSelect = document.createElement("select");
    matchSelect.className = "scope-input scope-select";
    matchSelect.appendChild(new Option(tr("scope.match_table"), "table"));
    matchSelect.appendChild(new Option(tr("scope.match_column"), "column"));
    toolbar.appendChild(matchSelect);

    var searchButton = make("button", "btn btn-small btn-secondary", tr("scope.search"));
    searchButton.type = "button";
    toolbar.appendChild(searchButton);
    handle.body.appendChild(toolbar);

    var bulk = make("div", "scope-bulk");
    var pickCurrent = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.pick_current")
    );
    var unpickCurrent = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.unpick_current")
    );
    var invertCurrent = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.invert_current")
    );
    var clearAll = make("button", "btn btn-small btn-secondary", tr("scope.clear_selected"));
    var grantAll = make("button", "btn btn-small btn-secondary", tr("scope.grant_all_tables"));
    [pickCurrent, unpickCurrent, invertCurrent, clearAll, grantAll].forEach(
      function (button) {
        button.type = "button";
        bulk.appendChild(button);
      }
    );
    handle.body.appendChild(bulk);

    var tabBar = make("div", "scope-tabs");
    var resultsTab = make("button", "scope-tab active", tr("scope.results_tab"));
    var selectedTab = make("button", "scope-tab", tr("scope.selected_tab", { n: 0 }));
    resultsTab.type = "button";
    selectedTab.type = "button";
    tabBar.appendChild(resultsTab);
    tabBar.appendChild(selectedTab);
    var totalNote = make("span", "scope-tabs-note muted", "");
    tabBar.appendChild(totalNote);
    handle.body.appendChild(tabBar);

    var resultsBox = make("div", "scope-table-list");
    var selectedBox = make("div", "scope-table-list scope-table-selected-list");
    selectedBox.hidden = true;
    handle.body.appendChild(resultsBox);
    handle.body.appendChild(selectedBox);

    var loadMore = make(
      "button",
      "btn btn-small btn-secondary scope-load-more",
      tr("scope.load_more")
    );
    loadMore.type = "button";
    loadMore.hidden = true;
    handle.body.appendChild(loadMore);
    var selectedMore = make(
      "button",
      "btn btn-small btn-secondary scope-load-more",
      tr("scope.show_more_selected")
    );
    selectedMore.type = "button";
    selectedMore.hidden = true;
    handle.body.appendChild(selectedMore);

    var status = note("", "muted");
    handle.body.appendChild(status);

    addFootButton(handle, tr("scope.cancel"), "btn-secondary", handle.close);
    addFootButton(handle, tr("scope.apply"), "btn-primary", applyDraft);

    function updateCounts() {
      handle.setCount(tr("scope.selected_tables", { n: draft.size }));
      selectedTab.textContent = tr("scope.selected_tab", { n: draft.size });
      var active = draftMode === "list";
      pickCurrent.disabled = !active || !view.list.length;
      unpickCurrent.disabled = !active || !view.list.length;
      invertCurrent.disabled = !active || !view.list.length;
      clearAll.disabled = !active || !draft.size;
      searchInput.disabled = !active;
      databaseSelect.disabled = !active;
      matchSelect.disabled = !active;
      searchButton.disabled = !active;
    }

    function tableRow(item) {
      var key = tableKey(item.database, item.table);
      var row = make("div", "scope-table-row");
      var label = make("label", "scope-check scope-table-label");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "scope-table-checkbox";
      checkbox.checked = draft.has(key);
      label.appendChild(checkbox);
      label.appendChild(make("code", "", item.database + "." + item.table));
      if (item.comment) {
        label.appendChild(make("span", "scope-table-comment", item.comment));
      }
      row.appendChild(label);

      if (Array.isArray(item.matched_columns) && item.matched_columns.length) {
        var chips = make("div", "scope-table-field");
        item.matched_columns.forEach(function (column) {
          chips.appendChild(make("code", "", column.column));
        });
        row.appendChild(chips);
      }

      var entry = draft.get(key);
      if (entry && entry.columnsMode === "list" && entry.columns.size) {
        row.appendChild(
          make("span", "badge badge-warning", tr("scope.badge_columns", { n: entry.columns.size }))
        );
      }

      var fieldButton = make("button", "btn btn-small btn-secondary", tr("scope.field_button"));
      fieldButton.type = "button";
      fieldButton.disabled = !checkbox.checked;
      fieldButton.addEventListener("click", function () {
        var current = draft.get(key);
        if (!current) {
          return;
        }
        openFields(sourceId, current, function () {
          renderResults();
          if (view.tab === "selected") {
            renderSelected();
          }
          updateCounts();
        });
      });
      row.appendChild(fieldButton);

      checkbox.addEventListener("change", function () {
        if (checkbox.checked) {
          if (!draft.has(key)) {
            draft.set(key, newTableEntry(item.database, item.table));
          }
        } else {
          draft.delete(key);
        }
        row.classList.toggle("scope-table-selected", checkbox.checked);
        fieldButton.disabled = !checkbox.checked;
        var badge = row.querySelector(".badge-warning");
        if (badge) {
          badge.parentNode.removeChild(badge);
        }
        updateCounts();
        selectedStale = true;
        if (view.tab === "selected") {
          renderSelected();
        }
      });

      if (checkbox.checked) {
        row.classList.add("scope-table-selected");
      }
      return row;
    }

    function renderResults() {
      resultsBox.textContent = "";
      if (view.error) {
        resultsBox.appendChild(note(view.error, "scope-note-warning"));
      }
      if (!view.list.length && !view.loading && !view.error) {
        resultsBox.appendChild(
          note(
            view.q || view.database
              ? tr("scope.no_matching_tables")
              : tr("scope.no_tables"),
            "muted"
          )
        );
      }
      view.list.forEach(function (item) {
        resultsBox.appendChild(tableRow(item));
      });
      loadMore.hidden = !view.hasMore;
      var loaded = view.list.length;
      totalNote.textContent = view.total
        ? loaded < view.total
          ? tr("scope.total_tables_loaded", { total: view.total, loaded: loaded })
          : tr("scope.total_tables", { total: view.total })
        : "";
    }

    function renderSelected() {
      selectedStale = false;
      selectedBox.textContent = "";
      var entries = Array.from(draft.values()).sort(function (left, right) {
        return compareArrays([left.db, left.table], [right.db, right.table]);
      });
      if (!entries.length) {
        selectedBox.appendChild(note(tr("scope.none_selected_tables"), "muted"));
      } else {
        entries.slice(0, view.selectedShown).forEach(function (entry) {
          selectedBox.appendChild(
            tableRow({ database: entry.db, table: entry.table, comment: "" })
          );
        });
      }
      selectedMore.hidden = entries.length <= view.selectedShown;
    }

    function switchTab(tab) {
      view.tab = tab;
      resultsTab.classList.toggle("active", tab === "results");
      selectedTab.classList.toggle("active", tab === "selected");
      resultsBox.hidden = tab !== "results";
      selectedBox.hidden = tab !== "selected";
      loadMore.hidden = tab !== "results" || !view.hasMore;
      if (tab === "selected" && selectedStale) {
        renderSelected();
      }
      selectedMore.hidden =
        tab !== "selected" || draft.size <= view.selectedShown;
    }

    function updateDatabases(databases) {
      if (!Array.isArray(databases) || !databases.length) {
        return;
      }
      var current = view.database;
      databaseSelect.textContent = "";
      databaseSelect.appendChild(new Option(tr("scope.all_databases"), ""));
      databases.forEach(function (item) {
        databaseSelect.appendChild(
          new Option(item.database + " (" + item.total + ")", item.database)
        );
      });
      databaseSelect.value = current;
    }

    function fetchPage(reset) {
      if (!reset && view.loading) {
        return;
      }
      if (reset) {
        if (view.controller) {
          view.controller.abort();
        }
        view.offset = 0;
        view.list = [];
        view.error = "";
      }
      view.loading = true;
      status.textContent = tr("scope.loading");
      var controller = window.AbortController ? new AbortController() : null;
      view.controller = controller;
      var params = new URLSearchParams();
      params.set("datasource_id", String(sourceId));
      params.set("offset", String(view.offset));
      params.set("limit", String(TABLE_PAGE));
      params.set("match", view.match);
      if (view.q) {
        params.set("q", view.q);
      }
      if (view.database) {
        params.set("database", view.database);
      }
      requestJson("/admin/tokens/scope/tables?" + params.toString(), {
        signal: controller ? controller.signal : undefined,
      })
        .then(function (data) {
          view.total = Number(data.total) || 0;
          view.hasMore = Boolean(data.has_more);
          view.list = view.list.concat(
            Array.isArray(data.tables) ? data.tables : []
          );
          view.offset = view.list.length;
          updateDatabases(data.databases);
          view.error = "";
          status.textContent = "";
          renderResults();
          updateCounts();
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") {
            return;
          }
          view.error = tr("scope.load_failed", { error: error.message });
          status.textContent = "";
          renderResults();
        })
        .then(function () {
          if (view.controller === controller) {
            view.controller = null;
            view.loading = false;
          }
        });
    }

    function scheduleSearch() {
      if (view.timer) {
        window.clearTimeout(view.timer);
      }
      view.timer = window.setTimeout(function () {
        view.timer = null;
        view.q = searchInput.value.trim();
        fetchPage(true);
      }, 300);
    }

    function setDraftMode(next) {
      if (next !== draftMode) {
        if (next === "all" && draft.size) {
          if (
            !window.confirm(tr("scope.confirm_draft_all", { n: draft.size }))
          ) {
            allRadio.checked = false;
            listRadio.checked = true;
            return;
          }
          draft.clear();
        }
        draftMode = next;
      }
      allRadio.checked = draftMode === "all";
      listRadio.checked = draftMode === "list";
      bulk.hidden = draftMode !== "list";
      toolbar.hidden = draftMode !== "list";
      tabBar.hidden = draftMode !== "list";
      resultsBox.hidden = draftMode !== "list" || view.tab !== "results";
      selectedBox.hidden = draftMode !== "list" || view.tab !== "selected";
      loadMore.hidden =
        draftMode !== "list" || view.tab !== "results" || !view.hasMore;
      selectedMore.hidden = draftMode !== "list" || view.tab !== "selected";
      status.textContent =
        draftMode === "all" ? tr("scope.draft_all_note") : "";
      if (draftMode === "list") {
        if (!view.list.length && !view.loading && !view.error) {
          fetchPage(true);
        }
        renderResults();
      }
      updateCounts();
    }

    function applyDraft() {
      if (draftMode === "all") {
        if (
          source.tableMode === "all" &&
          !source.unrestrictedDatabases.size &&
          !source.tables.size
        ) {
          handle.close();
          return;
        }
        if (
          (source.tables.size || source.unrestrictedDatabases.size) &&
          !window.confirm(tr("scope.draft_apply_all", { n: source.tables.size }))
        ) {
          return;
        }
        source.tableMode = "all";
        source.tables = new Map();
        source.unrestrictedDatabases = new Set();
        handle.close();
        render();
        return;
      }
      if (!draft.size) {
        if (source.tableMode === "none" && !source.unrestrictedDatabases.size) {
          handle.close();
          return;
        }
        if (
          !window.confirm(tr("scope.confirm_no_tables"))
        ) {
          return;
        }
        source.tableMode = "none";
        source.tables = new Map();
        source.unrestrictedDatabases = new Set();
        handle.close();
        render();
        return;
      }
      var narrowedDatabases = [];
      draft.forEach(function (entry) {
        if (
          source.unrestrictedDatabases.has(entry.db) &&
          narrowedDatabases.indexOf(entry.db) === -1
        ) {
          narrowedDatabases.push(entry.db);
        }
      });
      if (
        narrowedDatabases.length &&
        !window.confirm(
          tr("scope.confirm_narrow_dbs", {
            dbs: narrowedDatabases.join(tr("list_separator")),
          })
        )
      ) {
        return;
      }
      narrowedDatabases.forEach(function (db) {
        source.unrestrictedDatabases.delete(db);
      });
      source.tableMode = "list";
      source.tables = cloneTables(draft);
      handle.close();
      render();
    }

    allRadio.addEventListener("change", function () {
      if (allRadio.checked) {
        setDraftMode("all");
      }
    });
    listRadio.addEventListener("change", function () {
      if (listRadio.checked) {
        setDraftMode("list");
      }
    });
    searchInput.addEventListener("input", scheduleSearch);
    searchButton.addEventListener("click", function () {
      view.q = searchInput.value.trim();
      fetchPage(true);
    });
    databaseSelect.addEventListener("change", function () {
      view.database = databaseSelect.value;
      fetchPage(true);
    });
    matchSelect.addEventListener("change", function () {
      view.match = matchSelect.value;
      if (view.match === "column" && !searchInput.value.trim()) {
        view.match = "table";
        matchSelect.value = "table";
      }
      fetchPage(true);
    });
    resultsTab.addEventListener("click", function () {
      switchTab("results");
    });
    selectedTab.addEventListener("click", function () {
      switchTab("selected");
    });
    loadMore.addEventListener("click", function () {
      fetchPage(false);
    });
    selectedMore.addEventListener("click", function () {
      view.selectedShown += TABLE_PAGE;
      renderSelected();
    });
    pickCurrent.addEventListener("click", function () {
      view.list.forEach(function (item) {
        var key = tableKey(item.database, item.table);
        if (!draft.has(key)) {
          draft.set(key, newTableEntry(item.database, item.table));
        }
      });
      renderResults();
      selectedStale = true;
      updateCounts();
    });
    unpickCurrent.addEventListener("click", function () {
      view.list.forEach(function (item) {
        draft.delete(tableKey(item.database, item.table));
      });
      renderResults();
      selectedStale = true;
      updateCounts();
    });
    invertCurrent.addEventListener("click", function () {
      view.list.forEach(function (item) {
        var key = tableKey(item.database, item.table);
        if (draft.has(key)) {
          draft.delete(key);
        } else {
          draft.set(key, newTableEntry(item.database, item.table));
        }
      });
      renderResults();
      selectedStale = true;
      updateCounts();
    });
    clearAll.addEventListener("click", function () {
      if (!draft.size) {
        return;
      }
      if (!window.confirm(tr("scope.confirm_clear_draft", { n: draft.size }))) {
        return;
      }
      draft.clear();
      renderResults();
      selectedStale = true;
      updateCounts();
    });
    grantAll.addEventListener("click", function () {
      allRadio.checked = true;
      setDraftMode("all");
    });

    setDraftMode(draftMode);
    updateCounts();
  }

  // ------------------------------------------------------------ field picker

  function openFields(sourceId, entry, onChange) {
    var meta = metaById.get(sourceId);
    var mode = entry.columnsMode === "list" ? "list" : "all";
    var picked = new Map(entry.columns);
    var view = {
      list: [],
      total: 0,
      offset: 0,
      hasMore: false,
      q: "",
      loading: false,
      error: "",
      controller: null,
      timer: null,
    };

    var handle = openDialog({
      title: tr("scope.field_picker_title", { target: entry.db + "." + entry.table }),
      subtitle: meta ? tr("scope.field_picker_subtitle", { name: meta.name }) : "",
      variant: "modal",
      count: "",
    });

    var modeBar = make("div", "scope-mode");
    var allLabel = make("label", "scope-mode-option");
    var allRadio = document.createElement("input");
    allRadio.type = "radio";
    allRadio.name = "scope_column_mode";
    allRadio.value = "all";
    allRadio.checked = mode === "all";
    allLabel.appendChild(allRadio);
    allLabel.appendChild(make("span", "", tr("scope.all_columns")));
    allLabel.appendChild(make("small", "", tr("scope.all_columns_short")));

    var listLabel = make("label", "scope-mode-option");
    var listRadio = document.createElement("input");
    listRadio.type = "radio";
    listRadio.name = "scope_column_mode";
    listRadio.value = "list";
    listRadio.checked = mode === "list";
    listLabel.appendChild(listRadio);
    listLabel.appendChild(make("span", "", tr("scope.list_columns")));
    listLabel.appendChild(make("small", "", tr("scope.list_columns_short")));

    modeBar.appendChild(allLabel);
    modeBar.appendChild(listLabel);
    handle.body.appendChild(modeBar);

    var toolbar = make("div", "scope-toolbar");
    var searchInput = document.createElement("input");
    searchInput.type = "search";
    searchInput.className = "scope-input scope-search";
    searchInput.placeholder = tr("scope.search_columns_placeholder");
    toolbar.appendChild(searchInput);
    var searchButton = make("button", "btn btn-small btn-secondary", tr("scope.search"));
    searchButton.type = "button";
    toolbar.appendChild(searchButton);
    handle.body.appendChild(toolbar);

    var bulk = make("div", "scope-bulk");
    var pickCurrent = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.pick_current")
    );
    var unpickCurrent = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.unpick_current")
    );
    var invertCurrent = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.invert_current")
    );
    var clearAll = make("button", "btn btn-small btn-secondary", tr("scope.clear_selected"));
    [pickCurrent, unpickCurrent, invertCurrent, clearAll].forEach(function (
      button
    ) {
      button.type = "button";
      bulk.appendChild(button);
    });
    handle.body.appendChild(bulk);

    var totalNote = make("p", "scope-tabs-note muted", "");
    handle.body.appendChild(totalNote);

    var listBox = make("div", "scope-column-list");
    handle.body.appendChild(listBox);

    var loadMore = make(
      "button",
      "btn btn-small btn-secondary scope-load-more",
      tr("scope.load_more")
    );
    loadMore.type = "button";
    loadMore.hidden = true;
    handle.body.appendChild(loadMore);

    var status = note("", "muted");
    handle.body.appendChild(status);

    var footerError = note("", "scope-note-warning");
    footerError.hidden = true;
    handle.foot.appendChild(footerError);

    addFootButton(handle, tr("scope.cancel"), "btn-secondary", handle.close);
    addFootButton(handle, tr("scope.apply"), "btn-primary", apply);

    function updateCounts() {
      handle.setCount(tr("scope.selected_columns", { n: picked.size }));
      var active = mode === "list";
      searchInput.disabled = !active;
      searchButton.disabled = !active;
      pickCurrent.disabled = !active || !view.list.length;
      unpickCurrent.disabled = !active || !view.list.length;
      invertCurrent.disabled = !active || !view.list.length;
      clearAll.disabled = !active || !picked.size;
    }

    function renderList() {
      listBox.textContent = "";
      if (view.error) {
        listBox.appendChild(note(view.error, "scope-note-warning"));
      }
      if (!view.list.length && !view.loading && !view.error) {
        listBox.appendChild(
          note(
            view.q ? tr("scope.no_matching_columns") : tr("scope.no_columns"),
            "muted"
          )
        );
      }
      view.list.forEach(function (item) {
        var name = String(item.column);
        var key = columnKey(name);
        var row = make("label", "scope-column-row");
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.className = "scope-column-checkbox";
        checkbox.checked = picked.has(key);
        checkbox.disabled = mode !== "list";
        checkbox.addEventListener("change", function () {
          if (checkbox.checked) {
            picked.set(key, name);
          } else {
            picked.delete(key);
          }
          row.classList.toggle("scope-column-selected", checkbox.checked);
          updateCounts();
        });
        row.appendChild(checkbox);
        row.appendChild(make("code", "", name));
        if (item.data_type) {
          row.appendChild(
            make("span", "scope-column-type muted", item.data_type)
          );
        }
        if (item.comment) {
          row.appendChild(
            make("span", "scope-column-comment muted", item.comment)
          );
        }
        if (item.sensitive) {
          row.appendChild(
            make(
              "span",
              "badge badge-warning",
              tr("scope.sensitive") +
                (item.sensitivity ? " · " + item.sensitivity : "")
            )
          );
        }
        if (checkbox.checked) {
          row.classList.add("scope-column-selected");
        }
        listBox.appendChild(row);
      });
      loadMore.hidden = !view.hasMore;
      totalNote.textContent = view.total
        ? view.list.length < view.total
          ? tr("scope.total_columns_loaded", {
              total: view.total,
              loaded: view.list.length,
            })
          : tr("scope.total_columns", { total: view.total })
        : "";
    }

    function fetchPage(reset) {
      if (!reset && view.loading) {
        return;
      }
      if (reset) {
        if (view.controller) {
          view.controller.abort();
        }
        view.offset = 0;
        view.list = [];
        view.error = "";
      }
      view.loading = true;
      status.textContent = tr("scope.loading");
      var controller = window.AbortController ? new AbortController() : null;
      view.controller = controller;
      var params = new URLSearchParams();
      params.set("datasource_id", String(sourceId));
      params.set("database", entry.db);
      params.set("table", entry.table);
      params.set("offset", String(view.offset));
      params.set("limit", String(COLUMN_PAGE));
      if (view.q) {
        params.set("q", view.q);
      }
      requestJson("/admin/tokens/scope/columns?" + params.toString(), {
        signal: controller ? controller.signal : undefined,
      })
        .then(function (data) {
          view.total = Number(data.total) || 0;
          view.hasMore = Boolean(data.has_more);
          view.list = view.list.concat(
            Array.isArray(data.columns) ? data.columns : []
          );
          view.offset = view.list.length;
          view.error = "";
          status.textContent = "";
          renderList();
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") {
            return;
          }
          view.error = tr("scope.load_failed", { error: error.message });
          status.textContent = "";
          renderList();
        })
        .then(function () {
          if (view.controller === controller) {
            view.controller = null;
            view.loading = false;
          }
        });
    }

    function setMode(next) {
      if (next === mode) {
        return;
      }
      mode = next;
      allRadio.checked = mode === "all";
      listRadio.checked = mode === "list";
      toolbar.hidden = mode !== "list";
      bulk.hidden = mode !== "list";
      footerError.hidden = true;
      renderList();
      updateCounts();
      if (mode === "list" && !view.list.length) {
        fetchPage(true);
      }
    }

    function apply() {
      if (mode === "list" && !picked.size) {
        footerError.textContent = tr("scope.require_one_column");
        footerError.hidden = false;
        return;
      }
      entry.columnsMode = mode;
      entry.columns = mode === "list" ? new Map(picked) : new Map();
      handle.close();
      onChange();
    }

    allRadio.addEventListener("change", function () {
      if (allRadio.checked) {
        setMode("all");
      }
    });
    listRadio.addEventListener("change", function () {
      if (listRadio.checked) {
        setMode("list");
      }
    });
    searchInput.addEventListener("input", function () {
      if (view.timer) {
        window.clearTimeout(view.timer);
      }
      view.timer = window.setTimeout(function () {
        view.timer = null;
        view.q = searchInput.value.trim();
        fetchPage(true);
      }, 300);
    });
    searchButton.addEventListener("click", function () {
      view.q = searchInput.value.trim();
      fetchPage(true);
    });
    loadMore.addEventListener("click", function () {
      fetchPage(false);
    });
    pickCurrent.addEventListener("click", function () {
      view.list.forEach(function (item) {
        picked.set(columnKey(item.column), String(item.column));
      });
      renderList();
      updateCounts();
    });
    unpickCurrent.addEventListener("click", function () {
      view.list.forEach(function (item) {
        picked.delete(columnKey(item.column));
      });
      renderList();
      updateCounts();
    });
    invertCurrent.addEventListener("click", function () {
      view.list.forEach(function (item) {
        var key = columnKey(item.column);
        if (picked.has(key)) {
          picked.delete(key);
        } else {
          picked.set(key, String(item.column));
        }
      });
      renderList();
      updateCounts();
    });
    clearAll.addEventListener("click", function () {
      if (!picked.size) {
        return;
      }
      if (
        !window.confirm(tr("scope.confirm_clear_columns", { n: picked.size }))
      ) {
        return;
      }
      picked.clear();
      renderList();
      updateCounts();
    });

    toolbar.hidden = mode !== "list";
    bulk.hidden = mode !== "list";
    fetchPage(true);
    updateCounts();
  }

  // ---------------------------------------------------------------- import

  function openImport(sourceId) {
    var meta = metaById.get(sourceId);
    if (!meta) {
      return;
    }
    var source = ensureSource(sourceId);
    if (!source.selected) {
      source.selected = true;
      render();
    }
    var draft = cloneTables(source.tables);
    var handle = openDialog({
      title: tr("scope.import_title", { name: meta.name }),
      subtitle: tr("scope.import_subtitle", { limit: IMPORT_LIMIT }),
      variant: "modal",
    });

    var textarea = make("textarea", "scope-textarea");
    textarea.rows = 8;
    textarea.placeholder = tr("scope.import_placeholder", {
      lines:
        (meta.defaultDb ? meta.defaultDb + ".orders\n" : "shop.orders\n") +
        (meta.defaultDb
          ? meta.defaultDb + ".users.phone"
          : "shop.users.phone"),
    });
    handle.body.appendChild(textarea);

    var resultBox = make("div", "scope-import-result");
    handle.body.appendChild(resultBox);

    var status = note("", "muted");
    handle.body.appendChild(status);

    addFootButton(handle, tr("scope.cancel"), "btn-secondary", handle.close);
    var applyButton = addFootButton(
      handle,
      tr("scope.apply"),
      "btn-primary",
      commit
    );
    applyButton.disabled = true;

    function parseLines() {
      var lines = textarea.value.split(/\r?\n/);
      var items = [];
      var invalid = [];
      lines.forEach(function (rawLine, index) {
        var line = rawLine.trim();
        if (!line || line.charAt(0) === "#") {
          return;
        }
        var parts = line
          .split(/[.\t]/)
          .map(function (part) {
            return part.trim();
          })
          .filter(function (part) {
            return part.length > 0;
          });
        if (parts.length === 1) {
          parts = [meta.defaultDb, parts[0]];
        }
        if (parts.length < 2 || parts.length > 3 || !parts[0]) {
          invalid.push(index + 1);
          return;
        }
        items.push(parts);
      });
      return { items: items, invalid: invalid };
    }

    function validate() {
      resultBox.textContent = "";
      status.textContent = "";
      var parsed = parseLines();
      if (parsed.invalid.length) {
        resultBox.appendChild(
          note(
            tr("scope.import_invalid_lines", {
              lines: parsed.invalid.slice(0, 10).join(tr("list_separator")),
            }),
            "scope-note-warning"
          )
        );
        applyButton.disabled = true;
        return;
      }
      if (!parsed.items.length) {
        resultBox.appendChild(note(tr("scope.import_empty"), "muted"));
        applyButton.disabled = true;
        return;
      }
      if (parsed.items.length > IMPORT_LIMIT) {
        resultBox.appendChild(
          note(
            tr("scope.import_too_many", { limit: IMPORT_LIMIT }),
            "scope-note-warning"
          )
        );
        applyButton.disabled = true;
        return;
      }
      status.textContent = tr("scope.import_validating", {
        n: parsed.items.length,
      });
      applyButton.disabled = true;
      requestJson("/admin/tokens/scope/resolve", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ datasource_id: sourceId, items: parsed.items }),
      })
        .then(function (data) {
          var matched = Array.isArray(data.matched) ? data.matched : [];
          var missing = Array.isArray(data.missing) ? data.missing : [];
          matched.forEach(function (item) {
            var key = tableKey(item[0], item[1]);
            var entry = draft.get(key);
            if (!entry) {
              entry = newTableEntry(item[0], item[1]);
              draft.set(key, entry);
            }
            if (item.length === 3) {
              entry.columnsMode = "list";
              entry.columns.set(columnKey(item[2]), String(item[2]));
            }
          });
          status.textContent = "";
          resultBox.textContent = "";
          resultBox.appendChild(
            note(
              tr("scope.import_ok", { n: matched.length }),
              "scope-note-success"
            )
          );
          if (missing.length) {
            var box = make("div", "scope-import-missing");
            box.appendChild(
              make(
                "p",
                "scope-note-warning",
                tr("scope.import_missing", { n: missing.length })
              )
            );
            missing.slice(0, 50).forEach(function (item) {
              box.appendChild(
                make("code", "scope-import-missing-row", item.join("."))
              );
            });
            if (missing.length > 50) {
              box.appendChild(note(tr("scope.show_first_50"), "muted"));
            }
            resultBox.appendChild(box);
          }
          applyButton.disabled = matched.length === 0;
        })
        .catch(function (error) {
          status.textContent = "";
          resultBox.textContent = "";
          resultBox.appendChild(
            note(
              tr("scope.import_failed", { error: error.message }),
              "scope-note-warning"
            )
          );
          applyButton.disabled = true;
        });
    }

    var checkButton = make(
      "button",
      "btn btn-small btn-secondary",
      tr("scope.verify")
    );
    checkButton.type = "button";
    checkButton.addEventListener("click", validate);
    handle.foot.insertBefore(checkButton, applyButton);

    textarea.addEventListener("input", function () {
      resultBox.textContent = "";
      status.textContent = "";
      applyButton.disabled = true;
    });

    function commit() {
      if (!draft.size) {
        return;
      }
      if (
        (source.tableMode === "all" || source.unrestrictedDatabases.size) &&
        !window.confirm(tr("scope.confirm_import_narrow", { n: draft.size }))
      ) {
        return;
      }
      source.tableMode = "list";
      source.unrestrictedDatabases = new Set();
      source.tables = cloneTables(draft);
      handle.close();
      render();
    }
  }

  // ------------------------------------------------------------ copy/template

  function ensureMissingRows() {
    var missing = [];
    state.sources.forEach(function (source, id) {
      if (!metaById.has(id)) {
        missing.push(id);
      }
    });
    missing.forEach(appendMissingRow);
  }

  function applyScopeDict(raw, label) {
    var hydrated = hydrate(raw);
    state.mode = hydrated.mode;
    state.sources = hydrated.sources;
    ensureMissingRows();
    render();
    if (label) {
      window.alert(tr("scope.copied_applied", { label: label }));
    }
  }

  function openCopyDialog() {
    var handle = openDialog({
      title: tr("scope.copy_title"),
      subtitle: tr("scope.copy_subtitle"),
      variant: "modal",
    });
    var box = make("div", "scope-template-list");
    box.appendChild(note(tr("scope.copy_loading"), "muted"));
    handle.body.appendChild(box);
    addFootButton(handle, tr("scope.close"), "btn-secondary", handle.close);

    requestJson("/admin/tokens/scope/presets")
      .then(function (data) {
        box.textContent = "";
        var tokens = Array.isArray(data.tokens) ? data.tokens : [];
        if (!tokens.length) {
          box.appendChild(note(tr("scope.copy_empty"), "muted"));
          return;
        }
        tokens.forEach(function (token) {
          var row = make("div", "scope-template-row");
          var info = make("div", "scope-template-info");
          info.appendChild(
            make("strong", "", token.user_name || "Token #" + token.id)
          );
          info.appendChild(
            make(
              "span",
              "muted",
              " #" +
                token.id +
                " · " +
                (token.enabled ? tr("scope.enabled") : tr("scope.disabled")) +
                " · " +
                token.summary
            )
          );
          if (currentTokenId && String(token.id) === currentTokenId) {
            info.appendChild(make("span", "badge", tr("scope.current_token")));
          }
          row.appendChild(info);

          var useButton = make(
            "button",
            "btn btn-small btn-secondary",
            tr("scope.copy")
          );
          useButton.type = "button";
          useButton.disabled =
            Boolean(currentTokenId) && String(token.id) === currentTokenId;
          useButton.addEventListener("click", function () {
            if (
              !window.confirm(
                tr("scope.confirm_copy_token", {
                  name: token.user_name || "Token #" + token.id,
                })
              )
            ) {
              return;
            }
            requestJson("/admin/tokens/" + token.id + "/scope")
              .then(function (scopeData) {
                applyScopeDict(
                  scopeData.scope,
                  token.user_name || "Token #" + token.id
                );
                handle.close();
              })
              .catch(function (error) {
                window.alert(tr("scope.copy_failed", { error: error.message }));
              });
          });
          row.appendChild(useButton);
          box.appendChild(row);
        });
      })
      .catch(function (error) {
        box.textContent = "";
        box.appendChild(
          note(
            tr("scope.load_failed", { error: error.message }),
            "scope-note-warning"
          )
        );
      });
  }

  function readTemplates() {
    try {
      var raw = window.localStorage.getItem(TEMPLATE_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function writeTemplates(list) {
    try {
      window.localStorage.setItem(TEMPLATE_KEY, JSON.stringify(list));
      return true;
    } catch (error) {
      window.alert(tr("scope.storage_failed"));
      return false;
    }
  }

  function openTemplateDialog() {
    var handle = openDialog({
      title: tr("scope.template_title"),
      subtitle: tr("scope.template_subtitle"),
      variant: "modal",
    });

    var saveRow = make("div", "scope-inline-form");
    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "scope-input";
    nameInput.placeholder = tr("scope.template_name_placeholder");
    nameInput.maxLength = 40;
    saveRow.appendChild(nameInput);
    var saveButton = make(
      "button",
      "btn btn-small btn-primary",
      tr("scope.template_save")
    );
    saveButton.type = "button";
    saveRow.appendChild(saveButton);
    handle.body.appendChild(saveRow);

    var listBox = make("div", "scope-template-list");
    handle.body.appendChild(listBox);
    addFootButton(handle, tr("scope.close"), "btn-secondary", handle.close);

    function renderTemplates() {
      listBox.textContent = "";
      var templates = readTemplates();
      if (!templates.length) {
        listBox.appendChild(note(tr("scope.template_empty"), "muted"));
        return;
      }
      templates.forEach(function (template, index) {
        var row = make("div", "scope-template-row");
        var info = make("div", "scope-template-info");
        info.appendChild(
          make("strong", "", template.name || tr("scope.untitled"))
        );
        var payload = template.scope || {};
        info.appendChild(
          make(
            "span",
            "muted",
            " " +
              tr("scope.template_stats", {
                ds: (payload.datasources || []).length,
                tables: (payload.tables || []).length,
                columns: (payload.columns || []).length,
              })
          )
        );
        row.appendChild(info);

        var useButton = make(
          "button",
          "btn btn-small btn-secondary",
          tr("scope.apply")
        );
        useButton.type = "button";
        useButton.addEventListener("click", function () {
          if (
            !window.confirm(
              tr("scope.confirm_template_apply", {
                name: template.name || tr("scope.untitled"),
              })
            )
          ) {
            return;
          }
          applyScopeDict(
            template.scope,
            tr("scope.template_label", {
              name: template.name || tr("scope.untitled"),
            })
          );
          handle.close();
        });
        row.appendChild(useButton);

        var deleteButton = make(
          "button",
          "btn btn-small btn-danger",
          tr("scope.delete")
        );
        deleteButton.type = "button";
        deleteButton.addEventListener("click", function () {
          if (
            !window.confirm(
              tr("scope.confirm_template_delete", {
                name: template.name || tr("scope.untitled"),
              })
            )
          ) {
            return;
          }
          var next = readTemplates();
          next.splice(index, 1);
          if (writeTemplates(next)) {
            renderTemplates();
          }
        });
        row.appendChild(deleteButton);
        listBox.appendChild(row);
      });
    }

    saveButton.addEventListener("click", function () {
      var name = nameInput.value.trim();
      if (!name) {
        window.alert(tr("scope.template_name_required"));
        nameInput.focus();
        return;
      }
      var templates = readTemplates().filter(function (item) {
        return item.name !== name;
      });
      templates.unshift({
        name: name,
        savedAt: Date.now(),
        scope: collectScope(),
      });
      if (writeTemplates(templates)) {
        nameInput.value = "";
        renderTemplates();
      }
    });

    renderTemplates();
  }

  // ---------------------------------------------------------------- preview

  function tableLimitText(source) {
    if (source.tableMode === "all") {
      return tr("scope.preview_all_tables");
    }
    if (source.tableMode === "none") {
      return tr("scope.mode_no_tables");
    }
    var parts = [];
    if (source.unrestrictedDatabases.size) {
      parts.push(
        tr("scope.preview_whole", { n: source.unrestrictedDatabases.size })
      );
    }
    parts.push(tr("scope.preview_list", { n: source.tables.size }));
    return parts.join(" · ");
  }

  function openPreview() {
    var handle = openDialog({
      title: tr("scope.preview_title"),
      subtitle: tr("scope.preview_subtitle"),
      variant: "modal",
    });
    var box = make("div", "scope-preview");
    handle.body.appendChild(box);
    addFootButton(handle, tr("scope.close"), "btn-secondary", handle.close);

    if (state.mode === "all") {
      box.appendChild(note(tr("scope.preview_all"), "scope-note-warning"));
      return;
    }

    var payload = collectScope();
    if (!payload.datasources.length) {
      box.appendChild(note(tr("scope.preview_no_ds"), "muted"));
      return;
    }

    payload.datasources.forEach(function (id) {
      var meta = metaById.get(id);
      var source = state.sources.get(id);
      if (!source) {
        return;
      }
      var card = make("div", "scope-preview-card");
      card.appendChild(
        make("h5", "", meta ? meta.name : tr("scope.datasource_number", { id: id }))
      );
      card.appendChild(make("p", "", tableLimitText(source)));
      if (source.tableMode === "list") {
        var list = make("div", "scope-preview-tables");
        Array.from(source.unrestrictedDatabases)
          .sort()
          .forEach(function (db) {
            list.appendChild(
              make(
                "code",
                "scope-preview-line",
                tr("scope.whole_db_suffix", { db: db })
              )
            );
          });
        var entries = Array.from(source.tables.values()).sort(function (
          left,
          right
        ) {
          return compareArrays([left.db, left.table], [right.db, right.table]);
        });
        entries.slice(0, 100).forEach(function (entry) {
          var text = entry.db + "." + entry.table;
          if (entry.columnsMode === "list" && entry.columns.size) {
            text += tr("scope.column_suffix", { n: entry.columns.size });
          }
          list.appendChild(make("code", "scope-preview-line", text));
        });
        if (entries.length > 100) {
          list.appendChild(note(tr("scope.show_first_100"), "muted"));
        }
        card.appendChild(list);
      }
      box.appendChild(card);
    });
  }

  // ---------------------------------------------------------------- events

  root.addEventListener("change", function (event) {
    var target = event.target;

    if (target.classList.contains("scope-ds-checkbox")) {
      var row = target.closest(".scope-ds");
      var id = Number(row.getAttribute("data-ds-id"));
      var meta = metaById.get(id);
      if (!meta) {
        return;
      }
      if (!toggleSource(meta, target.checked)) {
        target.checked = true;
      }
      return;
    }

    var modeInput = target.closest('input[name="scope_ds_mode"]');
    if (modeInput && modeInput.checked) {
      setMode(modeInput.value);
    }
  });

  root.addEventListener("click", function (event) {
    var target = event.target;

    if (target.closest("[data-scope-to-specified]")) {
      setMode("specified");
      return;
    }
    if (target.closest("[data-ds-select-all]")) {
      bulkSelectAllSources();
      return;
    }
    if (target.closest("[data-ds-invert]")) {
      bulkInvertSources();
      return;
    }
    if (target.closest("[data-ds-clear]")) {
      bulkClearSources();
      return;
    }
    if (target.closest("[data-scope-copy]")) {
      openCopyDialog();
      return;
    }
    if (target.closest("[data-scope-template]")) {
      openTemplateDialog();
      return;
    }
    if (target.closest("[data-scope-preview]")) {
      openPreview();
      return;
    }
    if (target.closest("[data-scope-reset]")) {
      var payload = collectScope();
      var hasRestriction = Boolean(
        payload.datasources.length ||
          payload.tables.length ||
          payload.columns.length ||
          payload.unrestricted_databases.length
      );
      if (
        hasRestriction &&
        !window.confirm(tr("scope.confirm_reset"))
      ) {
        return;
      }
      state.mode = "all";
      state.sources = new Map();
      render();
      return;
    }

    var actionButton = target.closest(
      "[data-ds-all], [data-ds-pick], [data-ds-import]"
    );
    if (!actionButton) {
      return;
    }
    var row = actionButton.closest(".scope-ds");
    var id = Number(row && row.getAttribute("data-ds-id"));
    var meta = metaById.get(id);
    if (!meta || meta.missing) {
      return;
    }
    if (actionButton.hasAttribute("data-ds-all")) {
      setAllTables(meta);
    } else if (actionButton.hasAttribute("data-ds-pick")) {
      openTablePicker(id);
    } else {
      openImport(id);
    }
  });

  if (form) {
    form.addEventListener("submit", function (event) {
      var payload = collectScope();
      var problem = validatePayload(payload);
      if (problem) {
        event.preventDefault();
        window.alert(problem);
        return;
      }
      var previous = parsePermissions(initialPayload);
      var next = parsePermissions(payload);
      if (isExpansion(previous, next)) {
        if (!window.confirm(tr("scope.confirm_expand"))) {
          event.preventDefault();
          return;
        }
      }
      var noneCount = 0;
      payload.datasources.forEach(function (id) {
        var source = state.sources.get(id);
        if (source && source.tableMode === "none") {
          noneCount += 1;
        }
      });
      if (
        noneCount &&
        !window.confirm(tr("scope.confirm_save_none", { n: noneCount }))
      ) {
        event.preventDefault();
        return;
      }
      payloadInput.value = JSON.stringify(payload);
      submitting = true;
    });
  }

  window.addEventListener("beforeunload", function (event) {
    if (!form || submitting) {
      return;
    }
    if (JSON.stringify(collectScope()) !== initialKey) {
      event.preventDefault();
      event.returnValue = "";
      return "";
    }
    return undefined;
  });

  // -------------------------------------------------------------- bootstrap

  readMeta();

  var parsedInitial = null;
  try {
    var rawInitial = root.getAttribute("data-initial") || "";
    if (rawInitial) {
      parsedInitial = JSON.parse(rawInitial);
    }
  } catch (error) {
    parsedInitial = null;
  }

  var hydrated = hydrate(parsedInitial);
  state.mode = hydrated.mode;
  state.sources = hydrated.sources;
  if (state.mode === "all" && defaultMode === "specified") {
    state.mode = "specified";
  }
  ensureMissingRows();
  initialPayload = collectScope();
  initialKey = JSON.stringify(initialPayload);
  render();
})();
