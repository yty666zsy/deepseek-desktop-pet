# -*- coding: utf-8 -*-
"""下载免费开源的中文预训练声线（v6.92）

用法：
    .venv\\Scripts\\python.exe tools\\fetch_voices.py --list          # 看清单与状态
    .venv\\Scripts\\python.exe tools\\fetch_voices.py                 # 下载全部缺的
    .venv\\Scripts\\python.exe tools\\fetch_voices.py --only kokoro   # 只下某一个
    .venv\\Scripts\\python.exe tools\\fetch_voices.py --verify        # 只校验已下的完整性

清单在 `pet_voicelib.MODELS` 里（8 个模型 / 约 1.2 GB / 2000+ 说话人）。
下载规则刻意**只取推理必需的文件**：
  · 清单里指定的那一个 onnx（fp32 与 int8 同时存在时只下一个，省一半空间）
  · tokens.txt / lexicon*.txt / dict/ / *.fst（分词与文本正则化，中文必需）
  · kokoro 额外要 voices.bin + espeak-ng-data（多语种音素表）
  · **跳过** 训练垃圾：G_AISHELL.pth（456 MB）、rule.far（172 MB）、.gitattributes、README

文件续传：已存在且大小一致的直接跳过，所以中断后重跑即可。
"""
import argparse
import concurrent.futures as futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pet_voicelib as vlib  # noqa: E402

HF = 'https://huggingface.co'
UA = {'User-Agent': 'desktop-pet-voice-fetcher/1.0'}


# ---------- 联网：代理 / 直连自动挑 ----------
# 本机系统代理是 127.0.0.1:7890（Clash 之类），但 Python 的 urllib 不读注册表，
# 直连 huggingface.co 会 WinError 10060（PowerShell 反而能通，因为它走系统代理）。
# 所以这里自己读注册表拿代理，并保留直连作为备胎；谁先通就一直用它。
_OPENERS = []
_PROXY_HINT = ''


def _system_proxy():
    for k in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'):
        v = os.environ.get(k)
        if v:
            return v.strip()
    try:
        import winreg
        path = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        enable = winreg.QueryValueEx(key, 'ProxyEnable')[0]
        server = str(winreg.QueryValueEx(key, 'ProxyServer')[0] or '')
        if enable and server:
            if '=' in server:                      # http=1.2.3.4:8080;https=... 取 https 那段
                for part in server.split(';'):
                    if part.lower().startswith('https='):
                        return part.split('=', 1)[1].strip()
            return server.split(';')[0].strip()
    except Exception:
        pass
    return ''


def _build_openers():
    global _OPENERS, _PROXY_HINT
    if _OPENERS:
        return _OPENERS
    proxy = _system_proxy()
    if proxy and '://' not in proxy:
        proxy = 'http://' + proxy
    if proxy:
        _PROXY_HINT = proxy
        _OPENERS.append(urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy, 'https': proxy})))
    _OPENERS.append(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    return _OPENERS


def _open(req, timeout=40):
    """按当前顺序试（通了的那个移到最前面，后续全用它）"""
    openers = _build_openers()
    last = None
    for opener in list(openers):
        try:
            with opener.open(req, timeout=timeout) as r:
                data = r.read()
            if openers[0] is not opener:
                openers.remove(opener)
                openers.insert(0, opener)
            return data
        except Exception as e:                                    # noqa: BLE001
            last = e
            if hasattr(req, 'data') or req.get_method() != 'GET':
                break
    raise last if last else RuntimeError('联网失败')


def _tree(repo):
    url = '%s/api/models/%s/tree/main?recursive=true' % (HF, repo)
    return json.loads(_open(urllib.request.Request(url, headers=UA)).decode('utf-8'))


def wanted(m, path):
    """这个远端文件要不要下（清单规则）"""
    name = os.path.basename(path)
    if name in ('.gitattributes', 'README.md') or path.endswith('.md'):
        return False
    if path.endswith(('.pth', '.far', '.ckpt', '.tar', '.zip')):
        return False
    if path == m['onnx']:                      # 只下清单指定的那一个 onnx
        return True
    if path.endswith('.onnx'):                 # 其它导出（fp32/int8 备胎）不要
        return False
    if path in m.get('text', ()) or name in m.get('text', ()):
        return True
    if m.get('voices') and path == m['voices']:
        return True
    for d in m.get('dirs', ()):                # dict/ 与 espeak-ng-data/ 整目录
        if path.startswith(d):
            return True
    return False


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return -1


def download(url, dest, expect, tries=4):
    """带断点续传与重试的单文件下载；返回 (ok, 说明)"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    openers = _build_openers()
    last = ''
    for attempt in range(1, tries + 1):
        have = _size(dest)
        if have == expect and expect > 0:
            return True, 'skip'
        headers = dict(UA)
        if have > 0:
            headers['Range'] = 'bytes=%d-' % have
        for opener in list(openers):
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=90) as r, open(dest, 'ab' if have else 'wb') as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                if openers[0] is not opener:                 # 这条链路通了 → 以后都走它
                    openers.remove(opener)
                    openers.insert(0, opener)
                if _size(dest) == expect:
                    return True, 'ok'
                last = '大小不符（%d/%d 字节）' % (_size(dest), expect)
                break                                        # 链路没问题，只是没下完 → 续传
            except Exception as e:                           # noqa: BLE001
                last = '%s: %s' % (type(e).__name__, str(e)[:60])
                continue                                     # 换下一条链路（代理 ↔ 直连）
        time.sleep(1.2 * attempt)
    return False, '重试 %d 次仍失败（%s）' % (tries, last)


def fetch_model(m, only_missing=True, workers=4):
    key, repo = m['key'], m['repo']
    dest_root = vlib.model_dir(key)
    print('\n==== %s（%s，%s MB） ====' % (key, m['title'], m['mb']))
    try:
        tree = _tree(repo)
    except Exception as e:                                       # noqa: BLE001
        print('  ✕ 取文件列表失败：%s' % str(e)[:100])
        return False
    files = [(t['path'], int(t.get('size') or 0)) for t in tree
             if t.get('type') == 'file' and wanted(m, t['path'])]
    if not files:
        print('  ✕ 清单规则没匹配到任何文件（模型结构可能变了）')
        return False
    total = sum(s for _p, s in files)
    print('  需要 %d 个文件，共 %.0f MB' % (len(files), total / (1024.0 * 1024.0)))

    if only_missing and vlib.is_installed(key):
        print('  ✔ 已装好，跳过')
        return True

    def job(item):
        rel, size = item
        dest = os.path.join(dest_root, rel.replace('/', os.sep))
        # 文件名里可能有空格（espeak-ng-data/voices/!v/Mr serious 就是），必须转义，
        # 否则 urllib 直接抛 InvalidURL（第一版就踩了，只能在重试里空转）
        url = '%s/%s/resolve/main/%s' % (HF, repo, urllib.parse.quote(rel))
        ok, how = download(url, dest, size)
        return rel, ok, how, size

    fails = []
    done = 0
    t0 = time.time()
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for rel, ok, how, size in ex.map(job, files):
            done += 1
            if not ok:
                fails.append((rel, how))
                print('  ✕ %-46s %s' % (rel, how))
            elif done % 10 == 0 or done == len(files):
                pct = done * 100 // max(1, len(files))
                print('  … %d/%d（%d%%）%.0fs' % (done, len(files), pct, time.time() - t0))
    if fails:
        print('  ✕ %d 个文件失败，重跑本脚本会续传' % len(fails))
        return False
    ok = vlib.is_installed(key)
    print('  %s %s（%s MB 已用）' % ('✔' if ok else '✕', '下载完成' if ok else '关键文件缺失',
                                    vlib.disk_usage()))
    return ok


def verify():
    bad = []
    for m in vlib.MODELS:
        key = m['key']
        if not vlib.is_installed(key):
            bad.append(key)
            print('  ✕ %-12s 未安装/不完整' % key)
            continue
        try:
            import sherpa_onnx
        except Exception:
            print('  ! sherpa_onnx 未安装，跳过可加载性检查')
            return 1 if bad else 0
        try:
            vlib.get_tts(key)
            print('  ✔ %-12s 可加载（%s）' % (key, vlib.label(key)))
        except Exception as e:                                   # noqa: BLE001
            bad.append(key)
            print('  ✕ %-12s 加载失败：%s' % (key, str(e)[:100]))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true', help='只打印清单与安装状态')
    ap.add_argument('--only', default='', help='只处理某个 key（逗号分隔）')
    ap.add_argument('--force', action='store_true', help='已装的也重下（校验用）')
    ap.add_argument('--verify', action='store_true', help='只校验已下载模型能否加载')
    args = ap.parse_args()

    if args.list:
        print('本地声线库：%s   目录 %s' % (vlib.summary(), vlib.ROOT))
        for m in vlib.MODELS:
            flag = '✔' if vlib.is_installed(m['key']) else '·'
            print('  %s %-12s %-26s %4d 声线  %4d MB  %s'
                  % (flag, m['key'], m['title'], m['speakers'], m['mb'], m['note']))
        return 0
    if args.verify:
        return verify()

    keys = [k.strip() for k in args.only.split(',') if k.strip()] or [m['key'] for m in vlib.MODELS]
    print('待下载：%s' % ', '.join(keys))
    ok_all = True
    for m in vlib.MODELS:
        if m['key'] not in keys:
            continue
        ok_all = fetch_model(m, only_missing=not args.force) and ok_all
    print('\n结果：%s' % vlib.summary())
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
