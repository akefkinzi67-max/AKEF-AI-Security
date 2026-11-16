"use strict";

const toggle = document.querySelector("[data-camera-toggle]");
if (toggle) {
    const grid = document.querySelector("#camera-grid");
    const tiles = [...grid.querySelectorAll("[data-camera-tile]")];
    const feeds = tiles.map(tile => ({
        tile,
        image: tile.querySelector("[data-camera]"),
        status: tile.querySelector("[data-camera-status]"),
        visible: false,
        url: null
    })).filter(feed => feed.image);
    const byTile = new Map(feeds.map(feed => [feed.tile, feed]));
    const observer = new IntersectionObserver(entries => {
        for (const entry of entries) byTile.get(entry.target).visible = entry.isIntersecting;
    });
    for (const feed of feeds) observer.observe(feed.tile);

    function show(feed, jpeg) {
        const former = feed.url;
        feed.url = jpeg ? URL.createObjectURL(new Blob([jpeg], {type: "image/jpeg"})) : null;
        if (feed.url) feed.image.src = feed.url;
        else {
            feed.image.removeAttribute("src");
            feed.image.hidden = true;
            feed.status.textContent = "Not Connected";
        }
        if (former) URL.revokeObjectURL(former);
    }

    for (const feed of feeds) {
        feed.image.addEventListener("load", () => {
            feed.image.hidden = false;
            feed.status.textContent = "Live feed";
        });
        feed.image.addEventListener("error", () => show(feed, null));
    }

    async function refresh() {
        const active = feeds.filter(feed => feed.visible && !feed.tile.hidden && !document.hidden);
        for (const feed of feeds) {
            if (feed.url && !active.includes(feed)) show(feed, null);
        }
        try {
            if (active.length) {
                const names = new URLSearchParams();
                for (const feed of active) names.append("camera", feed.image.dataset.camera);
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 5000);
                let response, data;
                try {
                    response = await fetch(grid.dataset.frameUrl + "?" + names, {
                        signal: controller.signal, mode: "same-origin", credentials: "same-origin"
                    });
                    if (response.ok) data = await response.arrayBuffer();
                } finally {
                    clearTimeout(timer);
                }
                if (!response.ok) throw Error("Feed unavailable");
                const view = new DataView(data);
                let offset = 0;
                for (const feed of active) {
                    const size = view.getUint32(offset);
                    offset += 4;
                    if (size > data.byteLength - offset) throw Error("Invalid feed");
                    show(feed, size ? new Uint8Array(data, offset, size) : null);
                    offset += size;
                }
            }
        } catch {
            for (const feed of active) show(feed, null);
        } finally {
            setTimeout(refresh, 330);
        }
    }
    refresh();

    toggle.addEventListener("click", () => {
        const expand = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", String(expand));
        grid.classList.toggle("camera-grid-expanded", expand);
        toggle.textContent = expand ? "Show first 4 cameras" : "Show all 12 cameras";
        for (const tile of tiles.slice(4)) tile.hidden = !expand;
    });
}

const setup = document.querySelector("[data-station-setup]");
if (setup) {
    const message = setup.querySelector("[data-setup-error]");
    setup.addEventListener("submit", async event => {
        event.preventDefault();
        const submit = setup.querySelector("button[type=submit]");
        submit.disabled = true;
        message.hidden = true;
        try {
            const response = await fetch(setup.action, {
                method: "POST",
                body: new URLSearchParams(new FormData(setup))
            });
            if (response.redirected && response.ok) {
                window.location.assign(response.url);
                return;
            }
            const result = new DOMParser().parseFromString(await response.text(), "text/html");
            const error = result.querySelector("[data-operation-message]");
            message.textContent = error ? error.textContent.trim() : "Setup could not be completed. Check the form and retry.";
        } catch {
            message.textContent = "The station could not be reached. Retry without reentering your information.";
        } finally {
            submit.disabled = false;
        }
        message.hidden = false;
        message.focus();
    });
}
