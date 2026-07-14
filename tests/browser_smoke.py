"""Kleine echte Browser-Regressionssuite fuer die wichtigsten UI-Pfade."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8765"
API_KEY = "browser-test-key-1234567890"


def _wait_until_ready(process: subprocess.Popen[str]) -> None:
    for _ in range(100):
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Testserver wurde vorzeitig beendet (Exit {process.returncode})\n{output}")
        try:
            with urllib.request.urlopen(f"{BASE_URL}/livez", timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Testserver wurde nicht rechtzeitig bereit")


def _run_browser_checks() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 375, "height": 812})
        page.route(
            "**/*",
            lambda route: route.continue_()
            if route.request.url.startswith(BASE_URL)
            else route.abort(),
        )

        page.goto(f"{BASE_URL}/login", wait_until="networkidle")
        viewport = page.locator('meta[name="viewport"]').get_attribute("content") or ""
        assert "user-scalable=no" not in viewport
        assert "maximum-scale=1" not in viewport

        page.get_by_label("API-Key").fill(API_KEY)
        page.get_by_role("button", name="Anmelden").click()
        page.wait_for_url(f"{BASE_URL}/")
        page.wait_for_load_state("networkidle")

        sidebar = page.locator(".sidebar")
        hamburger = page.get_by_role("button", name="Menü")
        assert sidebar.get_attribute("aria-hidden") == "true"
        assert sidebar.evaluate("element => element.inert") is True

        hamburger.focus()
        focus_style = hamburger.evaluate(
            "element => ({outline: getComputedStyle(element).outlineStyle, shadow: getComputedStyle(element).boxShadow})"
        )
        assert focus_style["outline"] != "none" or focus_style["shadow"] != "none"

        hamburger.click()
        assert sidebar.get_attribute("aria-hidden") == "false"
        assert sidebar.evaluate("element => element.inert") is False
        page.keyboard.press("Escape")
        assert sidebar.get_attribute("aria-hidden") == "true"
        assert sidebar.evaluate("element => element.inert") is True
        assert hamburger.evaluate("element => document.activeElement === element") is True

        page.goto(f"{BASE_URL}/?file=welcome.md", wait_until="networkidle")
        page.locator(".file-view").wait_for()
        page.wait_for_function("document.title.toLowerCase().includes('welcome')")
        assert "file=welcome.md" in page.url
        assert "welcome" in page.title().lower()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        page.goto(f"{BASE_URL}/settings", wait_until="networkidle")
        form_width = page.locator(".settings-form").evaluate("element => element.getBoundingClientRect().width")
        button_width = page.locator(".settings-form .btn-primary").evaluate(
            "element => element.getBoundingClientRect().width"
        )
        assert button_width <= form_width
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        browser.close()


def main() -> None:
    global BASE_URL
    with socket.socket() as free_socket:
        free_socket.bind(("127.0.0.1", 0))
        port = free_socket.getsockname()[1]
    BASE_URL = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="kiwiki-browser-") as data_dir:
        user_dir = Path(data_dir) / "admin"
        user_dir.mkdir(parents=True)
        (user_dir / "welcome.md").write_text("# Willkommen\n\nBrowser-Test.\n", encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "KIWIKI_DATA_DIR": data_dir,
                "KIWIKI_USERS": f"admin:{API_KEY}:admin",
                "KIWIKI_BASE_URL": BASE_URL,
                "KIWIKI_TRUST_PROXY": "false",
                "KIWIKI_OAUTH_TOKEN_SECRET": "browser-test-oauth-secret-1234567890",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_until_ready(process)
            _run_browser_checks()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
