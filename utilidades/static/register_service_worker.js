(function () {
  // En la app de escritorio (pywebview) el SW no aporta y puede interferir.
  if (window.pywebview) return;
  if (!('serviceWorker' in navigator)) return;
  var meta = document.querySelector('meta[name="service-worker-url"]');
  var swUrl = meta && meta.getAttribute('content');
  if (!swUrl) return;
  window.addEventListener('load', function () {
    navigator.serviceWorker.register(swUrl).catch(function () {});
  });
})();
