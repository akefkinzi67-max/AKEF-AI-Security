"use strict";

if (window.isSecureContext && "serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
}

const connection = document.querySelector("[data-station-connection]");
if (connection) {
    function setOffline(offline) {
        connection.hidden = !offline;
        document.documentElement.toggleAttribute("data-station-offline", offline);
    }
    async function checkConnection() {
        if (!navigator.onLine) {
            setOffline(true);
            return;
        }
        try {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 3500);
            try {
                const response = await fetch("/station-connection", {cache: "no-store", signal: controller.signal});
                setOffline(!navigator.onLine || response.status !== 204);
            } finally {
                clearTimeout(timer);
            }
        } catch {
            setOffline(true);
        }
    }
    window.addEventListener("online", checkConnection);
    window.addEventListener("offline", () => setOffline(true));
    window.addEventListener("pageshow", event => {
        if (event.persisted) window.location.reload();
    });
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) checkConnection();
    });
    setInterval(() => {
        if (!document.hidden) checkConnection();
    }, 15000);
    if (!navigator.onLine) setOffline(true);
}
