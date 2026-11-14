(function () {
  "use strict";

  window.tr = function (key, params) {
    var catalog = window.ADMIN_I18N || {};
    var text = catalog[key] || key;
    if (!params) {
      return text;
    }
    return text.replace(/\{(\w+)\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match;
    });
  };

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
      }
    });
  });

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      try {
        var ok = document.execCommand("copy");
        document.body.removeChild(area);
        ok ? resolve() : reject(new Error("copy failed"));
      } catch (error) {
        document.body.removeChild(area);
        reject(error);
      }
    });
  }

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-copy-target"));
      if (!target) {
        return;
      }
      copyText(target.textContent.trim()).then(
        function () {
          var original = button.textContent;
          button.textContent = window.tr("copy_done");
          window.setTimeout(function () {
            button.textContent = original;
          }, 1500);
        },
        function () {
          window.prompt(window.tr("copy_failed"), target.textContent.trim());
        }
      );
    });
  });
})();
