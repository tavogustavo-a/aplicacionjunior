/** Acordeones con estado guardado en localStorage (clave effi.accordion.<panelId>). */
(function (global) {
  function bindAccordion(panelId, toggleId, defaultOpen) {
    var panel = document.getElementById(panelId);
    var toggle = document.getElementById(toggleId);
    var body = panel && panel.querySelector(":scope > .accordion-body");
    if (!panel || !toggle || !body) return;

    var key = "effi.accordion." + panelId;

    function apply(open) {
      panel.classList.toggle("is-open", !!open);
      body.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    }

    var saved = null;
    try {
      saved = localStorage.getItem(key);
    } catch (e) {}

    if (saved === "1") apply(true);
    else if (saved === "0") apply(false);
    else apply(!!defaultOpen);

    toggle.addEventListener("click", function () {
      var open = !panel.classList.contains("is-open");
      apply(open);
      try {
        localStorage.setItem(key, open ? "1" : "0");
      } catch (e) {}
    });
  }

  global.bindEffiAccordion = bindAccordion;
})(window);
