(function () {
  "use strict";

  var token = document.getElementById("credential-token");
  var datasource = document.getElementById("credential-datasource");

  function reload() {
    var params = new URLSearchParams();
    if (token && token.value) {
      params.set("token_id", token.value);
    }
    if (datasource && datasource.value) {
      params.set("datasource_id", datasource.value);
    }
    window.location.assign("/admin/credentials/new?" + params.toString());
  }

  if (token) {
    token.addEventListener("change", reload);
  }
  if (datasource) {
    datasource.addEventListener("change", reload);
  }

  var ttl = document.getElementById("credential-ttl");
  document.querySelectorAll("[data-ttl]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (ttl) {
        ttl.value = button.getAttribute("data-ttl");
      }
    });
  });
})();
