(function () {
  function retryNow() {
    var url = window.location.href.split('#')[0];
    url = url.replace(/([?&])_retry=\d+/g, '$1').replace(/[?&]$/, '');
    var sep = url.indexOf('?') >= 0 ? '&' : '?';
    window.location.replace(url + sep + '_retry=' + Date.now());
  }

  var retry = document.getElementById('errRetryBtn');
  if (!retry) return;

  retry.addEventListener('click', function (e) {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    retryNow();
  });
})();
