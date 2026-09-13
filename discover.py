# -*- coding: utf-8 -*-
import json, os, socket, subprocess, threading, time, urllib.request

PORT = 9222
PROFILE = os.path.join(os.path.expanduser("~"), ".cc_dl_profile")
EP = input("粘贴章节网址（直接回车用默认）：").strip() or "https://championcross.jp/episodes/b93aa70547cb2"


def find_browser():
    for c in [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ]:
        if os.path.exists(c):
            return c
    return None


def port_alive(p):
    try:
        with socket.create_connection(("127.0.0.1", p), 0.5):
            return True
    except OSError:
        return False


def wait_port(p, t=40):
    end = time.time() + t
    while time.time() < end:
        if port_alive(p):
            return True
        time.sleep(0.5)
    return False


def page_target():
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
        ts = json.loads(r.read().decode("utf-8"))
    pages = [t for t in ts if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for t in pages:
        if "championcross" in (t.get("url") or ""):
            return t
    return pages[0] if pages else None


class CDP:
    def __init__(self, url):
        from websocket import create_connection
        self.ws = create_connection(url, timeout=30)
        self.i = 0
        self.resp = {}
        self.events = []
        self.lock = threading.Lock()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                break
            if "id" in m:
                with self.lock:
                    self.resp[m["id"]] = m
            else:
                with self.lock:
                    self.events.append(m)

    def call(self, method, params=None, timeout=25):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if mid in self.resp:
                    return self.resp.pop(mid)
            time.sleep(0.05)
        raise TimeoutError(method)

    def ev(self):
        with self.lock:
            return list(self.events)


def main():
    if not port_alive(PORT):
        b = find_browser()
        subprocess.Popen([b, f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
                          f"--user-data-dir={PROFILE}", "--no-first-run",
                          "--no-default-browser-check", EP])
        wait_port(PORT, 40)
        time.sleep(3)

    t = page_target()
    if not t:
        print("没有找到页面")
        return
    c = CDP(t["webSocketDebuggerUrl"])
    c.call("Page.enable")
    c.call("Network.enable")
    c.call("Runtime.enable")

    print("打开章节页（开始记录）…")
    c.call("Page.navigate", {"url": EP})
    time.sleep(12)          # 等动态目录加载

    # 滚到目录并点“もっと見る”
    c.call("Runtime.evaluate", {"expression":
        "(()=>{const e=document.querySelector('.series-eplist-wrap'); if(e)e.scrollIntoView();"
        "const b=document.querySelector('button[data-e2e=\"eplistShowMore\"]'); if(b)b.click(); return true;})()"})
    time.sleep(4)

    # 点每个分页标签
    for n in range(1, 8):
        c.call("Runtime.evaluate", {"expression":
            "(()=>{const a=[...document.querySelectorAll('a.series-sort-link')]"
            f".find(x=>(x.getAttribute('href')||'').endsWith('/{n}')); if(a){{a.click();return true;}}}})()"})
        time.sleep(2.5)
    time.sleep(3)

    print("\n===== 所有 XHR / Fetch 请求 =====")
    rid_url = {}
    for e in c.ev():
        if e.get("method") == "Network.requestWillBeSent":
            p = e["params"]
            r = p.get("request", {})
            typ = p.get("type")
            if typ in ("XHR", "Fetch") or r.get("method") == "POST":
                print(f"\n[{typ}] {r.get('method')} {r.get('url')}")
                hdrs = {k.lower(): v for k, v in (r.get("headers") or {}).items()}
                for key in ("rsc", "next-action", "next-router-state-tree", "content-type", "accept"):
                    if key in hdrs:
                        print(f"   {key}: {hdrs[key][:120]}")
                if r.get("postData"):
                    print("   postData:", r["postData"][:200])
                rid_url[p.get("requestId")] = r.get("url")

    print("\n===== 含章节数据的响应体 =====")
    for e in c.ev():
        if e.get("method") == "Network.responseReceived":
            p = e["params"]
            rid = p.get("requestId")
            url = p.get("response", {}).get("url", "")
            try:
                body = c.call("Network.getResponseBody", {"requestId": rid})["result"].get("body", "")
            except Exception:
                continue
            if ("indexId" in body or "eliTitle" in body or '"episodes"' in body):
                print(f"\n★ {url}\n   片段：{body[:500]}".replace("\n", " "))

    print("\n完成，请把上面全部内容复制给开发者。")


main()
