"""Check the PWA on WebKit against a loopback synthetic station."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright
from waitress import create_server

from station import connect
from test_live_camera import start_fixture, test_app as make_test_app


def verify(page, context, base, viewport):
    page.on("pageerror", lambda error: print(f"Browser error: {error}"))
    page.goto(base)
    page.get_by_label("Username").fill("manager")
    page.get_by_label("Password").fill("synthetic-wall-only")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Station Dashboard")).to_be_visible()
    tiles = page.locator("#camera-grid [data-camera-tile]:visible")
    expect(tiles).to_have_count(4)
    tiles.first.scroll_into_view_if_needed()
    expect(page.locator("#camera-grid img:not([hidden])").first).to_be_visible(timeout=15000)
    for size in (viewport, {"width": viewport["height"], "height": viewport["width"]}):
        page.set_viewport_size(size)
        assert page.evaluate("() => document.documentElement.scrollWidth <= innerWidth"), size
        assert page.evaluate("""() => {
            const grid = document.querySelector('#camera-grid').getBoundingClientRect();
            return [...document.querySelectorAll('.camera-tile:not([hidden])')].every(tile => {
                const bounds = tile.getBoundingClientRect();
                return bounds.left >= grid.left - 1 && bounds.right <= grid.right + 1;
            });
        }"""), size
    page.set_viewport_size(viewport)
    page.get_by_role("button", name="Show all 12 cameras").click()
    expect(tiles).to_have_count(12)

    manifest = page.request.get(base + "static/manifest.webmanifest")
    assert manifest.ok and manifest.json()["display"] == "standalone"
    page.evaluate("""() => new Promise((resolve, reject) => {
        if (navigator.serviceWorker.controller) return resolve(true);
        const timeout = setTimeout(() => reject(Error('No active service worker')), 10000);
        navigator.serviceWorker.addEventListener('controllerchange', () => {
            clearTimeout(timeout);
            resolve(true);
        }, {once: true});
    })""")
    context.set_offline(True)
    expect(page.locator("[data-station-connection]")).to_be_visible()
    expect(page.get_by_role("heading", name="Station Dashboard")).not_to_be_visible()
    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="NAVIQRA is offline")).to_be_visible()
    assert "Synthetic Camera Wall" not in page.content()
    assert page.evaluate("caches.keys()") == []
    context.set_offline(False)
    page.get_by_role("link", name="Retry connection").click()
    dashboard = page.get_by_role("heading", name="Station Dashboard")
    signin = page.get_by_role("heading", name="Sign in")
    expect(dashboard.or_(signin)).to_be_visible()
    if signin.is_visible():
        page.get_by_label("Username").fill("manager")
        page.get_by_label("Password").fill("synthetic-wall-only")
        page.get_by_role("button", name="Sign in").click()
    expect(dashboard).to_be_visible()
    page.locator("#camera-grid [data-camera-tile]").first.scroll_into_view_if_needed()
    expect(page.locator("#camera-grid img:not([hidden])").first).to_be_visible(timeout=15000)


def main():
    with TemporaryDirectory() as temp:
        path = Path(temp) / "pwa.sqlite3"
        fixture = start_fixture(300)
        try:
            app = make_test_app(path, f"http://127.0.0.1:{fixture.server_port}")
            with closing(connect(path)) as db, db:
                db.execute("INSERT INTO camera_setup VALUES ('fixture_camera', 'RTSP', NULL)")
            server = create_server(app, host="127.0.0.1", port=0, threads=24)
            thread = Thread(target=server.run, daemon=True)
            thread.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.webkit.launch()
                    try:
                        for name in ("iPhone 15", "iPad Mini"):
                            profile = dict(playwright.devices[name])
                            profile.pop("default_browser_type", None)
                            context = browser.new_context(**profile, service_workers="allow")
                            try:
                                verify(context.new_page(), context, f"http://localhost:{server.effective_port}/",
                                       profile["viewport"])
                                print(f"{name}: WebKit emulation, portrait/landscape, four/12 tiles and offline privacy passed.")
                            finally:
                                context.close()
                    finally:
                        browser.close()
            finally:
                server.close()
                thread.join(timeout=3)
        finally:
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    main()
