(function () {
  const input = document.getElementById("effi_session");
  const msg = document.getElementById("cookie-clean-msg");
  if (!input) return;

  function extractCiSession(raw) {
    const text = String(raw || "").trim();
    if (!text) return "";

    const match = text.match(/ci_session=([A-Za-z0-9_-]+)/i);
    if (match) return match[1];

    // Valor limpio (sin curl ni saltos)
    if (!/\n/.test(text) && !/curl\s/i.test(text) && /^[A-Za-z0-9_-]+$/.test(text)) {
      return text.replace(/^['"]|['"]$/g, "");
    }

    return "";
  }

  function cleanField(showMsg) {
    const original = input.value;
    const cleaned = extractCiSession(original);
    if (!cleaned) {
      if (showMsg && msg && (original.includes("curl") || original.includes("ci_session"))) {
        msg.hidden = false;
        msg.textContent = "No se encontró ci_session en el texto pegado.";
        msg.classList.add("error");
      }
      return;
    }

    if (cleaned !== original.trim()) {
      input.value = cleaned;
      if (showMsg && msg) {
        msg.hidden = false;
        msg.classList.remove("error");
        msg.textContent = "Listo: se extrajo solo la ci_session del cURL.";
      }
    }
  }

  input.addEventListener("paste", function () {
    setTimeout(function () {
      cleanField(true);
    }, 0);
  });

  input.addEventListener("blur", function () {
    cleanField(true);
  });

  const form = document.getElementById("form-guardar-cookie");
  if (form) {
    form.addEventListener("submit", function () {
      cleanField(false);
    });
  }

  // Si ya hay texto raro al cargar, limpia
  cleanField(false);
})();
