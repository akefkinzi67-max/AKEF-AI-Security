"use strict";

const toggle = document.querySelector("[data-camera-toggle]");
if (toggle) {
    const tiles = [...document.querySelectorAll("[data-camera-tile]")];
    const observer = new IntersectionObserver(entries => {
        for (const entry of entries) {
            const tile = entry.target;
            tile.cameraVisible = entry.isIntersecting;
            update(tile);
        }
    });

    function stop(tile) {
        clearTimeout(tile.cameraRetry);
        tile.cameraRetry = null;
        if (tile.cameraImage.hasAttribute("src")) tile.cameraImage.removeAttribute("src");
    }

    function update(tile) {
        if (document.hidden || tile.hidden || !tile.cameraVisible) {
            stop(tile);
            return;
        }
        if (tile.cameraImage.hasAttribute("src") || tile.cameraRetry) return;
        connect(tile);
    }

    function connect(tile) {
        tile.cameraImage.src = tile.cameraImage.dataset.stream;
        tile.cameraStatus.textContent = "Connecting...";
        // A new request renews the bounded server stream and its authorization.
        tile.cameraRetry = setTimeout(() => restart(tile), 48000);
    }

    function restart(tile) {
        stop(tile);
        tile.cameraRetry = null;
        update(tile);
    }

    for (const tile of tiles) {
        const image = tile.querySelector("[data-stream]");
        if (!image) continue;
        tile.cameraImage = image;
        tile.cameraStatus = tile.querySelector("[data-camera-status]");
        image.addEventListener("load", () => {
            tile.cameraStatus.textContent = "Live feed";
            image.hidden = false;
        });
        image.addEventListener("error", () => {
            if (!image.hasAttribute("src")) return;
            stop(tile);
            image.hidden = true;
            tile.cameraStatus.textContent = "Not Connected";
            tile.cameraRetry = setTimeout(() => restart(tile), 5000);
        });
        observer.observe(tile);
    }

    toggle.addEventListener("click", () => {
        const expand = toggle.getAttribute("aria-expanded") !== "true";
        toggle.setAttribute("aria-expanded", String(expand));
        toggle.textContent = expand ? "Show first 4 cameras" : "Show all 12 cameras";
        for (const tile of tiles.slice(4)) {
            tile.hidden = !expand;
            if (tile.cameraImage) {
                if (!expand) restart(tile);
                else update(tile);
            }
        }
    });
    document.addEventListener("visibilitychange", () => {
        for (const tile of tiles.filter(tile => tile.cameraImage)) restart(tile);
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
            const error = result.querySelector("[role=status]");
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
