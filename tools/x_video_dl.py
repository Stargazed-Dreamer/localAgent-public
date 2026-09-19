# -*- coding: utf-8 -*-
"""x.com（Twitter）视频批量下载工具 — 走 savetwitter.net 免梯子解析链路。

用法:
    uv run python tools/x_video_dl.py <URL> [URL ...] [--out DIR] [--seq-start N]
                                     [--max-res N] [--delay SEC] [--prefix TAG]

链路: savetwitter.net api/ajaxSearch 解析 → dl.snapcdn.app 签名直链（JWT 1h 有效）
→ 流式下载。顺序前台执行，逐条输出进度，条间默认 1.5s 限速——不做后台整批猛跑。

坑位备忘（实测踩出，改动前必读）:
- 解析返回的下载按钮 label 内嵌 <i> 图标标签，正则提取直链必须用 (.*?)</a>
  再去标签；用 [^<]* 会全部匹配失败（症状: 全部报"无直链"但 status=ok）。
- 输出文件名严禁含':'（Windows 会静默写成 NTFS 备用数据流 ADS: 主文件 0 字节、
  播放器打不开、数据藏在 dir /r 才可见的流里）。本工具对文件名做白名单清洗。

账本去重: 输出目录下 download_manifest.json 按 tweet_id 记账。账本已有的一律
跳过——文件还在=已下载；文件被删=用户筛掉不想留的，也不重下。--force 无视账本。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

API = 'https://savetwitter.net/api/ajaxSearch'
REFERER = 'https://savetwitter.net/en4'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
STATUS_RE = re.compile(r'https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)')


def build_session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False  # 直连可用，绕开任何系统代理
    s.headers.update({'User-Agent': UA, 'X-Requested-With': 'XMLHttpRequest',
                      'Referer': REFERER})
    return s


def parse_tweet(url: str) -> tuple[str, str]:
    m = STATUS_RE.match(url.strip())
    if not m:
        raise ValueError(f'不是 x.com/twitter 状态链接: {url}')
    return m.group(1), m.group(2)


def resolve(session: requests.Session, tweet_url: str) -> list[tuple[int, str]]:
    """解析出 [(分辨率数字, 直链)]，按分辨率降序。"""
    r = session.post(API, data={
        'q': tweet_url,
        'k_url_search': 'https://savetwitter.net/api/ajaxSearch',
    }, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get('status') != 'ok':
        raise RuntimeError(f'API status={payload.get("status")}')
    data = payload.get('data', '')
    out = []
    # 按钮 label 内嵌 <i> 图标标签，必须非贪婪到 </a> 再去标签（见模块 docstring 坑位）
    for href, raw_label in re.findall(
            r'href="(https://dl\.snapcdn\.app/get\?token=[^"]+)"[^>]*>(.*?)</a>',
            data, re.S):
        label = re.sub(r'<[^>]+>', '', raw_label)
        if 'MP4' not in label.upper():
            continue
        m = re.search(r'\((\d{3,4})p\)', label)
        res = int(m.group(1)) if m else 0
        out.append((res, href))
    out.sort(key=lambda x: -x[0])
    return out


def pick(cands: list[tuple[int, str]], max_res: int | None) -> tuple[int, str]:
    """选目标分辨率：≤max_res 的最高档；没有则取最低档兜底。max_res=None 取最高档。"""
    if max_res is None:
        return cands[0]
    under = [c for c in cands if c[0] and c[0] <= max_res]
    return under[0] if under else cands[-1]


def safe_filename(name: str) -> str:
    """文件名白名单清洗：杜绝 ADS 冒号坑与 Windows 非法字符。"""
    cleaned = re.sub(r'[\\/:*?"<>|]', '', name).strip('. ')
    return cleaned or 'video.mp4'


MANIFEST_NAME = 'download_manifest.json'


def load_manifest(out_dir: Path) -> dict:
    """读下载账本（按 tweet_id 记账）。损坏时报错退出，不静默当空账本（会重下）。"""
    p = out_dir / MANIFEST_NAME
    if not p.exists():
        return {'downloaded': {}}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f'账本 {p} 读取失败（{e}），请先手工处理该文件再跑')
    data.setdefault('downloaded', {})
    return data


def save_manifest(out_dir: Path, manifest: dict) -> None:
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')


def download(session: requests.Session, url: str, dest: Path) -> int:
    with session.get(url, stream=True, timeout=(15, 90)) as r:
        r.raise_for_status()
        size = 0
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
                size += len(chunk)
    return size


def verify_mp4(path: Path) -> bool:
    try:
        return path.read_bytes()[:8][4:8] == b'ftyp'
    except OSError:
        return False


def process(session: requests.Session, raw_url: str, dest_dir: Path,
            seq: int, prefix: str, max_res: int | None) -> tuple[str, str, dict | None]:
    """下载单条。返回 (status, 摘要, 成功时的账本附加信息)。"""
    handle, tid = parse_tweet(raw_url)
    tweet_url = f'https://x.com/{handle}/status/{tid}'
    tag = safe_filename(f'{seq:02d}_{prefix}_{handle}') if prefix \
        else safe_filename(f'{seq:02d}_{handle}')
    print(f'\n[{seq:02d}] {tweet_url}', flush=True)

    last_err: Exception | None = None
    for attempt in (1, 2):  # 网络类失败重试一次
        try:
            cands = resolve(session, tweet_url)
            if not cands:
                raise RuntimeError('未解析出 MP4 直链（可能非视频帖或站点风控）')
            res, href = pick(cands, max_res)
            dest = dest_dir / f'{tag}_{res}p.mp4'
            print(f'  选定 {res}p（候选 {len(cands)} 档），下载中...', flush=True)
            size = download(session, href, dest)
            if not verify_mp4(dest):
                dest.unlink(missing_ok=True)
                raise RuntimeError('下载内容校验失败（无 MP4 头），已删除半截文件')
            mb = size / 1048576
            print(f'  ✅ {mb:.1f}MB -> {dest.name}', flush=True)
            return 'ok', f'{res}p {mb:.1f}MB', {'file': dest.name, 'res': res,
                                                'size': size}
        except Exception as e:  # noqa: BLE001 逐条隔离，单条失败不拖垮整批
            last_err = e
            if attempt == 1:
                print(f'  第1次失败（{e}），重试...', flush=True)
                time.sleep(3)
    print(f'  ❌ {type(last_err).__name__}: {last_err}', flush=True)
    return 'fail', f'{type(last_err).__name__}: {last_err}', None


def main() -> int:
    parser = argparse.ArgumentParser(
        description='x.com(Twitter) 视频下载（savetwitter.net 免梯子链路）')
    parser.add_argument('urls', nargs='+', help='x.com/twitter 状态链接，可多个')
    parser.add_argument('--out', default='',
                        help='输出目录（默认 ~/<data_drive>:/Downloads/x_videos_<当天日期>）')
    parser.add_argument('--seq-start', type=int, default=1, help='序号起始（默认 1）')
    parser.add_argument('--prefix', default='', help='文件名附加标记（如 追加/群名），冒号等非法字符自动清除')
    parser.add_argument('--max-res', type=int, default=None,
                        help='分辨率上限，如 720（默认取最高档）')
    parser.add_argument('--delay', type=float, default=1.5,
                        help='条间限速秒数（默认 1.5）')
    parser.add_argument('--force', action='store_true',
                        help='无视账本去重，已下载过的也强制重下')
    args = parser.parse_args()

    out_dir = (Path(args.out) if args.out else
               Path.home() / '<data_drive>:/Downloads' / f'x_videos_{time.strftime("%Y%m%d")}')
    out_dir.mkdir(parents=True, exist_ok=True)

    session = build_session()
    manifest = load_manifest(out_dir)
    results = []
    for i, raw in enumerate(args.urls, args.seq_start):
        tid = None
        try:
            _, tid = parse_tweet(raw)
        except ValueError:
            pass  # 非法链接照旧交给 process 报错
        if tid and not args.force and tid in manifest['downloaded']:
            rec = manifest['downloaded'][tid]
            fname = rec.get('file', '')
            gone = ('，文件已被删（视为筛掉不想留）'
                    if not fname or not (out_dir / fname).exists() else '')
            print(f'\n[{i:02d}] ⏭ 账本已有{gone}，跳过: {raw}', flush=True)
            results.append((raw, 'skip', f"已有 {fname or '?'}", None))
            continue
        status, info, extra = process(session, raw, out_dir, i, args.prefix,
                                      args.max_res)
        results.append((raw, status, info, extra))
        if status == 'ok' and tid and extra:
            manifest['downloaded'][tid] = {'url': raw.strip(), 'file': extra['file'],
                                           'res': extra['res'], 'size': extra['size'],
                                           'time': time.strftime('%Y-%m-%d %H:%M:%S')}
            save_manifest(out_dir, manifest)
        if i - args.seq_start < len(args.urls) - 1:
            time.sleep(args.delay)

    ok = sum(1 for r in results if r[1] == 'ok')
    skip = sum(1 for r in results if r[1] == 'skip')
    print(f'\n=== 汇总: 新下 {ok}/{len(results)}，账本跳过 {skip} ===')
    for raw, status, info, _ in results:
        print(f'{status.upper():4} {raw}  {info}')
    print(f'目录: {out_dir}')
    return 0 if ok + skip == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
