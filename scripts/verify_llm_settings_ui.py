"""Render the settings page in an isolated local browser session."""
from pathlib import Path
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    from playwright.sync_api import sync_playwright
    destination = ROOT/"cache"/"llm-settings-preview"
    destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fcf-llm-ui-") as temporary:
        directory=Path(temporary)
        env=dict(os.environ)
        env.update(FCF_PRIMARY_ROOT=str(directory), OUTPUT_DIR=str(directory/"output"),
                   CACHE_DIR=str(directory/"cache"),DATABASE_PATH=str(directory/"factory.sqlite3"),
                   OPENAI_API_KEY="preview-key", OPENAI_BASE_URL="https://api.example.com/v1",
                   OPENAI_MODEL="your-api-model", PYTHONUTF8="1",FCF_LLM_PROVIDER="api")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            port=sock.getsockname()[1]
        with (directory/"server.log").open("w",encoding="utf-8") as log:
            server=subprocess.Popen([sys.executable,"-m","streamlit","run",str(ROOT/"ui"/"app.py"),
                "--server.address","127.0.0.1","--server.port",str(port),"--server.headless","true",
                "--browser.gatherUsageStats","false"],env=env,cwd=ROOT,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            try:
                url=f"http://127.0.0.1:{port}"
                for _ in range(60):
                    try:
                        with urllib.request.urlopen(url+"/_stcore/health",timeout=1) as response:
                            if response.status==200: break
                    except OSError: time.sleep(.25)
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(executable_path=r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",headless=True)
                    page=browser.new_page(viewport={"width":1366,"height":1100},device_scale_factor=1)
                    page.goto(url)
                    page.get_by_text("设置",exact=True).click()
                    page.get_by_role("heading",name="模型与 API",exact=True).wait_for()
                    page.screenshot(path=str(destination/"desktop-api.png"),full_page=True)
                    page.get_by_text("Codex CLI（ChatGPT / Plus）",exact=True).click()
                    page.get_by_label("模型名称",exact=True).wait_for()
                    page.screenshot(path=str(destination/"desktop-codex.png"),full_page=True)
                    page.set_viewport_size({"width":390,"height":844})
                    page.wait_for_timeout(300)
                    sidebar=page.locator('[data-testid="stSidebar"]')
                    collapse=page.locator('[data-testid="stSidebarCollapseButton"] button')
                    if sidebar.get_attribute("aria-expanded") == "true":
                        collapse.dispatch_event("click")
                    page.wait_for_function("document.querySelector('[data-testid=stSidebar]').getAttribute('aria-expanded') === 'false'")
                    page.wait_for_timeout(500)
                    page.screenshot(path=str(destination/"mobile-codex.png"),full_page=True)
                    errors=page.locator('[data-testid="stException"]').count()
                    overflow=page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                    print({"exceptions":errors,"horizontal_overflow":overflow,"screenshots":str(destination)})
                    if errors or overflow: raise RuntimeError("Settings page has rendering errors")
                    browser.close()
            finally:
                if os.name == "nt" and server.poll() is None:
                    subprocess.run(["taskkill","/PID",str(server.pid),"/T","/F"],capture_output=True,
                                   timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill();server.wait()


if __name__=="__main__":
    main()
