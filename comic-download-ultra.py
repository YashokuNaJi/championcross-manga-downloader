# -*- coding: utf-8 -*-
import ast
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from PIL import Image, ImageChops, ImageStat

BASE = "https://championcross.jp"
DEBUG_PORT = 9222
PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".cc_dl_profile")

session = requests.Session()
HEADERS = {
    "accept": "text/html,application/json,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
}
TIMEOUT = (15, 60)

SPECIAL_KW = ("特典", "番外", "通知", "お知らせ", "告知", "記念", "特別", "読切", "読み切り",
              "出張", "コラボ", "企画", "エッセイ", "インタビュー", "プレビュー", "試し読み",
              "外伝", "序章", "プロローグ", "エピローグ", "日めくり")

# 可立即免费阅读的权限类型（無料 / 今なら無料）
FREE_ACCESS_TYPES = {"trial", "free", "memberonlyfree", "normal", "campaign"}


# ================= 真实桌面路径识别 =================
def get_desktop():
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            text=True, timeout=10).strip()
        if out and os.path.isdir(out):
            return out
    except Exception:
        pass
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            v = winreg.QueryValueEx(k, "Desktop")[0]
            v = os.path.expandvars(v)
            if os.path.isdir(v):
                return v
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


# ================= 浏览器 =================
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


def _port_alive(port):
    try:
        with socket.create_connection(("127.0.0.1", port), 0.5):
            return True
    except OSError:
        return False


def _wait_port(port, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if _port_alive(port):
            return True
        time.sleep(0.5)
    return False


def _profile_browser_running():
    """检测是否有浏览器用本工具配置打开（但可能没开调试端口）"""
    for exe_name in ("chrome.exe", "msedge.exe"):
        try:
            ps = ["powershell", "-NoProfile", "-Command",
                  f"(Get-CimInstance Win32_Process -Filter \"Name='{exe_name}'\").CommandLine"]
            out = subprocess.check_output(ps, text=True, timeout=15, errors="ignore")
            if PROFILE_DIR.lower() in (out or "").lower():
                return exe_name
        except Exception:
            pass
    return None


def _page_target():
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=5) as r:
        targets = json.loads(r.read().decode("utf-8"))
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
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
            raise RuntimeError("找不到浏览器页面，请确认浏览器窗口还开着。")
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

    def _call_once(self, method, params, timeout):
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
        raise TimeoutError(method)

    def call(self, method, params=None, timeout=25):
        last = None
        for i in range(5):
            try:
                return self._call_once(method, params, timeout)
            except Exception as exc:
                last = exc
                try:
                    self.connect()
                except Exception:
                    pass
                time.sleep(1 + i)
        raise last if last else TimeoutError(method)

    def eval(self, expr, await_promise=False):
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
        if "exceptionDetails" in r.get("result", {}):
            raise RuntimeError("JS 错误：" + json.dumps(r["result"]["exceptionDetails"], ensure_ascii=False)[:200])
        return r.get("result", {}).get("result", {}).get("value")

    def get_text(self, url):
        return self.eval("fetch(%s,{credentials:'include'}).then(r=>r.text())" % json.dumps(url),
                         await_promise=True)

    def navigate(self, url, timeout=60):
        try:
            self.call("Page.enable", {}, timeout=10)
        except Exception:
            self.connect()
            self.call("Page.enable", {}, timeout=10)
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


BR = Browser()


def ensure_browser(url, browser_name="auto"):
    if _port_alive(DEBUG_PORT):
        print("检测到已有调试浏览器在运行，将直接复用（不再新开窗口）。")
        BR.connect()
        return

    running = _profile_browser_running()
    if running:
        print(f"检测到 {running} 已用本工具配置打开，但没有开启调试端口，无法接管。")
        print("请关闭该浏览器窗口，然后用 start-edge.bat / start-chrome.bat 重新打开，再运行本工具。")
        raise SystemExit

    print("未检测到可复用的调试浏览器。")
    print("提示：你平时打开的一般浏览器无法被接管（浏览器安全限制）。")
    print("      想自己先打开，可运行同目录的 start-edge.bat / start-chrome.bat。")
    ans = input("现在由本工具打开专用浏览器窗口？(Y/n)：").strip().lower()
    if ans == "n":
        print("请先用 start-edge.bat / start-chrome.bat 打开浏览器，再重新运行本工具。")
        raise SystemExit

    if browser_name == "chrome":
        path = _browser_path("chrome") or _browser_path("edge")
    elif browser_name == "edge":
        path = _browser_path("edge") or _browser_path("chrome")
    else:
        path = _find_browser()
    if not path:
        raise RuntimeError("没有找到 Chrome 或 Edge。")
    print(f"正在打开浏览器：{os.path.basename(path)}")
    subprocess.Popen([path,
                      f"--remote-debugging-port={DEBUG_PORT}",
                      "--remote-allow-origins=*",
                      f"--user-data-dir={PROFILE_DIR}",
                      "--no-first-run", "--no-default-browser-check", url])
    if not _wait_port(DEBUG_PORT, 40):
        raise RuntimeError("浏览器启动超时。")
    time.sleep(2)
    BR.connect()


# ================= 基础信息 =================
def _dig_id(obj, keys):
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, dict):
            for k, v in o.items():
                if k.lower() in keys and str(v).isdigit() and str(v) != "0":
                    return str(v)
                stack.append(v)
        elif isinstance(o, list):
            stack.extend(o)
    return ""


def get_series_hash():
    for _ in range(20):
        try:
            v = BR.eval("(()=>{const e=document.querySelector('#comici-viewer');"
                        "return e?e.getAttribute('data-series-id'):'';})()")
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.5)
    return ""


def get_uid():
    for _ in range(10):
        try:
            v = BR.eval("(()=>{const e=document.querySelector('[data-member-id]');"
                        "return e?e.getAttribute('data-member-id'):'0';})()")
            if v and v != "0":
                return v
        except Exception:
            pass
        time.sleep(0.5)
    for api in ("/api/user/info", "/api/auth/session"):
        try:
            data = json.loads(BR.get_text(BASE + api))
            v = _dig_id(data, {"userid", "memberid", "user_id", "id"})
            if v:
                return v
        except Exception:
            pass
    return "0"


def get_series_name():
    try:
        t = BR.eval("document.title") or ""
        t = t.split("|")[0]
        t = re.split(r"[・·]", t)[0]
        t = re.sub(r'[\\/:*?"<>|]', "_", t).strip()
        if t:
            return t
    except Exception:
        pass
    return "comic-download"


# ================= 话数识别 =================
def main_number(title):
    for pat in (r"Karte\.?\s*(\d+)", r"第\s*(\d+)\s*[話话]", r"Part\.?\s*(\d+)", r"#\s*(\d+)"):
        m = re.search(pat, title, re.I)
        if m:
            return int(m.group(1))
    m = re.search(r"(\d+)", title or "")
    return int(m.group(1)) if m else None


def episode_info(title):
    special = any(k in (title or "") for k in SPECIAL_KW)
    num = None
    if not special:
        num = main_number(title)
        if num is None:
            special = True
    return num, special


def parse_ranges(s):
    ranges = []
    for part in re.split(r"[,，、\s]+", (s or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[-~～]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError(f"无法识别：{part}")
        if a > b:
            a, b = b, a
        ranges.append((a, b))
    return ranges


def select_ranges(episodes, ranges, include_special=True):
    picked = []
    for a, b in ranges:
        start = end = None
        for i, ep in enumerate(episodes):
            n = ep["num"]
            if n is None:
                continue
            if n >= a and start is None:
                start = i
            if n <= b:
                end = i
        if start is None:
            continue
        if end is None or end < start:
            end = start
        for i in range(start, end + 1):
            ep = episodes[i]
            if include_special or not ep["special"]:
                picked.append(ep)
    seen = set()
    out = []
    for ep in picked:
        if ep["id"] in seen:
            continue
        seen.add(ep["id"])
        out.append(ep)
    return out


# ================= viewer-id =================
_VID_CACHE = {}


def get_episode_viewer_id(ep_id):
    if ep_id in _VID_CACHE:
        return _VID_CACHE[ep_id]
    page_url = f"{BASE}/episodes/{ep_id}"
    try:
        html = BR.get_text(page_url) or ""
    except Exception:
        html = ""
    if html:
        m = re.search(r'data-comici-viewer-id="([^"]+)"', html)
        if m:
            _VID_CACHE[ep_id] = m.group(1)
            return m.group(1)
    try:
        BR.navigate(page_url)
    except Exception:
        pass
    for _ in range(20):
        try:
            href = BR.eval("location.href") or ""
            vid = BR.eval("(()=>{const e=document.querySelector('#comici-viewer');"
                          "return e?e.getAttribute('data-comici-viewer-id'):'';})()") or ""
            if not vid:
                dom = BR.eval("document.documentElement.outerHTML") or ""
                m = re.search(r'data-comici-viewer-id="([^"]+)"', dom)
                vid = m.group(1) if m else ""
        except Exception:
            href, vid = "", ""
        if vid and ep_id in href:
            _VID_CACHE[ep_id] = vid
            return vid
        time.sleep(0.5)
    _VID_CACHE[ep_id] = ""
    return ""


# ================= 权限判定 =================
def read_access_map(series_hash):
    try:
        data = json.loads(BR.get_text(
            f"{BASE}/api/series/access?seriesHash={series_hash}&episodeFrom=1&episodeTo=1000"))
    except Exception as exc:
        print("读取权限接口失败：", exc)
        return {}
    out = {}

    def visit(o):
        if isinstance(o, dict):
            eid = o.get("episodeId")
            if eid:
                out[eid] = o
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(data)
    return out


def access_is_free(acc):
    if not isinstance(acc, dict) or not acc:
        return None
    if not acc.get("hasAccess"):
        return False
    t = str(acc.get("accessType") or "").lower()
    if "wait" in t or "soon" in t:
        return False
    return t in FREE_ACCESS_TYPES


def collect_episodes(series_hash):
    url = f"{BASE}/api/episodes?seriesHash={series_hash}&episodeFrom=1&episodeTo=1000"
    data = json.loads(BR.get_text(url))
    episodes = []
    seen = set()

    def visit(o):
        if isinstance(o, dict):
            _id = o.get("id")
            if (isinstance(_id, str) and re.fullmatch(r"[0-9a-z]{8,}", _id)
                    and not _id.isdigit() and o.get("title") is not None and _id not in seen):
                seen.add(_id)
                title = str(o.get("title"))
                num, special = episode_info(title)
                date = (o.get("datePublished") or o.get("publishedAt")
                        or o.get("publishDate") or o.get("date") or 0)
                try:
                    date = int(date)
                except Exception:
                    date = 0
                episodes.append({
                    "id": _id, "title": title,
                    "index": o.get("indexId") or o.get("seriesEpisodeNumber") or 0,
                    "num": num, "special": special, "date": date, "free": None,
                })
            for v in o.values():
                visit(v)
        elif isinstance(o, list):
            for v in o:
                visit(v)

    visit(data)
    episodes.sort(key=lambda e: (e["date"] if e["date"] else 10 ** 15, e["index"]))

    print("正在读取各话权限…")
    acc_map = read_access_map(series_hash)
    print(f"  读到 {len(acc_map)} 条权限")
    dist = Counter()
    for i, e in enumerate(episodes, 1):
        acc = acc_map.get(e["id"])
        f = access_is_free(acc)
        if f is None:
            f = bool(get_episode_viewer_id(e["id"]))
        e["free"] = bool(f)
        dist[str((acc or {}).get("accessType") or "（无）")] += 1
        print(f"\r  判定 {i}/{len(episodes)}", end="", flush=True)
    print()
    print("  权限类型分布：", dict(dist))
    free_n = sum(1 for e in episodes if e["free"])
    print(f"作品共 {len(episodes)} 话（免费 {free_n} 话，未免费 {len(episodes) - free_n} 话；"
          f"「待っと無料 / すぐ無料 / 租借」按未免费计）")
    return episodes


# ================= 图片处理 =================
def image_headers(referer):
    h = dict(HEADERS)
    h.update({
        "referer": referer, "origin": BASE,
        "sec-fetch-dest": "image", "sec-fetch-mode": "no-cors", "sec-fetch-site": "same-site",
    })
    return h


def _grid_candidates(n, w, h):
    cands = []
    side = round(n ** 0.5)
    if side * side == n:
        cands.append((side, side))
    else:
        for c in range(1, n + 1):
            if n % c:
                continue
            r = n // c
            if w % c == 0 and h % r == 0:
                cands.append((c, r))
    if not cands:
        cands = [(side, side)]
    extra = [(r, c) for (c, r) in cands if (r, c) not in cands]
    return cands + extra


def _build(img, scramble, cols, rows, col_major):
    w, h = img.size
    bw, bh = w // cols, h // rows
    out = Image.new("RGB", (w, h))
    for index, s in enumerate(scramble):
        s = int(s)
        if col_major:
            sc_, sr_ = s // rows, s % rows
            dc_, dr_ = index // rows, index % rows
        else:
            sr_, sc_ = divmod(s, cols)
            dr_, dc_ = divmod(index, cols)
        x1, y1 = sc_ * bw, sr_ * bh
        x2 = w if sc_ == cols - 1 else x1 + bw
        y2 = h if sr_ == rows - 1 else y1 + bh
        out.paste(img.crop((x1, y1, x2, y2)), (dc_ * bw, dr_ * bh))
    return out


def _seam_score(img, cols, rows):
    w, h = img.size
    g = img.convert("L")
    bw, bh = w // cols, h // rows
    total, cnt = 0.0, 0
    for c in range(1, cols):
        x = c * bw
        d = ImageChops.difference(g.crop((x - 1, 0, x, h)), g.crop((x, 0, x + 1, h)))
        total += ImageStat.Stat(d).mean[0]
        cnt += 1
    for r in range(1, rows):
        y = r * bh
        d = ImageChops.difference(g.crop((0, y - 1, w, y)), g.crop((0, y, w, y + 1)))
        total += ImageStat.Stat(d).mean[0]
        cnt += 1
    return total / cnt if cnt else 0


def unscramble(input_bytes, scramble):
    img = Image.open(BytesIO(input_bytes)).convert("RGB")
    n = len(scramble)
    if n == 0:
        return img
    best = None
    for (cols, rows) in _grid_candidates(n, img.size[0], img.size[1]):
        for col_major in (True, False):
            out = _build(img, scramble, cols, rows, col_major)
            s = _seam_score(out, cols, rows)
            if best is None or s < best[0]:
                best = (s, out)
    return best[1]


def _dl_one(item, referer):
    try:
        r = session.get(item["url"], headers=image_headers(referer), timeout=TIMEOUT)
        r.raise_for_status()
        img = unscramble(r.content, item["scramble"])
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return (f"image_{int(item['sort']) + 1:04d}.jpg", buf.getvalue(), None)
    except Exception as exc:
        return (None, None, str(exc))


def fetch_contents(viewer_id, uid):
    items = {}
    i = 0
    while i < 400:
        api = (f"{BASE}/api/book/contentsInfo?user-id={uid}&comici-viewer-id={viewer_id}"
               f"&page-from={i}&page-to={i + 1}")
        data = json.loads(BR.get_text(api))
        res = data.get("result") or []
        if not res:
            if i == 0 and data.get("message") not in (None, "NoError"):
                raise RuntimeError(f"contentsInfo 提示：{data.get('message')}")
            break
        for it in res:
            if it.get("imageUrl") and it.get("scramble") is not None:
                sc = it["scramble"]
                if isinstance(sc, str):
                    try:
                        sc = json.loads(sc)
                    except json.JSONDecodeError:
                        sc = ast.literal_eval(sc)
                sort = int(it.get("sort", i))
                items[sort] = {"url": it["imageUrl"], "scramble": sc, "sort": sort}
        i += 1
    return [items[k] for k in sorted(items)]


def safe_name(text, fallback=""):
    return re.sub(r'[\\/:*?"<>|]', "_", (text or "")).strip() or fallback


def norm_name(text):
    """比较用：去掉所有空格（含全角）"""
    return re.sub(r"[\s\u3000]+", "", text or "")


def download_episode(ep_title, ep_id, uid, output_folder):
    referer = f"{BASE}/episodes/{ep_id}/"
    viewer_id = get_episode_viewer_id(ep_id)
    if not viewer_id:
        raise RuntimeError("该话取不到可读内容（可能未免费公开）")
    items = fetch_contents(viewer_id, uid)
    if not items:
        raise RuntimeError("该话没有可下载图片（可能未免费公开）")

    total = len(items)
    print(f"  共 {total} 页，开始下载…")
    results, pending = {}, list(items)
    for attempt in range(1, 4):
        if not pending:
            break
        fail, done = [], 0
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(_dl_one, it, referer): it for it in pending}
            for f in as_completed(futs):
                name, data, err = f.result()
                done += 1
                if err:
                    fail.append(futs[f])
                    print(f"\n   一页失败：{err}")
                else:
                    results[futs[f]["sort"]] = (name, data)
                filled = int(20 * done / len(pending))
                print(f"\r  [{attempt}] [{'#' * filled}{'-' * (20 - filled)}] {done}/{len(pending)} 页",
                      end="", flush=True)
        print()
        pending = fail
        if pending:
            print(f"  第 {attempt} 轮剩 {len(pending)} 页，重试…")

    if pending:
        raise RuntimeError(f"有 {len(pending)} 页仍未成功，本话不打包")
    if len(results) != total:
        raise RuntimeError(f"页数不完整（{len(results)}/{total}），本话不打包")

    zip_path = os.path.join(output_folder, f"{safe_name(ep_title, ep_id)}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for k in sorted(results):
            name, data = results[k]
            z.writestr(name, data)
    print(f"  完成（{total} 页）：{zip_path}")


# ================= 主流程 =================
def main():
    print("=" * 52)
    print(" championcross 漫画下载器 (ultra)")
    print("=" * 52)
    print("提示：1、建议先手动打开浏览器（Chrome / Edge），登录 championcross")
    print("      2、登录 championcross 会需要日区魔法，请自行解决")
    print("      3、然后在浏览器里打开你想下载的漫画的第一话的页面")
    print("      4、地址形如：https://championcross.jp/episodes/xxxxxxx")
    print("      5、然后把该网址复制粘贴到下面即可，原网页不要关闭以便于后续加载")
    print()
    url = input("粘贴漫画章节网址（输入 exit 退出）：").strip()
    if not url or url.lower() == "exit":
        return

    print("\n请选择要使用的浏览器：")
    print("  1. Chrome")
    print("  2. Edge")
    print("  3. 自动")
    b = input("请输入 1/2/3（直接回车=自动）：").strip()
    browser_name = {"1": "chrome", "2": "edge"}.get(b, "auto")

    desktop = get_desktop()
    print("\n请选择保存位置：")
    print(f"  1. 默认（桌面新建漫画同名文件夹）  →  {desktop}")
    print("  2. 自定义（粘贴一个文件夹路径）")
    loc = input("请输入 1/2（直接回车=默认）：").strip()
    if loc == "2":
        base_dir = input("粘贴保存目录（例如 D:\\漫画）：").strip().strip('"').strip("'")
        if not base_dir:
            base_dir = desktop
    else:
        base_dir = desktop
    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception as exc:
        print(f"目录不可用（{exc}），改用桌面。")
        base_dir = desktop
        os.makedirs(base_dir, exist_ok=True)
    print(f"保存到：{base_dir}")

    print("\n[1/3] 准备浏览器…")
    ensure_browser(url, browser_name)
    print("如果还没登录，请在浏览器里登录 championcross（Google / X 均可）。")
    input("登录好之后，回到本窗口按回车…")

    BR.navigate(url)
    series_hash = get_series_hash()
    uid = get_uid()
    series_name = get_series_name()
    print(f"作品ID：{series_hash}，用户ID：{uid}，作品名：{series_name}")
    if not series_hash:
        raise RuntimeError("没有取到作品ID。")

    output_folder = os.path.join(base_dir, series_name)
    os.makedirs(output_folder, exist_ok=True)
    print(f"本次输出文件夹：{output_folder}")

    print("\n[2/3] 读取章节目录…")
    episodes = collect_episodes(series_hash)
    if not episodes:
        raise RuntimeError("接口没有返回章节。")

    total_count = len(episodes)
    free_count = sum(1 for e in episodes if e["free"])

    print("\n前 3 条示例：")
    for i, ep in enumerate(episodes[:3], 1):
        tag = " [特/番外]" if ep["special"] else ""
        paid = "" if ep["free"] else "（未免费）"
        print(f"  {i}. {ep['title']}{tag}{paid}")

    print("\n选择下载模式：")
    print("  1. 全部话（免费的正常下，未免费的自动跳过）")
    print("  2. 自定义章节（段落内全部，含特典/番外/通知）")
    print("  3. 自定义章节（不含特典/番外/通知）")
    print("  4. 仅下载最新免费公开话数（默认为最新1话，也可以为最新 N 话）")
    print("  5. 检查已下载目录并补下缺失的话")
    mode = input("请输入 1/2/3/4/5：").strip()

    if mode == "1":
        todo = list(episodes)
    elif mode in ("2", "3"):
        print("  提示：可输入单话或区间，用逗号分隔，例如：1 或 11-45, 41-42, 325")
        rng = input("请输入话数（单话/区间）：").strip()
        try:
            ranges = parse_ranges(rng)
        except Exception as exc:
            print("格式错误：", exc)
            return
        todo = select_ranges(episodes, ranges, include_special=(mode == "2"))
    elif mode == "4":
        free_eps = [e for e in episodes if e["free"]]
        if not free_eps:
            print("没有可免费下载的话。")
            return
        cnt = input("要下载最新几话？（直接回车=1）：").strip()
        cnt = int(cnt) if cnt.isdigit() and int(cnt) > 0 else 1
        todo = free_eps[-cnt:]
        print(f"已选最新 {len(todo)} 话：")
        for e in todo:
            print("   ", e["title"])
    elif mode == "5":
        print("  识别依据：本地“zip 文件名 / 文件夹名”与网站话标题相同（或话号相同）即视为已下载。")
        print("  说明：若之前用本工具下载（按话标题命名），可直接对上；也支持按话号匹配，")
        print("        例如本地 “Karte.5 …” 与网站 “Karte.5 …” 视为同一话。")
        scan_dir = input("要检查的目录（直接回车=本次输出目录）：").strip().strip('"')
        if not scan_dir:
            scan_dir = output_folder
        if not os.path.isdir(scan_dir):
            print("目录不存在：", scan_dir)
            return

        existing = set()
        for entry in os.listdir(scan_dir):
            p = os.path.join(scan_dir, entry)
            if os.path.isfile(p) and entry.lower().endswith(".zip"):
                existing.add(norm_name(os.path.splitext(entry)[0]))
            elif os.path.isdir(p):
                existing.add(norm_name(entry))               # 按文件夹名
                try:                                          # 再看一层子文件夹里的 zip
                    for fn in os.listdir(p):
                        if fn.lower().endswith(".zip"):
                            existing.add(norm_name(os.path.splitext(fn)[0]))
                except Exception:
                    pass

        missing = []
        for e in episodes:
            nm = norm_name(safe_name(e["title"], e["id"]))
            hit = nm in existing
            if not hit and e["num"] is not None:              # 按话号兜底
                for ex in existing:
                    if main_number(ex) == e["num"]:
                        hit = True
                        break
            if not hit:
                missing.append(e)

        print(f"\n该目录识别到 {len(existing)} 个已下载项；与网站对比，缺失 {len(missing)} 话：")
        for i, e in enumerate(missing[:30], 1):
            lb = "" if e["free"] else "（未免费）"
            print(f"  {i}. {e['title']}{lb}")
        if len(missing) > 30:
            print(f"  ... 共 {len(missing)} 话")
        if not missing:
            print("没有缺失，全部已下载。")
            return
        ans = input("是否补下缺失的话？(Y/n)：").strip().lower()
        if ans == "n":
            return
        todo = missing
    else:
        print("已取消。")
        return

    if not todo:
        print("没有匹配到任何章节。")
        return

    print(f"\n已选中 {len(todo)} 话：")
    for i, ep in enumerate(todo[:5], 1):
        print(f"  {i}. {ep['title']}")
    if len(todo) > 5:
        print(f"  ... 共 {len(todo)} 话")

    print("\n[3/3] 开始下载：")
    print("!! 运行期间请不要关闭、不要最小化、不要让电脑休眠，保持浏览器窗口打开 !!")
    ok = fail_pre = fail_dl = 0
    ok_list, fail_list = [], []

    for i, ep in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {ep['title']}")
        if not ep["free"]:
            fail_pre += 1
            fail_list.append((ep["title"], "预统计", "未免费（含待っと無料 / すぐ無料 / 租借）"))
            print("  失败：该话未免费（含待っと無料 / すぐ無料 / 租借）")
            continue
        try:
            download_episode(ep["title"], ep["id"], uid, output_folder)
            ok += 1
            ok_list.append(ep["title"])
        except Exception as exc:
            fail_dl += 1
            fail_list.append((ep["title"], "下载", str(exc)))
            print(f"  失败：{exc}")
        time.sleep(2)
        if i % 10 == 0:
            time.sleep(5)

    failed = fail_pre + fail_dl
    print("\n========== 任务统计 ==========")
    print(f"作品共 {total_count} 话（免费 {free_count} 话，未免费 {total_count - free_count} 话）")
    print(f"已选中：{len(todo)} 话")
    print(f"成功下载：{ok} 话")
    print(f"失败：{failed} 话（预统计阶段 {fail_pre} + 下载阶段 {fail_dl}）")
    if fail_list:
        print("--- 失败清单 ---")
        for j, (t, st, m) in enumerate(fail_list, 1):
            print(f"  {j}. {t}  —— [{st}] {m}")
    print(f"输出目录：{output_folder}")

    try:
        report = os.path.join(output_folder, "下载报告.txt")
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"作品共 {total_count} 话（免费 {free_count} 话，未免费 {total_count - free_count} 话）\n")
            f.write(f"已选中 {len(todo)} 话，成功 {ok} 话，"
                    f"失败 {failed} 话（预统计阶段 {fail_pre} + 下载阶段 {fail_dl}）\n\n")
            f.write("【成功】\n" + "\n".join("  " + t for t in ok_list) + "\n\n")
            f.write("【失败】\n" + "\n".join(f"  {t} —— [{st}] {m}" for t, st, m in fail_list) + "\n")
        print(f"报告已写入：{report}")
    except Exception as exc:
        print("写报告失败：", exc)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("已取消。")
    except Exception as exc:
        print("运行失败：", exc)
        traceback.print_exc()
    finally:
        input("按回车键关闭窗口…")
