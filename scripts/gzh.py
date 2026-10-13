# -*- coding: utf-8 -*-
"""gzh.py — 公众号排版流水线（秀米）可复用自动化 CLI

设计目标：把「打开秀米 → 上传图片 → 套模板 → 替换文案 → 换图 → 调格式 → 保存 → 出预览二维码」
这一整套动作固化下来，下次同类任务直接调命令，不用重新摸索。

依赖：本机 Playwright（python）+ 可见 Chrome（CDP 9222）+ 秀米登录态（持久化 profile）。

常用：
  python gzh.py status                      # 浏览器/登录/当前编辑页状态
  python gzh.py start                       # 启动可见 Chrome（带 CDP，持久化登录）
  python gzh.py lib -o library.json         # 列出图库图片（名字 -> CDN 地址）
  python gzh.py upload img\\01.jpg img\\02.jpg
  python gzh.py title "标题文字"
  python gzh.py inject layout.html          # 整篇注入 HTML（自建排版路线）
  python gzh.py cells                       # 列出模板所有文本单元格
  python gzh.py set-cells map.json          # 按单元格序号替换文案
  python gzh.py set-images urls.json        # 替换模板图片位
  python gzh.py add-images urls.json --after-cell 5
  python gzh.py format --cells 5,12,15 --size 14 --line-height 1.5
  python gzh.py save
  python gzh.py preview -o out/             # 保存+预览：二维码 png + 预览长图 + 预览链接
  python gzh.py open --match 军训夏令营      # 从「打开」弹窗里选中并打开某个图文
  python gzh.py shop --keyword 军训 --free  # 风格排版商城搜索（返回卡片信息）
  python gzh.py shop-save --tag 开学新生军训  # 另存模板给自己
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CDP = os.environ.get("GZH_CDP", "http://127.0.0.1:9222")
PROFILE = os.environ.get("GZH_PROFILE", str(Path.home() / ".gzh-content" / "xiumi_profile"))
CHROME = os.environ.get(
    "GZH_CHROME",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright" / "chromium-1234"
        / "chrome-win64" / "chrome.exe"),
)
TITLE_INPUT = "input[placeholder='请输入图文的标题']"
CONTENT = "div[contenteditable='true']"
EDITOR = "https://xiumi.us/studio/v5#/paper"


# ---------------------------------------------------------------- 基础设施
def log(*a):
    print(*a, flush=True)


def browser_alive() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(CDP + "/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_browser(url: str = EDITOR):
    """启动用户看得见的 Chrome（带 CDP 调试口），登录态存在持久化 profile 里。"""
    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    if not Path(CHROME).exists():
        raise SystemExit(f"找不到 Chrome：{CHROME}（用 GZH_CHROME 指定）")
    subprocess.Popen([
        CHROME, f"--remote-debugging-port={CDP.rsplit(':', 1)[-1]}",
        f"--user-data-dir={PROFILE}", "--start-maximized",
        "--no-first-run", "--no-default-browser-check", url,
    ])
    for _ in range(30):
        time.sleep(1)
        if browser_alive():
            log("浏览器已启动（CDP 就绪）")
            return
    raise SystemExit("浏览器启动超时")


class Session:
    """连到那个「用户看得见」的 Chrome，操作秀米页面。"""

    def __init__(self, ensure: bool = True, paper: str | None = None):
        self._pw = sync_playwright().start()
        if not browser_alive():
            if not ensure:
                raise SystemExit("浏览器没在跑（先执行 python gzh.py start）")
            start_browser()
        self.browser = self._pw.chromium.connect_over_cdp(CDP)
        self.ctx = self.browser.contexts[0]
        self.paper = paper          # 目标图文：URL 里含这个片段（如 726736288）

    # --- 页面
    def editor(self, match: str | None = None):
        match = match or self.paper
        pages = [p for p in self.ctx.pages if "studio/v5" in p.url and not p.is_closed()]
        if not pages:
            p = self.ctx.new_page()
            p.goto(EDITOR, wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            pages = [p]
        if match:
            for p in pages:
                if match in p.url:
                    return p
            raise SystemExit(f"没找到含 {match!r} 的编辑器标签页；先 python gzh.py status 看有哪些")
        return pages[-1]

    def page_by_url(self, part: str):
        for p in self.ctx.pages:
            if part in p.url and not p.is_closed():
                return p
        return None

    # --- 状态
    def status(self) -> dict:
        out = {"cdp": browser_alive(), "pages": []}
        for p in self.ctx.pages:
            try:
                out["pages"].append({"url": p.url[:110], "title": p.title()[:60]})
            except Exception:
                out["pages"].append({"url": p.url[:110], "title": "<err>"})
        page = self.editor()
        page.bring_to_front()
        loc = page.locator("a:has-text('登录')")
        vis = sum(1 for i in range(loc.count()) if loc.nth(i).is_visible())
        out["logged_in"] = vis == 0 and "/auth" not in page.url
        try:
            ti = page.locator(TITLE_INPUT)
            out["editing_title"] = ti.input_value() if ti.count() else None
        except Exception:
            out["editing_title"] = None
        return out

    # --- 图库
    def upload(self, paths: list[str], wait: float = 10.0) -> dict:
        page = self.editor()
        page.bring_to_front()
        page.set_input_files("#imageFileUploadInput", [str(Path(p).resolve()) for p in paths])
        time.sleep(wait)
        return self.library()

    def library(self) -> dict:
        page = self.editor()
        page.bring_to_front()
        # 素材面板不打开时扫不到图，先点开「我的图库」
        try:
            page.get_by_text("我的图库", exact=True).first.click(timeout=5000)
            time.sleep(2)
        except Exception:
            pass
        items = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('img').forEach(img => {
            const s = img.src || '';
            if (!s.includes('img.xiumi.us')) return;
            const r = img.getBoundingClientRect();
            // 只要左侧素材面板里的图（正文区的图也会命中 img.xiumi.us，必须排除）
            if (r.width < 20 || r.x > 360) return;
            let tile = img.closest('li,div'), name = '';
            for (let i = 0; i < 5 && tile; i++) {
              const t = (tile.innerText || '').trim();
              if (t && t.length < 60) { name = t.split('\\n')[0]; break; }
              tile = tile.parentElement;
            }
            out.push({name, src: s.split('?')[0]});
          });
          return out;
        }""")
        lib = {}
        for it in items:
            n = (it.get("name") or "").strip()
            if n and n not in lib:
                lib[n] = it["src"]
        return lib

    # --- 编辑
    def set_title(self, text: str):
        page = self.editor()
        page.bring_to_front()
        page.locator(TITLE_INPUT).fill(text)

    def inject_html(self, html: str | Path):
        page = self.editor()
        page.bring_to_front()
        h = Path(html).read_text(encoding="utf-8") if Path(str(html)).exists() else str(html)
        area = page.locator(CONTENT).first
        area.click()
        time.sleep(1)
        area.evaluate("(el, x) => { el.innerHTML = x; }", h)
        area.evaluate("el => el.dispatchEvent(new Event('input', {bubbles: true}))")
        time.sleep(2)

    def cells(self) -> list[dict]:
        page = self.editor()
        return page.evaluate("""() => Array.from(document.querySelectorAll("div[contenteditable='true']"))
            .map((e, i) => ({i, text: (e.innerText || '').trim().slice(0, 60),
                             h: Math.round(e.getBoundingClientRect().height)}))""")

    def set_cells(self, mapping: dict) -> list:
        page = self.editor()
        mapping = {str(k): v for k, v in mapping.items()}
        return page.evaluate("""(map) => {
          const ce = Array.from(document.querySelectorAll("div[contenteditable='true']"));
          const res = [];
          for (const [k, text] of Object.entries(map)) {
            const el = ce[Number(k)];
            if (!el) { res.push([k, 'missing']); continue; }
            const lines = String(text).split('\\n');
            const ps = Array.from(el.querySelectorAll('p'));
            if (ps.length === 0) { el.textContent = text; res.push([k, 'plain']); }
            else {
              ps.forEach((para, i) => {
                const holder = para.querySelector('span') || para;
                if (i < lines.length) holder.textContent = lines[i];
                else para.style.display = 'none';
              });
              res.push([k, 'ok:' + ps.length]);
            }
            el.dispatchEvent(new Event('input', {bubbles: true}));
          }
          return res;
        }""", mapping)

    def set_images(self, urls: list[str], skip: int = 0) -> list:
        page = self.editor()
        return page.evaluate("""(args) => {
          const [srcs, skip] = args;
          const ws = Array.from(document.querySelectorAll('.tn-image-inst-wrapper'))
            .filter(w => w.querySelector('img') && w.getBoundingClientRect().width > 50);
          const out = [];
          ws.forEach((w, i) => {
            const n = i - skip;
            if (n >= 0 && n < srcs.length) {
              const img = w.querySelector('img');
              img.src = srcs[n];
              img.removeAttribute('data-src');
              w.dispatchEvent(new Event('input', {bubbles: true}));
              out.push([i, 'set']);
            }
          });
          return out;
        }""", [urls, skip])

    def add_images(self, urls: list[str], after_cell: int | None = None,
                   at: str = "tail") -> list:
        """克隆一个图片单元格并写入新地址。at=tail 追加到最后一个图片位之后；"""
        page = self.editor()
        return page.evaluate("""(args) => {
          const [srcs, afterCell, at] = args;
          const ws = Array.from(document.querySelectorAll('.tn-image-inst-wrapper'))
            .filter(w => w.querySelector('img') && w.getBoundingClientRect().width > 50);
          if (!ws.length) return [['err', 'no-image-cell']];
          const proto = ws[ws.length - 1];
          const protoSection = proto.closest('section') || proto.parentElement;
          let anchor = protoSection;
          if (at === 'after-cell' && afterCell !== null) {
            const ce = Array.from(document.querySelectorAll("div[contenteditable='true']"));
            const cell = ce[Number(afterCell)];
            anchor = (cell.closest('section') || cell.parentElement);
          }
          const added = [];
          for (const s of srcs) {
            const c = protoSection.cloneNode(true);
            const img = c.querySelector('img');
            img.src = s; img.removeAttribute('data-src');
            anchor.parentElement.insertBefore(c, anchor.nextSibling);
            anchor = c;
            added.push(s.split('/').pop().slice(0, 24));
          }
          return added;
        }""", [urls, after_cell, at])

    def format_body(self, cells: list[int], size: int = 14, line_height: float = 1.5,
                    indent: bool = True, justify: bool = True) -> list:
        page = self.editor()
        return page.evaluate("""(args) => {
          const [idxs, size, lh, indent, justify] = args;
          const ce = Array.from(document.querySelectorAll("div[contenteditable='true']"));
          const done = [];
          for (const k of idxs) {
            const el = ce[k];
            if (!el) { done.push([k, 'missing']); continue; }
            el.style.fontFamily = "'Microsoft YaHei','微软雅黑',sans-serif";
            el.style.fontSize = size + 'px';
            el.style.lineHeight = String(lh);
            el.querySelectorAll('p').forEach(p => {
              p.style.fontFamily = "'Microsoft YaHei','微软雅黑',sans-serif";
              p.style.fontSize = size + 'px';
              p.style.lineHeight = String(lh);
              p.style.letterSpacing = '0.5px';
              p.style.textIndent = indent && (p.innerText || '').trim() ? '2em' : '0';
              p.style.textAlign = justify ? 'justify' : 'left';
              p.querySelectorAll('span').forEach(s => {
                s.style.fontFamily = "'Microsoft YaHei','微软雅黑',sans-serif";
                s.style.fontSize = size + 'px';
                s.style.lineHeight = String(lh);
              });
            });
            el.dispatchEvent(new Event('input', {bubbles: true}));
            done.push([k, el.querySelectorAll('p').length]);
          }
          return done;
        }""", [cells, size, line_height, indent, justify])

    def save(self) -> bool:
        page = self.editor()
        page.bring_to_front()
        btns = page.get_by_text("保存", exact=True)
        for i in range(btns.count()):
            b = btns.nth(i)
            try:
                if b.is_visible() and (b.bounding_box() or {}).get("y", 999) < 80:
                    b.click()
                    time.sleep(4)
                    return True
            except Exception:
                pass
        return False

    def preview(self, outdir: Path, long_shot: bool = True) -> dict:
        """保存 → 预览 → 取二维码 png / 预览链接 / 预览长图。"""
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        page = self.editor()
        page.bring_to_front()
        self.save()
        pv = page.get_by_text("预览", exact=True)
        for i in range(pv.count()):
            b = pv.nth(i)
            try:
                if b.is_visible() and (b.bounding_box() or {}).get("y", 999) < 80:
                    b.click()
                    break
            except Exception:
                pass
        time.sleep(7)
        info = {"qr": None, "preview_url": None, "long_shot": None}
        qr = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('img').forEach(i => {
            if ((i.src || '').startsWith('data:image/png;base64')) {
              const r = i.getBoundingClientRect();
              if (r.width > 60) out.push(i.src);
            }
          });
          return out;
        }""")
        if qr:
            best = max(qr, key=len)
            f = outdir / "预览二维码.png"
            f.write_bytes(base64.b64decode(best.split(",", 1)[1]))
            info["qr"] = str(f)
        fr = page.evaluate("""() => {
          const f = document.querySelector('iframe.preview-frame, iframe[src*="v.xiumius"]');
          return f ? f.src : null;
        }""")
        info["preview_url"] = fr
        page.screenshot(path=str(outdir / "编辑器截图.png"))
        if fr and long_shot:
            vp = self.ctx.new_page()
            try:
                vp.set_viewport_size({"width": 430, "height": 900})
                vp.goto(fr, wait_until="load", timeout=60000)
                time.sleep(6)
                vp.screenshot(path=str(outdir / "排版预览长图.png"), full_page=True)
                info["long_shot"] = str(outdir / "排版预览长图.png")
            finally:
                vp.close()
        return info

    # --- 打开已有图文（弹窗流程）
    def open_paper(self, match: str) -> str:
        page = self.editor()
        page.bring_to_front()
        o = page.get_by_text("打开", exact=True)
        for i in range(o.count()):
            b = o.nth(i)
            try:
                if b.is_visible() and (b.bounding_box() or {}).get("y", 999) < 80:
                    b.click()
                    break
            except Exception:
                pass
        time.sleep(5)
        card = page.locator(".tn-dialog-show-explorer, .modal.in").first
        card.locator("div,li,a").filter(has_text=match).first.click(timeout=10000)
        time.sleep(2)
        btn = page.locator(".tn-dialog-show-explorer, .modal.in").locator("text=打开").first
        if btn.count():
            btn.click()
        time.sleep(10)
        return "opened"


# ---------------------------------------------------------------- 商城（风格排版）
def shop_search(sess: Session, keyword: str, free: bool = True) -> list[dict]:
    page = sess.page_by_url("shop/paper") or sess.ctx.new_page()
    page.bring_to_front()
    page.goto("https://xiumi.us/#/studio/shop/paper", wait_until="domcontentloaded", timeout=60000)
    time.sleep(8)
    box = None
    for i in range(page.locator("input").count()):
        b = page.locator("input").nth(i)
        try:
            if b.is_visible() and (b.get_attribute("placeholder") or "").startswith("输入标题搜索"):
                box = b
                break
        except Exception:
            pass
    if box:
        box.click()
        box.fill(keyword)
        page.keyboard.press("Enter")
        time.sleep(6)
    if free:
        try:
            page.get_by_text("免费", exact=True).first.click(timeout=5000)
            time.sleep(4)
        except Exception:
            pass
    out = []
    cards = page.locator("div.entry.thumbnail")
    for i in range(cards.count()):
        c = cards.nth(i)
        try:
            t = c.inner_text().strip().split("\n")
            out.append({"tag": t[1] if len(t) > 1 else "", "name": t[0] if t else ""})
        except Exception:
            pass
    return out


def shop_save(sess: Session, tag: str) -> str:
    """在商城里 hover 卡片 → 预览 → 另存给自己。"""
    page = sess.page_by_url("shop/paper")
    if page is None:
        raise SystemExit("先在商城页搜索（shop 子命令）")
    page.bring_to_front()
    card = page.locator("div.entry.thumbnail").filter(has_text=tag).first
    card.scroll_into_view_if_needed()
    time.sleep(1)
    card.hover()
    time.sleep(1)
    card.locator(".my-show-mask").get_by_text("预览", exact=False).first.click(timeout=8000)
    time.sleep(8)
    btn = page.get_by_text("另存给自己", exact=True).first
    btn.click()
    time.sleep(8)
    return "saved-to-my-papers"


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="公众号排版流水线（秀米）")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--paper", default=None,
                        help="目标图文标签页（URL 片段，如 726736288）；不填则用最后一个编辑器页")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    p = sub.add_parser("start")
    p.add_argument("--url", default=EDITOR)

    p = sub.add_parser("lib", parents=[common])
    p.add_argument("-o", "--out", default="library.json")

    p = sub.add_parser("upload", parents=[common])
    p.add_argument("files", nargs="+")

    p = sub.add_parser("title", parents=[common])
    p.add_argument("text")

    p = sub.add_parser("inject", parents=[common])
    p.add_argument("html")

    sub.add_parser("cells", parents=[common])

    p = sub.add_parser("set-cells", parents=[common])
    p.add_argument("map")

    p = sub.add_parser("set-images", parents=[common])
    p.add_argument("urls")

    p = sub.add_parser("add-images", parents=[common])
    p.add_argument("urls")
    p.add_argument("--after-cell", type=int, default=None)

    p = sub.add_parser("format", parents=[common])
    p.add_argument("--cells", required=True, help="逗号分隔的单元格序号")
    p.add_argument("--size", type=int, default=14)
    p.add_argument("--line-height", type=float, default=1.5)

    sub.add_parser("save", parents=[common])

    p = sub.add_parser("preview", parents=[common])
    p.add_argument("-o", "--outdir", default="preview_out")

    p = sub.add_parser("open", parents=[common])
    p.add_argument("--match", required=True)

    p = sub.add_parser("shop")
    p.add_argument("--keyword", required=True)
    p.add_argument("--free", action="store_true")

    p = sub.add_parser("shop-save")
    p.add_argument("--tag", required=True)

    args = ap.parse_args()

    if args.cmd == "start":
        start_browser(args.url)
        return

    sess = Session(ensure=(args.cmd in ("status", "upload", "shop", "shop-save", "preview")),
                   paper=getattr(args, "paper", None))

    if args.cmd == "status":
        log(json.dumps(sess.status(), ensure_ascii=False, indent=1))
    elif args.cmd == "lib":
        lib = sess.library()
        Path(args.out).write_text(json.dumps(lib, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"{len(lib)} 张图片 -> {args.out}")
        for k, v in list(lib.items())[:30]:
            log("  ", k, "->", v[:90])
    elif args.cmd == "upload":
        lib = sess.upload(args.files)
        log(f"上传完成，图库现有 {len(lib)} 张")
    elif args.cmd == "title":
        sess.set_title(args.text)
        log("标题已设置")
    elif args.cmd == "inject":
        sess.inject_html(args.html)
        log("HTML 已注入")
    elif args.cmd == "cells":
        for c in sess.cells():
            log(f'[{c["i"]:>2}] h={c["h"]:>4}  {c["text"] or "<空>"}')
    elif args.cmd == "set-cells":
        m = json.loads(Path(args.map).read_text(encoding="utf-8"))
        log("替换:", sess.set_cells(m))
    elif args.cmd == "set-images":
        u = json.loads(Path(args.urls).read_text(encoding="utf-8"))
        urls = u if isinstance(u, list) else [u[k] for k in sorted(u)]
        log("设置:", sess.set_images(urls))
    elif args.cmd == "add-images":
        u = json.loads(Path(args.urls).read_text(encoding="utf-8"))
        urls = u if isinstance(u, list) else [u[k] for k in sorted(u)]
        if args.after_cell is not None:
            log("插入:", sess.add_images(urls, after_cell=args.after_cell, at="after-cell"))
        else:
            log("插入:", sess.add_images(urls))
    elif args.cmd == "format":
        idxs = [int(x) for x in args.cells.split(",") if x.strip()]
        log("格式化:", sess.format_body(idxs, size=args.size, line_height=args.line_height))
    elif args.cmd == "save":
        log("保存:", sess.save())
    elif args.cmd == "preview":
        log(json.dumps(sess.preview(Path(args.outdir)), ensure_ascii=False, indent=1))
    elif args.cmd == "open":
        log(sess.open_paper(args.match))
    elif args.cmd == "shop":
        for c in shop_search(sess, args.keyword, free=args.free):
            log(c)
    elif args.cmd == "shop-save":
        log(shop_save(sess, args.tag))


if __name__ == "__main__":
    main()
