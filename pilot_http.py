"""Smoke test a disposable, loopback-only NAVIQRA instance with synthetic data."""
import argparse
from http.cookiejar import CookieJar
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


def run(base, setup_token, mode):
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("Use a disposable loopback HTTP instance.")
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    base = base.rstrip("/")

    def request(path, fields=None):
        data = urlencode(fields).encode() if fields is not None else None
        with client.open(Request(base + path, data=data), timeout=8) as page:
            return page.read().decode()

    def form(path, **fields):
        html = request("/")
        token = re.search(r'name="csrf" value="([^"]+)"', html)
        if not token:
            raise RuntimeError("CSRF token missing.")
        return request(path, dict(fields, csrf=token[1]))

    if mode == "seed":
        page = request("/")
        if "Set up your station" not in page:
            raise RuntimeError("Use an uninitialized disposable station.")
        form("/setup", token=setup_token, name="Synthetic Pilot Station", timezone="America/Chicago",
             warehouse="Main", username="pilot-test", password="synthetic-pilot-only")
    dashboard = form("/login", username="pilot-test", password="synthetic-pilot-only")
    if "Synthetic Pilot Station" not in dashboard or "Station Dashboard" not in dashboard:
        raise RuntimeError("Station dashboard verification failed.")
    if mode == "seed":
        form("/product", sku="SMOKE-1", name="Synthetic Pilot Stock", barcode="", reorder="2")
        dashboard = form("/movement", sku="SMOKE-1", warehouse="Main", reason="Receive", quantity="7")
    if "Synthetic Pilot Stock" not in dashboard or "<td>Main</td><td>7</td>" not in dashboard:
        raise RuntimeError("Synthetic stock failed verification.")
    print(f"Pilot HTTP {mode}: setup, login and stock verified.")


if __name__ == "__main__":
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "verify"))
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    run(args.base, os.environ.get("NAVIQRA_SETUP_TOKEN"), args.mode)
