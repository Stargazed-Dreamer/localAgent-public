#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fix_bat_encoding.py —— 把含中文的 Windows 批处理(.bat/.cmd)修成 cmd.exe 能稳定解析的格式。

本机结论（Windows 10 build 19045，ACP=936 / OEMCP=936）：
    编码 = GBK(CP936)  换行 = CRLF  BOM = 不要

用法：
    python fix_bat_encoding.py <输入.bat> [输出.bat] [--enc gbk|utf8] [--inplace]
    python fix_bat_encoding.py <输入.bat> --check        # 只自检不修改，全 OK 退出码 0

说明：
  * 默认输出 GBK + CRLF + 无 BOM，与本机 ACP/OEMCP=936 一致，cmd.exe 原生可读。
  * --enc utf8 时输出 UTF-8 无 BOM + CRLF，并自动在 @echo off 后插入 chcp 65001。
    该方案在本机实测可行，但 chcp 65001 在部分系统/字体下仍有兼容风险，非首选。
  * 源编码自动探测：BOM -> gb18030 -> utf-8。
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def detect(data: bytes) -> str:
    if data[:3] == b'\xef\xbb\xbf':
        return 'utf-8-sig'
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return 'utf-16'
    for enc in ('gb18030', 'utf-8'):
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return 'gb18030'          # 兜底，宽松解码


def normalize(text: str) -> str:
    """统一换行为 CRLF，并保证文件以换行结尾。"""
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = text.replace('\n', '\r\n')
    if not text.endswith('\r\n'):
        text += '\r\n'
    return text


def convert(src: str, dst: str, enc: str = 'gbk') -> dict:
    raw = open(src, 'rb').read()
    src_enc = detect(raw)
    text = raw.decode(src_enc)

    if enc == 'utf8':
        # chcp 65001 必须紧跟 @echo off，且要放到最前面
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if 'chcp 65001' not in text:
            # 插到第 1 行之后，绝不改动首行（首行可能是被注释掉的 @echo off）
            lines = text.split('\n')
            lines.insert(1, 'chcp 65001 >nul 2>&1')
            text = '\n'.join(lines)
        out = normalize(text).encode('utf-8')
        used = 'utf-8 (no BOM)'
    else:
        out = normalize(text).encode('gbk')
        used = 'gbk (cp936)'

    if os.path.abspath(src) == os.path.abspath(dst):
        # in-place：先写临时文件再替换，避免中途失败丢内容
        tmp = dst + '.tmp'
        open(tmp, 'wb').write(out)
        os.replace(tmp, dst)
    else:
        open(dst, 'wb').write(out)

    return {
        'src': src, 'dst': dst, 'src_enc': src_enc, 'dst_enc': used,
        'crlf': out.count(b'\r\n'), 'lone_lf': out.count(b'\n') - out.count(b'\r\n'),
        'bom': out[:3] == b'\xef\xbb\xbf', 'bytes': len(out),
    }


def check(src: str) -> dict:
    """自检：不修改文件，判定是否满足 GBK + CRLF + 无 BOM + 末尾换行。"""
    raw = open(src, 'rb').read()
    crlf = raw.count(b'\r\n')
    lone_lf = raw.count(b'\n') - crlf
    lone_cr = raw.count(b'\r') - crlf
    try:
        raw.decode('gbk')
        gbk_ok = True
    except UnicodeDecodeError:
        gbk_ok = False
    try:
        raw.decode('utf-8')
        utf8_ok = True
    except UnicodeDecodeError:
        utf8_ok = False
    return {
        'src': src,
        'bom': raw[:3] == b'\xef\xbb\xbf',
        'crlf': crlf, 'lone_lf': lone_lf, 'lone_cr': lone_cr,
        'gbk_ok': gbk_ok, 'utf8_ok': utf8_ok,
        'ends_nl': raw.endswith(b'\r\n'),
        'empty': len(raw) == 0,
    }


def main(argv):
    if '--check' in argv:
        files = [a for a in argv[1:] if not a.startswith('--')]
        if not files:
            print(__doc__)
            return 2
        all_ok = True
        for f in files:
            r = check(f)
            if r['empty']:
                print('%s: FAIL (空文件)' % f)
                all_ok = False
                continue
            items = [
                ('BOM', not r['bom']),
                ('GBK可解码', r['gbk_ok']),
                ('孤立LF=0', r['lone_lf'] == 0),
                ('孤立CR=0', r['lone_cr'] == 0),
                ('CRLF结尾', r['ends_nl']),
            ]
            ok = all(v for _, v in items)
            all_ok = all_ok and ok
            verdict = 'OK' if ok else 'FAIL'
            extra = '  (可被 utf-8 解码，疑似 UTF-8 文件)' if (not r['gbk_ok'] and r['utf8_ok']) else ''
            detail = '  '.join('%s=%s' % (k, '✓' if v else '✗') for k, v in items)
            print('%s: %s  %s%s' % (f, verdict, detail, extra))
        return 0 if all_ok else 1

    args = [a for a in argv[1:] if not a.startswith('--')]
    if '--enc' in argv:
        enc = argv[argv.index('--enc') + 1]
    else:
        enc = 'gbk'
    if not args:
        print(__doc__)
        return 1
    src = args[0]
    dst = args[1] if len(args) > 1 else (src if '--inplace' in argv else _default_dst(src))
    r = convert(src, dst, enc)
    print('源编码   : %s' % r['src_enc'])
    print('目标编码 : %s' % r['dst_enc'])
    print('CRLF=%d  孤立LF=%d  BOM=%s  大小=%d' % (r['crlf'], r['lone_lf'], r['bom'], r['bytes']))
    print('输出     : %s' % r['dst'])
    return 0


def _default_dst(src: str) -> str:
    root, ext = os.path.splitext(src)
    return root + '.fixed' + (ext if ext else '.bat')


if __name__ == '__main__':
    sys.exit(main(sys.argv))
