# -*- coding: utf-8 -*-
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request

BASE = "https://championcross.jp"
PORT = 9222
PROFILE = os.path.join(os.path.expanduser("~"), ".cc_dl_profile")


def _browser_path(name):
    if name == "chrome":
        cands = [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]
    else:
        cands = [
            os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def _find_browser():
    return _browser_path("chrome") or _browser_path("edge")


def _port_alive(p):
    try:
        with socket.create_connection(("127.0.0.1", p), 0.5):
            return True
    except OSError:
        return False


def _wait_port(p, t=40):
    end = time.time() + t
    while time.time() < end:
        if _port_alive(p):
            return True
        time.sleep(0.5)
    return False


def _page_target():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
        ts = json.loads(r.read().decode("utf-8"))
    pages = [t for t in ts if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    for t in pages:
        if "championcross" in (t.get("url") or ""):
            return t
    return pages[0]


class Browser:
    def __init__(self):
        self.ws = None
        self.i = 0
        self.resp = {}
        self.lock = threading.Lock()
        self.alive = False

    def connect(self):
        from websocket import create_connection
        t = _page_target()
        if not t:
            raise RuntimeError("找不到浏览器页面")
        self.ws = create_connection(t["webSocketDebuggerUrl"], timeout=30)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                self.alive = False
                break
            if "id" in m:
                with self.lock:
                    self.resp[m["id"]] = m

    def call(self, method, params=None, timeout=25):
        for _ in range(3):
            try:
                if not self.alive or self.ws is None:
                    self.connect()
                self.i += 1
                mid = self.i
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                end = time.time() + timeout
                while time.time() < end:
                    with self.lock:
                        if mid in self.resp:
                            return self.resp.pop(mid)
                    time.sleep(0.02)
            except Exception:
                try:
                    self.connect()
                except Exception:
                    pass
                time.sleep(0.5)
        raise TimeoutError(method)

    def eval(self, expr, await_promise=False):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
        return r.get("result", {}).get("result", {}).get("value")

    def get_text(self, url):
        return self.eval("fetch(%s,{credentials:'include'}).then(r=>r.text())" % json.dumps(url),
                         await_promise=True)

    def navigate(self, url, timeout=60):
        try:
            self.call("Page.enable", {}, timeout=10)
        except Exception:
            pass
        self.call("Page.navigate", {"url": url})
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.eval("document.readyState") == "complete":
                    time.sleep(0.5)
                    return
            except Exception:
                pass
            time.sleep(0.5)


def main():
    url = input("粘贴章节网址（第一话即可）：").strip()

    if not _port_alive(PORT):
        b = _find_browser()
        print("启动浏览器…")
        subprocess.Popen([b, f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                          f"--user-data-dir={PROFILE}", "--no-first-run",
                          "--no-default-browser-check", url])
        _wait_port(PORT, 40)
        time.sleep(2)

    br = Browser()
    br.connect()
    br.navigate(url)
    time.sleep(1)

    sh = ""
    for _ in range(20):
        sh = br.eval("(()=>{const e=document.querySelector('#comici-viewer');"
                     "return e?e.getAttribute('data-series-id'):'';})()") or ""
        if sh:
            break
        time.sleep(0.5)
    print("\n=== seriesHash ===")
    print(sh)

    print("\n=== /api/episodes 前 900 字 ===")
    try:
        print(br.get_text(f"{BASE}/api/episodes?seriesHash={sh}&episodeFrom=1&episodeTo=1000")[:900])
    except Exception as e:
        print("失败：", e)

    print("\n=== /api/series/access 前 900 字 ===")
    try:
        print(br.get_text(f"{BASE}/api/series/access?seriesHash={sh}&episodeFrom=1&episodeTo=1000")[:900])
    except Exception as e:
        print("失败：", e)

    print("\n=== 作品页统计 ===")
    br.navigate(f"{BASE}/series/{sh}")
    for _ in range(30):
        n = br.eval("document.querySelectorAll('[data-e2e=\"eliTitle\"]').length") or 0
        if n:
            break
        time.sleep(0.5)
    dom = br.eval("document.documentElement.outerHTML") or ""
    print("DOM 长度：", len(dom))
    print("series-eplist-item 次数：", dom.count("series-eplist-item"))
    print("eliFreeBadge 次数：", dom.count("eliFreeBadge"))
    k = dom.find("eliFreeBadge")
    if k > 0:
        print("标签片段：", dom[max(0, k - 280):k + 140].replace("\n", " "))
    else:
        k2 = dom.find("series-eplist-item")
        if k2 > 0:
            print("列表片段：", dom[max(0, k2 - 100):k2 + 500].replace("\n", " "))
        else:
            print("页面片段：", dom[:600].replace("\n", " "))
    print("\n（把以上全部内容复制发我）")


main()
