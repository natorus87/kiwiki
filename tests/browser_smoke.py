"""Kleine echte Browser-Regressionssuite fuer die wichtigsten UI-Pfade."""

from __future__ import annotations

import json
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
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
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

        hamburger.click()
        notes_folder = page.locator('.tree-row[data-kind="dir"][data-path="notes"] .file-item')
        notes_folder.click()
        nested_note = page.locator('.tree-row[data-kind="file"][data-path="notes/nested.md"] .file-item')
        nested_note.wait_for()
        nested_note.click()
        page.locator(".file-view").wait_for()
        assert "Explorer-Test." in page.locator(".markdown-content").inner_text()
        assert "file=notes%2Fnested.md" in page.url

        hamburger.click()
        page.locator("#select-toggle").click()
        batch_checkboxes = page.locator('.tree-checkbox[data-kind="file"][data-path^="notes/browser-batch-"]')
        assert batch_checkboxes.count() == 35
        for index in range(batch_checkboxes.count()):
            batch_checkboxes.nth(index).check()
        delete_requests: list[str] = []

        def record_delete_request(request) -> None:
            if request.method == "DELETE" and "/api/file" in request.url:
                delete_requests.append(request.url)

        page.on("request", record_delete_request)
        page.get_by_role("button", name="Auswahl löschen").click()
        with page.expect_response(
            lambda response: response.request.method == "DELETE" and response.url.endswith("/api/files")
        ) as response_info:
            page.locator('.kw-modal [data-action="ok"]').click()
        response = response_info.value
        assert response.status == 200
        result = response.json()
        assert set(result["deleted"]) == {f"notes/browser-batch-{index}.md" for index in range(35)}
        assert result["failed"] == []
        assert result["index_cleanup_pending"] == []
        page.locator('.tree-checkbox[data-path="notes/browser-batch-0.md"]').wait_for(state="detached")
        assert delete_requests == [f"{BASE_URL}/api/files"]
        page.remove_listener("request", record_delete_request)
        page.keyboard.press("Escape")

        page.route(
            "**/ui/file?path=welcome.md",
            lambda route: route.fulfill(
                status=429,
                content_type="application/json",
                headers={"Retry-After": "42"},
                body='{"detail":"Zu viele Anfragen. Bitte später erneut versuchen.","retry_after":42}',
            ),
        )
        hamburger.click()
        page.locator('.tree-row[data-kind="file"][data-path="welcome.md"] .file-item').click()
        page.locator(".kw-toast.error").wait_for()
        assert "Zu viele Anfragen" in page.locator(".kw-toast.error").inner_text()
        assert "file=notes%2Fnested.md" in page.url
        assert "Explorer-Test." in page.locator(".markdown-content").inner_text()
        page.unroute("**/ui/file?path=welcome.md")

        page.route(
            "**/ui/files?path=projects",
            lambda route: route.fulfill(
                status=429,
                content_type="application/json",
                headers={"Retry-After": "42"},
                body='{"detail":"Zu viele Anfragen. Bitte später erneut versuchen.","retry_after":42}',
            ),
        )
        error_count = page.locator(".kw-toast.error").count()
        projects_row = page.locator('.tree-row[data-kind="dir"][data-path="projects"]')
        projects_row.locator(".file-item").click()
        page.wait_for_function(
            "(count) => document.querySelectorAll('.kw-toast.error').length > count",
            arg=error_count,
        )
        assert "open" not in (projects_row.get_attribute("class") or "").split()
        assert projects_row.get_attribute("aria-expanded") == "false"
        page.unroute("**/ui/files?path=projects")

        page.route(
            "**/ui/file?path=notes%2Fnested.md",
            lambda route: route.fulfill(
                status=429,
                content_type="application/json",
                headers={"Retry-After": "42"},
                body='{"detail":"Zu viele Anfragen. Bitte später erneut versuchen.","retry_after":42}',
            ),
        )
        page.evaluate(
            """() => {
                document.querySelector(
                    '.tree-row[data-kind="file"][data-path="welcome.md"] .file-item'
                ).click();
                document.querySelector(
                    '.tree-row[data-kind="file"][data-path="notes/nested.md"] .file-item'
                ).click();
            }"""
        )
        page.wait_for_url(f"{BASE_URL}/?file=welcome.md")
        page.locator(".file-view").wait_for()
        assert "Browser-Test." in page.locator(".markdown-content").inner_text()
        page.unroute("**/ui/file?path=notes%2Fnested.md")

        page.goto(f"{BASE_URL}/?file=welcome.md", wait_until="networkidle")
        page.locator(".file-view").wait_for()
        page.wait_for_function("document.title.toLowerCase().includes('welcome')")
        assert "file=welcome.md" in page.url
        assert "welcome" in page.title().lower()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        page.route(
            "**/ui/files?path=projects",
            lambda route: route.fulfill(
                status=429,
                content_type="application/json",
                headers={"Retry-After": "42"},
                body='{"detail":"Zu viele Anfragen. Bitte später erneut versuchen.","retry_after":42}',
            ),
        )
        page.evaluate("localStorage.setItem('kiwiki:openFolders', JSON.stringify(['projects']))")
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        projects_row = page.locator('.tree-row[data-kind="dir"][data-path="projects"]')
        projects_row.wait_for()
        assert "open" not in (projects_row.get_attribute("class") or "").split()
        assert page.evaluate("JSON.parse(localStorage.getItem('kiwiki:openFolders')).includes('projects')") is False
        page.unroute("**/ui/files?path=projects")

        page.goto(f"{BASE_URL}/settings", wait_until="networkidle")
        form_width = page.locator(".settings-form").evaluate("element => element.getBoundingClientRect().width")
        button_width = page.locator(".settings-form .btn-primary").evaluate(
            "element => element.getBoundingClientRect().width"
        )
        assert button_width <= form_width
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        graph_nodes = [
            {
                "id": f"document:{index}",
                "kind": "document",
                "label": f"Mobile graph node {index}",
                "path": f"notes/mobile-{index}.md",
            }
            for index in range(500)
        ]
        graph_edges = [
            {
                "id": f"edge:{index}",
                "source": "document:0",
                "target": f"document:{index + 1}",
                "kind": "links_to",
            }
            for index in range(424)
        ]
        page.route(
            "**/api/knowledge/graph?*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "status": "ready",
                        "nodes": graph_nodes,
                        "edges": graph_edges,
                        "truncated": False,
                    }
                ),
            ),
        )
        page.goto(f"{BASE_URL}/knowledge", wait_until="networkidle")
        page.wait_for_function("document.querySelector('#knowledge-node-count').textContent === '500'")
        animation_duration = page.evaluate(
            """() => new Promise(resolve => {
                const started = performance.now();
                let frames = 0;
                const tick = () => {
                    frames += 1;
                    frames >= 120 ? resolve(performance.now() - started) : requestAnimationFrame(tick);
                };
                requestAnimationFrame(tick);
            })"""
        )
        assert animation_duration < 4000
        bright_pixels = page.locator("#knowledge-graph").evaluate(
            """canvas => {
                const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
                let count = 0;
                for (let index = 0; index < data.length; index += 16) {
                    if (data[index] > 140 && data[index + 1] > 160 && data[index + 2] < 170) count += 1;
                }
                return count;
            }"""
        )
        assert bright_pixels > 100

        action_buttons = page.locator(".knowledge-icon-button")
        assert action_buttons.count() == 2
        button_boxes = action_buttons.evaluate_all(
            "buttons => buttons.map(button => { const rect = button.getBoundingClientRect(); return { top: rect.top, width: rect.width, height: rect.height }; })"
        )
        assert abs(button_boxes[0]["top"] - button_boxes[1]["top"]) < 0.25
        assert button_boxes[0]["width"] == button_boxes[0]["height"]
        assert button_boxes[1]["width"] == button_boxes[1]["height"]
        page.locator("#knowledge-reset").hover()
        page.wait_for_function(
            "() => getComputedStyle(document.querySelector('#knowledge-reset')).transform === 'none'",
            timeout=1000,
        )
        hovered_button_tops = action_buttons.evaluate_all(
            "buttons => buttons.map(button => button.getBoundingClientRect().top)"
        )
        assert hovered_button_tops[0] == hovered_button_tops[1]

        canvas = page.locator("#knowledge-graph")
        canvas.evaluate("element => { element.setPointerCapture = () => {}; }")
        canvas.dispatch_event("pointerdown", {"pointerId": 1, "pointerType": "touch", "clientX": 100, "clientY": 400})
        canvas.dispatch_event("pointerdown", {"pointerId": 2, "pointerType": "touch", "clientX": 200, "clientY": 400})
        canvas.dispatch_event("pointermove", {"pointerId": 2, "pointerType": "touch", "clientX": 260, "clientY": 400})
        assert int(page.locator("#knowledge-depth").inner_text().rstrip("%")) > 100
        canvas.dispatch_event("pointermove", {"pointerId": 2, "pointerType": "touch", "clientX": 150, "clientY": 400})
        assert int(page.locator("#knowledge-depth").inner_text().rstrip("%")) < 100
        canvas.dispatch_event("pointerup", {"pointerId": 2, "pointerType": "touch", "clientX": 150, "clientY": 400})
        canvas.dispatch_event("pointerup", {"pointerId": 1, "pointerType": "touch", "clientX": 100, "clientY": 400})
        page.locator("#knowledge-reset").click()
        tapped_button_boxes = action_buttons.evaluate_all(
            "buttons => buttons.map(button => { const rect = button.getBoundingClientRect(); return { top: rect.top, width: rect.width, height: rect.height }; })"
        )
        assert abs(tapped_button_boxes[0]["top"] - tapped_button_boxes[1]["top"]) < 0.25
        assert tapped_button_boxes == button_boxes
        assert page.locator("#knowledge-depth").inner_text() == "100%"
        page.evaluate(
            """() => {
                window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
                const canvas = document.querySelector('#knowledge-graph');
                const context = canvas.getContext('2d');
                context.fillStyle = '#171713';
                context.fillRect(0, 0, canvas.width, canvas.height);
                window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
            }"""
        )
        page.wait_for_function(
            """() => {
                const canvas = document.querySelector('#knowledge-graph');
                const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
                for (let index = 0; index < data.length; index += 16) {
                    if (data[index] > 140 && data[index + 1] > 160 && data[index + 2] < 170) return true;
                }
                return false;
            }"""
        )
        assert page_errors == []
        page.unroute("**/api/knowledge/graph?*")

        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("localStorage.setItem('kiwiki_sidebar_w', '380')")
        page.goto(f"{BASE_URL}/", wait_until="networkidle")
        sidebar = page.locator(".sidebar")
        hamburger = page.get_by_role("button", name="Menü")
        assert sidebar.get_attribute("aria-hidden") == "true"
        assert sidebar.evaluate("element => element.inert") is True
        assert sidebar.evaluate("element => element.getBoundingClientRect().width") == 0

        hamburger.click()
        assert sidebar.get_attribute("aria-hidden") == "false"
        assert sidebar.evaluate("element => element.inert") is False
        page.wait_for_function(
            "() => Math.round(document.querySelector('.sidebar').getBoundingClientRect().width) === 380"
        )
        assert sidebar.evaluate("element => Math.round(element.getBoundingClientRect().width)") == 380
        page.locator(".sidebar-account-button").click()
        account_menu = page.locator("#sidebar-account-menu")
        assert account_menu.get_attribute("aria-hidden") == "false"
        knowledge_link = account_menu.locator('a[href="/knowledge"]')
        assert knowledge_link.evaluate(
            """element => {
                const rect = element.getBoundingClientRect();
                const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return hit === element || element.contains(hit);
            }"""
        ) is True
        account_button = page.locator(".sidebar-account-button")
        page.set_viewport_size({"width": 1024, "height": 900})
        page.wait_for_function("() => document.querySelector('.sidebar').getAttribute('aria-hidden') === 'true'")
        assert account_menu.get_attribute("aria-hidden") == "true"
        assert hamburger.get_attribute("aria-expanded") == "false"
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_function(
            "() => Math.round(document.querySelector('.sidebar').getBoundingClientRect().width) === 380"
        )
        assert sidebar.get_attribute("aria-hidden") == "false"
        assert hamburger.get_attribute("aria-expanded") == "true"
        account_button.click()
        assert account_menu.get_attribute("aria-hidden") == "false"

        for _ in range(6):
            account_button.click()
            assert account_menu.get_attribute("aria-hidden") == "true"
            account_button.click()
            assert account_menu.get_attribute("aria-hidden") == "false"
        account_button.click()

        notes_folder = page.locator('.tree-row[data-kind="dir"][data-path="notes"] .file-item')
        notes_folder.click(button="right")
        context_menu = page.locator(".kw-context-menu")
        context_menu.wait_for()
        context_action = context_menu.locator(".kw-context-item:not(.disabled)").first
        assert context_action.evaluate(
            """element => {
                const rect = element.getBoundingClientRect();
                const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
                return hit === element || element.contains(hit);
            }"""
        ) is True
        context_action.click()
        context_menu.wait_for(state="detached")

        resizer = page.locator("#sidebar-resizer")
        resizer.dispatch_event(
            "pointerdown",
            {"pointerId": 9, "pointerType": "mouse", "button": 0, "clientX": 380, "clientY": 300},
        )
        assert "dragging" in (resizer.get_attribute("class") or "").split()
        assert page.locator("body").evaluate("element => element.style.userSelect") == "none"
        page.evaluate("window.dispatchEvent(new Event('blur'))")
        assert "dragging" not in (resizer.get_attribute("class") or "").split()
        assert page.locator("body").evaluate("element => element.style.userSelect") == ""

        hamburger.click()
        page.wait_for_function("() => document.querySelector('.sidebar').getBoundingClientRect().width === 0")
        assert sidebar.evaluate("element => element.getBoundingClientRect().width") == 0
        assert account_menu.get_attribute("aria-hidden") == "true"

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
        notes_dir = user_dir / "notes"
        notes_dir.mkdir()
        (notes_dir / "nested.md").write_text("# Verschachtelt\n\nExplorer-Test.\n", encoding="utf-8")
        for index in range(35):
            (notes_dir / f"browser-batch-{index}.md").write_text(
                f"# Browser Batch {index}\n",
                encoding="utf-8",
            )
        (user_dir / "projects").mkdir()

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
