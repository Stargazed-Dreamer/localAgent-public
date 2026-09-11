"""浏览器指纹一致性自检 —— localAgent 浏览器链路的体检工具。

为什么需要它
------------
localAgent 的浏览器接入方式是 `connect_over_cdp` 连**用户手动启动的真实有头
Chrome**，指纹天生合规。历史教训（2026-09-03）是：项目曾对每个 page 应用
`playwright-stealth`，结果在真实浏览器上制造了三处自相矛盾的假指纹，且永久
污染用户 tab 的后续导航。该依赖已移除，本脚本用于**防止这类补丁被重新引入**。

它不检测「像不像机器人」，而是检测「指纹自不自洽」——因为 bot.sannysoft.com
这类检测站会给出全绿的结果，却看不出 `navigator.language=zh-CN` 与
`navigator.languages=["en-US","en"]` 同时存在的矛盾。**通过率不等于安全。**

用法
----
    uv run python tools/browser/fingerprint_selfcheck.py
    uv run python tools/browser/fingerprint_selfcheck.py --port 9222
    uv run python tools/browser/fingerprint_selfcheck.py --json temp/fp.json

退出码
------
    0  全部通过（允许 WARN）
    1  存在 FAIL 项
    2  自检无法执行（调试浏览器未启动等）

设计约束
--------
- 用 `browser.new_context()` 建**临时隔离 context**，跑完即关，不污染用户 profile。
- 只读，不写任何持久化状态；不加 init script（自检本身绝不能改变被测对象）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

# playwright-stealth 2.0.3 的 webgl 伪造默认值，出现即说明有人又加了补丁
_STEALTH_WEBGL_RENDERER = "Intel Iris OpenGL Engine"
_STEALTH_WEBGL_VENDOR = "Intel Inc."
# 无头 Chromium 的软件渲染特征
_HEADLESS_RENDERER_MARKERS = ("SwiftShader", "Mesa OffScreen", "llvmpipe")


@dataclass
class Check:
    name: str
    status: str = PASS
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, hint: str = "",
            warn_only: bool = False) -> None:
        if ok:
            status = PASS
        elif warn_only:
            status = WARN
        else:
            status = FAIL
        self.checks.append(Check(name, status, detail, hint))

    def add_warn(self, name: str, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name, WARN, detail, hint))


# --------------------------------------------------------------------------
# 一次性采集：单次导航内取齐所有 JS 侧指纹 + 本次请求的 HTTP 头
# --------------------------------------------------------------------------

PROBE_JS = """() => {
  const nav = navigator;
  let webgl = {vendor: '', renderer: ''};
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) {
      const ext = gl.getExtension('WEBGL_debug_renderer_info');
      if (ext) {
        webgl.vendor   = String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL));
        webgl.renderer = String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL));
      }
    }
  } catch (e) { webgl.vendor = 'error'; webgl.renderer = 'error'; }

  let uaDataVersion = '';
  try {
    const d = nav.userAgentData;
    if (d && d.brands) {
      const c = d.brands.find(b => /chromium/i.test(b.brand));
      uaDataVersion = c ? String(c.version) : '';
    }
  } catch (e) {}

  return {
    userAgent:       nav.userAgent,
    language:        nav.language,
    languages:       Array.from(nav.languages || []),
    platform:        nav.platform,
    vendor:          nav.vendor,
    webdriver:       nav.webdriver,
    webdriverOwn:    Object.prototype.hasOwnProperty.call(nav, 'webdriver'),
    pluginsLen:      nav.plugins ? nav.plugins.length : -1,
    pluginsIsArray:  nav.plugins instanceof PluginArray,
    mimeTypesLen:    nav.mimeTypes ? nav.mimeTypes.length : -1,
    hwConcurrency:   nav.hardwareConcurrency,
    deviceMemory:    nav.deviceMemory,
    hasWindowChrome: typeof window.chrome !== 'undefined',
    uaDataVersion:   uaDataVersion,
    webglVendor:     webgl.vendor,
    webglRenderer:   webgl.renderer,
    // 注入痕迹：真实浏览器的 offsetHeight 定义在 HTMLElement.prototype 上，
    // HTMLDivElement.prototype 自身不含该属性。stealth 的 hairline 补丁会凭空加一个。
    divOwnOffsetHeight: Object.getOwnPropertyNames(
        HTMLDivElement.prototype).includes('offsetHeight'),
    fnToStringPatched: !Function.prototype.toString
        .toString().includes('native code'),
    httpAcceptLanguage: (document.getElementById('hdr').textContent
        .match(/^accept-language: (.+)$/m) || [null, ''])[1].trim(),
  };
}"""


def _start_echo_server() -> tuple[object, int]:
    """起临时本地 HTTP 服务，把**本次请求**的头渲染进返回的 HTML。

    这让一次导航就能同时拿到 HTTP 头层与 JS 层的值，彻底规避两次导航
    互相覆盖的竞态（头是逐请求变化的，服务端留存会串味）。
    """
    import html as _html
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            rows = "\n".join(f"{k.lower()}: {v}" for k, v in self.headers.items())
            body = ('<html><body><pre id="hdr">' + _html.escape(rows)
                    + "</pre></body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静音
            pass

    class Server(socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# --------------------------------------------------------------------------
# 判定逻辑
# --------------------------------------------------------------------------

def _evaluate(report: Report, d: dict) -> None:
    langs = d["languages"] or []

    # 1. language 与 languages[0] 必须一致（stealth 只改 languages，会当场穿帮）
    ok = bool(langs) and langs[0] == d["language"]
    report.add("navigator.language 与 languages[0] 一致", ok,
               f"language={d['language']!r} languages={langs!r}",
               "两者在真实浏览器中恒等；不一致说明有 JS 补丁只改了其中一个")

    # 2. HTTP 头 Accept-Language 与 JS 层一致（跨层自洽，最强的一条）
    al = d["httpAcceptLanguage"]
    primary = al.split(",")[0].split(";")[0].strip().lower() if al else ""
    if not primary:
        report.add_warn("HTTP Accept-Language 与 JS 一致",
                        f"未取到 Accept-Language 头（{al!r}）")
    else:
        ok = any(l.lower() == primary or l.lower().startswith(primary.split("-")[0])
                 for l in langs)
        report.add("HTTP Accept-Language 与 JS 一致", ok,
                   f"HTTP={primary!r} JS={langs!r}",
                   "补丁改不了已发出的 HTTP 头，只能改 JS，跨层矛盾一抓一个准")

    # 3. webdriver：真实浏览器恒为 false，且不能是自有属性
    report.add("navigator.webdriver 为 false", d["webdriver"] is False,
               f"webdriver={d['webdriver']!r}",
               "true 意味着浏览器被 WebDriver/CDP 标记为自动化")
    report.add("webdriver 非自有属性", not d["webdriverOwn"],
               f"hasOwnProperty={d['webdriverOwn']}",
               "真实 Chrome 的 webdriver 定义在 Navigator.prototype 上；"
               "在实例上建自有属性本身就是暴露源")

    # 4. plugins / mimeTypes：真实浏览器原生合规，补丁伪造反而挂类型检查
    report.add("plugins 为合规 PluginArray",
               d["pluginsIsArray"] and d["pluginsLen"] > 0,
               f"instanceof={d['pluginsIsArray']} length={d['pluginsLen']}")
    report.add("mimeTypes 非空", d["mimeTypesLen"] > 0,
               f"length={d['mimeTypesLen']}")

    # 5. UA 不含 Headless 标记
    ua = d["userAgent"]
    report.add("UA 无 Headless 标记", "Headless" not in ua,
               ua[:72] + ("..." if len(ua) > 72 else ""))

    # 6. UA 主版本与 userAgentData.brands 一致
    import re
    m = re.search(r"Chrome/(\d+)", ua)
    ua_ver = m.group(1) if m else ""
    ok = bool(ua_ver) and ua_ver == d["uaDataVersion"]
    report.add("UA 版本与 userAgentData 一致", ok,
               f"UA Chrome/{ua_ver or '?'} vs brands Chromium {d['uaDataVersion'] or '?'}",
               "两者不一致是典型的补丁拼接痕迹")

    # 7. WebGL：既不能是无头软件渲染，也不能是 stealth 的伪造默认值
    rend, vend = d["webglRenderer"], d["webglVendor"]
    if not rend:
        report.add_warn("WebGL renderer 可用", "取不到 WebGL renderer")
    elif any(x.lower() in rend.lower() for x in _HEADLESS_RENDERER_MARKERS):
        report.add("WebGL 非软件渲染", False, f"{vend} / {rend}",
                   "SwiftShader/llvmpipe 是无 GPU 无头环境的典型特征")
    elif rend == _STEALTH_WEBGL_RENDERER or vend == _STEALTH_WEBGL_VENDOR:
        report.add("WebGL 非伪造值", False, f"{vend} / {rend}",
                   "这正是 playwright-stealth 的默认伪造值——"
                   "说明有人重新引入了 stealth 补丁")
    else:
        report.add("WebGL 为真实 GPU", True, f"{vend} / {rend}")
        # 8. UA 平台与 navigator.platform 一致（真实 GPU + 真实平台交叉验证）
        plat_ok = True
        if "Windows" in ua:
            plat_ok = d["platform"] == "Win32"
        elif "Macintosh" in ua:
            plat_ok = d["platform"] in ("MacIntel", "Macintosh")
        elif "Linux" in ua:
            plat_ok = d["platform"].startswith("Linux")
        report.add("UA 平台与 navigator.platform 一致", plat_ok,
                   f"UA 平台={('Windows' if 'Windows' in ua else '其他')} "
                   f"platform={d['platform']!r}")

    # 9. 硬件信号合理
    report.add("hardwareConcurrency 合理",
               isinstance(d["hwConcurrency"], int) and d["hwConcurrency"] >= 2,
               f"hardwareConcurrency={d['hwConcurrency']}")
    report.add("deviceMemory 合理",
               isinstance(d["deviceMemory"], (int, float)) and d["deviceMemory"] >= 1,
               f"deviceMemory={d['deviceMemory']}")

    # 10. 无 JS 注入痕迹
    report.add("HTMLDivElement.prototype 无注入属性", not d["divOwnOffsetHeight"],
               f"含 offsetHeight={d['divOwnOffsetHeight']}",
               "stealth 的 hairline 补丁会在此凭空加 offsetHeight 属性")
    report.add("Function.prototype.toString 未被替换", not d["fnToStringPatched"],
               f"patched={d['fnToStringPatched']}",
               "补丁库常用它来隐藏 Proxy 痕迹；被替换说明有注入")

    # 11. 真实 Chrome 特征
    report.add("window.chrome 存在", d["hasWindowChrome"],
               f"window.chrome={d['hasWindowChrome']}")


# --------------------------------------------------------------------------

async def run(port: int) -> Report:
    from playwright.async_api import async_playwright

    report = Report()
    srv, echo_port = _start_echo_server()
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = await browser.new_context()   # 临时隔离 context，不污染用户 profile
            try:
                page = await ctx.new_page()
                await page.goto(f"http://127.0.0.1:{echo_port}/selfcheck",
                                wait_until="domcontentloaded", timeout=30_000)
                data = await page.evaluate(PROBE_JS)
            finally:
                # 只关自己建的临时 context。
                # ⚠️ 绝不能在这里调 browser.close()：CDP 连接下它会真的关掉
                # 用户的调试浏览器（实测：进程还在但 9222 调试端口已断）。
                # 同理 playwright 上下文退出也不影响浏览器。
                await ctx.close()
    finally:
        try:
            srv.shutdown()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    _evaluate(report, data)
    return report


def _render(report: Report) -> None:
    def w(s: str) -> int:
        return sum(2 if ord(c) > 127 else 1 for c in s)

    name_w = max(w("检查项"), *(w(c.name) for c in report.checks))
    det_w = min(max(w("实测值"), *(w(c.detail) for c in report.checks)), 62)

    def pad(s: str, n: int) -> str:
        s = s if len(s) <= 62 else s[:59] + "..."
        return s + " " * max(0, n - w(s))

    print()
    print("浏览器指纹一致性自检（真实 Chrome + CDP 链路体检）")
    print("=" * (name_w + det_w + 12))
    for c in report.checks:
        print(f"[{c.status:^4}] {pad(c.name, name_w)}  {c.detail}")
    print("=" * (name_w + det_w + 12))

    n_pass = sum(1 for c in report.checks if c.status == PASS)
    n_warn = sum(1 for c in report.checks if c.status == WARN)
    n_fail = sum(1 for c in report.checks if c.status == FAIL)
    print(f"通过 {n_pass} | 警告 {n_warn} | 失败 {n_fail}")
    if n_fail:
        print("\n失败项处理建议：")
        for c in report.checks:
            if c.status == FAIL and c.hint:
                print(f"  - {c.name}：{c.hint}")
        print("\n最常见的成因：有人重新引入了 JS 伪装补丁（如 playwright-stealth）。")
        print("在 connect_over_cdp 的真实浏览器上应当「做减法」——不注入任何补丁。")
        print("详见 docs/browser-anti-detection.md")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="浏览器指纹一致性自检")
    ap.add_argument("--port", type=int, default=9222, help="CDP 调试端口（默认 9222）")
    ap.add_argument("--json", default=None, help="额外输出 JSON 结果到指定文件")
    args = ap.parse_args()

    try:
        report = asyncio.run(run(args.port))
    except Exception as exc:  # noqa: BLE001
        print(f"\n[!] 自检无法执行：{type(exc).__name__}: {exc}")
        print("    请先启动调试浏览器：")
        print("    uv run python tools/browser/start_debug_browser.py")
        return 2

    _render(report)

    if args.json:
        payload = [{"name": c.name, "status": c.status, "detail": c.detail,
                    "hint": c.hint} for c in report.checks]
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[OK] JSON 已写入 {args.json}")

    return 1 if any(c.status == FAIL for c in report.checks) else 0


if __name__ == "__main__":
    sys.exit(main())
