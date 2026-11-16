/* The service worker does not cache station responses or credentials. */
const offline = `<!doctype html>
<html lang="en-US"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NAVIQRA Offline</title></head>
<body><main><h1>NAVIQRA is offline</h1>
<p>Reconnect to the authorized station network and try again. Cameras, recordings, sales, inventory, and sign-in require an active station connection.</p>
<p><a href="/">Retry connection</a></p></main></body></html>`;

self.addEventListener("install", event => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("fetch", event => {
    const request = event.request;
    if (request.mode !== "navigate" || request.method !== "GET"
        || new URL(request.url).origin !== self.location.origin) return;
    event.respondWith(fetch(request, {cache: "no-store"}).catch(() => new Response(offline, {
        status: 503,
        headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        }
    })));
});
