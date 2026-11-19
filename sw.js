
self.addEventListener("push", (event) => {
  let data = {};

  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = {
      title: "TikTok LIVE Checker",
      body: event.data ? event.data.text() : "A TikTok account is LIVE."
    };
  }

  const title = data.title || "TikTok LIVE Checker";

  const options = {
    body: data.body || "A TikTok account has started a LIVE.",
    icon: "./icon-192.png",
    badge: "./icon-192.png",
    data: {
      url: data.url || "./"
    },
    tag: data.tag || "tiktok-live",
    renotify: true
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const target = event.notification.data?.url || "./";

  event.waitUntil(
    clients.matchAll({
      type: "window",
      includeUncontrolled: true
    }).then((windows) => {
      for (const windowClient of windows) {
        if ("focus" in windowClient) {
          windowClient.navigate(target);
          return windowClient.focus();
        }
      }

      return clients.openWindow(target);
    })
  );
});
