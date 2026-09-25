// Uno Notetaker service worker — only for the Share target (Android:
// share a voice memo / recording → Notetaker). No offline caching: the app
// lives on the person's computer and needs it anyway.
"use strict";
const SHARE_CACHE = "nt-share";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "POST" || url.pathname !== "/share-target") return;
  event.respondWith((async () => {
    try {
      const form = await event.request.formData();
      const file = form.get("audio");
      if (file && typeof file !== "string") {
        const cache = await caches.open(SHARE_CACHE);
        await cache.put("/shared/file", new Response(file, { headers: {
          "Content-Type": file.type || "application/octet-stream",
          "X-File-Name": encodeURIComponent(file.name || "recording"),
          "X-Title": encodeURIComponent(String(form.get("title") || "")),
        } }));
        return Response.redirect("/#/share", 303);
      }
    } catch (e) { /* fall through */ }
    return Response.redirect("/#/share-missed", 303);
  })());
});
