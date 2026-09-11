"""通过 CDP 连接已有调试浏览器（Chromium 内核）并打开高分面经标签页"""
import asyncio

from playwright.async_api import async_playwright

RECOMMENDED = [
    {"title": "游戏客户端面经及经历分享(47分)", "url": "https://www.nowcoder.com/discuss/860341579272212480"},
    {"title": "24届游戏客户端暑期实习面经汇总(44分)", "url": "https://www.nowcoder.com/discuss/486288985824817152"},
    {"title": "游戏引擎暑期实习面经-腾讯网易字节(43分)", "url": "https://m.nowcoder.com/feed/main/detail/f95a51f3ef9b43dbab56e882b3e89c3f"},
    {"title": "字节跳动游戏客户端一二三加面(40分)", "url": "https://www.nowcoder.com/feed/main/detail/1daa83ea93d84575ab9270936ac2ada9"},
]

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        print(f"已连接调试浏览器，{len(browser.contexts)} 个上下文")

        context = browser.contexts[0]
        print(f"当前 {len(context.pages)} 个标签页")

        for item in RECOMMENDED:
            page = await context.new_page()
            await page.goto(item["url"], wait_until="domcontentloaded")
            print(f"已打开: {item['title']}")

        print(f"\n完成！已打开 {len(RECOMMENDED)} 个推荐面经标签页")

        # 断开连接但不关闭浏览器
        await browser.close()

asyncio.run(main())
