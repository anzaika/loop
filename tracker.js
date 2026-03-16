// Analytics tracker — paste into your static site's HTML.
// TODO: Set COLLECTOR_URL to your Tailscale Funnel URL before deploying.
(function () {
  var url = "https://CHANGEME.tail1234.ts.net/collect";
  var loc = window.location;
  var ref = document.referrer;
  var params = new URLSearchParams(loc.search);
  var path = loc.pathname.length > 1 ? loc.pathname.replace(/\/$/, "") : "/";

  var data = { path: path };
  if (ref && new URL(ref).origin !== loc.origin) data.referrer = ref;
  if (params.get("utm_source")) data.utm_source = params.get("utm_source");
  if (params.get("utm_medium")) data.utm_medium = params.get("utm_medium");
  if (params.get("utm_campaign")) data.utm_campaign = params.get("utm_campaign");

  document.addEventListener("DOMContentLoaded", function () {
    navigator.sendBeacon(url, JSON.stringify(data));
  });
})();
