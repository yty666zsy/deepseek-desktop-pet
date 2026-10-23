# -*- coding: utf-8 -*-
"""pet_music_free.py — MusicFree 插件桥（Python 侧）

背景：MusicFree（github.com/maotoumao/MusicFree，27.7k★，AGPL-3.0）本体是 React Native 应用，
界面塞不进 PySide6；但官方文档写明「插件本质是导出特定数据结构的 CommonJS 模块，可直接在 node.js 调用」。
所以分工是：
    musicfree/host.js   在 Node 里加载插件，提供 搜索 / 取音源 / 取歌词
    本模块              通过 stdin/stdout 一行一 JSON 跟宿主对话
    pet_music.py        拿到 url 交给 mpv 播放（已有的内核）
    desktop_pet.py      歌词送进头顶气泡

做过的实测（2026-10-21）：
    bilibili 插件搜「周杰伦」→ 返回 BV 号/标题/时长/封面；
    取音源 → 直链（需 Referer 头，mpv 用 --http-header-fields 带上即可）。

纪律：
- 不做任何阻塞重试的死循环；调用带超时，宿主挂了自动重启一次，再挂就报错给上层
- 插件会往 stdout 打日志 → 这里只认能 JSON 解析的行，其余丢弃（协议不会被污染）
"""
import json
import os
import shutil
import subprocess
import threading
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST_DIR = os.path.join(BASE_DIR, 'musicfree')
HOST_JS = os.path.join(HOST_DIR, 'host.js')
PLUGIN_DIR = os.path.join(HOST_DIR, 'plugins')
META_FILE = os.path.join(PLUGIN_DIR, '_plugins.json')   # 文件名 → {name, version, url, source}
VARS_FILE = os.path.join(HOST_DIR, 'vars.json')         # 插件用户变量（Jamendo 的 Client ID 等）
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
UA = 'desktop-pet/6.83 (MusicFree plugin host)'


def _cfg(key, default=''):
    """直接读 config.json（跟本项目的其它模块一个路子，不另立配置层）"""
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return (json.load(f) or {}).get(key, default)
    except Exception:
        return default


def find_node():
    """找 node.exe：config 的 music_node_path → PATH → 常见安装位置"""
    custom = str(_cfg('music_node_path', '') or '').strip()
    if custom and os.path.isfile(custom):
        return custom
    found = shutil.which('node')
    if found:
        return found
    for p in (r'C:\Program Files\nodejs\node.exe',
              os.path.expandvars(r'%LOCALAPPDATA%\Programs\nodejs\node.exe'),
              r'C:\Program Files (x86)\nodejs\node.exe'):
        if os.path.isfile(p):
            return p
    return ''


class PluginHostError(RuntimeError):
    pass


class PluginHost(object):
    """MusicFree 插件宿主的 Python 客户端（线程安全；不要在 GUI 线程里直接长调用）"""

    def __init__(self, node=None, timeout=25.0):
        self.node = node or find_node()
        self.timeout = timeout
        self._proc = None
        self._lock = threading.RLock()
        self._seq = 0
        self._last_error = ''

    # ---------- 环境 ----------
    @staticmethod
    def available():
        """能不能用：Node + host.js + 至少一个插件"""
        if not os.path.isfile(HOST_JS):
            return False
        if not find_node():
            return False
        try:
            return any(n.lower().endswith('.js') for n in os.listdir(PLUGIN_DIR))
        except Exception:
            return False

    def last_error(self):
        return self._last_error

    # ---------- 进程管理 ----------
    def _ensure(self):
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        if not self.node:
            raise PluginHostError('没找到 node.exe（装 Node.js 或在 config.json 里配 music_node_path）')
        if not os.path.isfile(HOST_JS):
            raise PluginHostError('缺少 musicfree/host.js')
        flags = 0
        if sys.platform == 'win32':
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)   # 别弹黑框
        try:
            self._proc = subprocess.Popen(
                [self.node, HOST_JS],
                cwd=HOST_DIR,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                creationflags=flags,
            )
        except Exception as exc:
            raise PluginHostError('启动 Node 宿主失败：%s' % exc)
        return self._proc

    def close(self):
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _readline_with_timeout(self, proc, timeout):
        """Windows 上没有 select，用一个一次性计时器看门狗兜住卡死"""
        box = {}

        def _read():
            try:
                box['line'] = proc.stdout.readline()
            except Exception as exc:
                box['err'] = exc

        th = threading.Thread(target=_read, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise PluginHostError('插件调用超时（%.0f 秒）' % timeout)
        if 'err' in box:
            raise PluginHostError('读取宿主输出失败：%s' % box['err'])
        return box.get('line', '')

    def call(self, method, retry=True, **args):
        """发一次请求并等结果；宿主崩了自动重启重试一次"""
        with self._lock:
            self._seq += 1
            req_id = self._seq
            try:
                proc = self._ensure()
                payload = json.dumps({'id': req_id, 'method': method, 'args': args}, ensure_ascii=False)
                proc.stdin.write(payload + '\n')
                proc.stdin.flush()
                while True:
                    line = self._readline_with_timeout(proc, self.timeout)
                    if not line:
                        raise PluginHostError('宿主没有响应（可能已退出）')
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue        # 插件打印的杂讯，丢掉
                    if msg.get('id') != req_id:
                        continue        # 迟到的旧响应
                    if msg.get('ok'):
                        return msg.get('result')
                    raise PluginHostError(str(msg.get('error') or '未知错误'))
            except Exception as exc:
                self._last_error = str(exc)
                self.close()
                if retry:
                    return self.call(method, retry=False, **args)
                raise

    # ---------- 业务接口 ----------
    def list_plugins(self):
        return (self.call('list') or {}).get('plugins') or []

    def reload(self):
        return (self.call('reload') or {}).get('plugins') or []

    def search(self, plugin, query, page=1, kind='music', limit=0):
        """搜索 → [(插件名, {id,title,artist,album,duration,artwork,...})]"""
        res = self.call('search', plugin=plugin, query=query, page=page, type=kind) or {}
        data = res.get('data') or []
        if limit:
            data = data[:limit]
        return data

    def media(self, plugin, item, quality='standard'):
        """取真实音源 → {'url':..., 'headers': {...}|None}"""
        res = self.call('media', plugin=plugin, item=item, quality=quality) or {}
        url = (res.get('url') or '').strip()
        if not url:
            raise PluginHostError('插件没给音源 URL')
        headers = res.get('headers') or None
        if not headers and res.get('userAgent'):
            headers = {'User-Agent': res['userAgent']}
        return {'url': url, 'headers': headers}

    def lyric(self, plugin, item):
        """取歌词 → (rawLrc, translation)"""
        res = self.call('lyric', plugin=plugin, item=item) or {}
        return (res.get('rawLrc') or '', res.get('translation') or '')


# ---------------------------------------------------------------------------
# 插件订阅（v6.83）
#
# MusicFree 的「订阅」本质就是一个 json 清单：[{name, url, version}, ...]。
# 有了它就不用手动一个个下 .js —— 面板里填订阅地址，一键装/更新全部音源。
# 实测可用：https://hebijunge.github.io/musicfree-plugins/plugins.json（57 个源）
# ---------------------------------------------------------------------------
import hashlib
import re
import time
import urllib.parse
import urllib.request


def _clean_url(url):
    """订阅里的地址常带中文文件名/空格（`元力QQ.js`），不编码的话 urllib 直接抛 ascii 错误"""
    url = ''.join(ch for ch in str(url or '').strip() if ord(ch) >= 32)
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@&=+$,~!*'()")
    query = urllib.parse.quote(parts.query, safe="=&%:/@+$,~!*'()[]")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ''))


def _http_get(url, timeout=30, retries=2):
    """带重试的 GET：插件站偶尔回 418/503（反爬/限流），换个 UA 再来一次往往就好了"""
    last = None
    for i in range(retries + 1):
        ua = UA if i == 0 else ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
        req = urllib.request.Request(_clean_url(url), headers={'User-Agent': ua, 'Accept': '*/*'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last = exc
            if i < retries:
                time.sleep(0.8 * (i + 1))
    raise last


def plugin_slug(url, name=''):
    """插件文件名：`qq-source.plugin.v1.9.23.js` → `qq-source.js`

    去掉版本号的原因是「更新要覆盖同一个文件」，不然每次升级都留一份旧文件，
    插件列表里会冒出一堆同名重复项。中文文件名（`元力QQ.js`）原样保留 —— 换成
    哈希会把 76 个插件全变成 plugin_xxxxxxxx.js，找都没法找。
    """
    base = os.path.basename(urllib.parse.unquote(str(url or '').split('?')[0].split('#')[0])).strip()
    stem = re.sub(r'(\.plugin)?(\.v?\d+(\.\d+)*)?\.js$', '', base, flags=re.I)
    stem = re.sub(r'[-_.]v?\d+(\.\d+)*$', '', stem, flags=re.I)
    stem = re.sub(r'[^\w.\-]+', '-', stem, flags=re.UNICODE).strip('-._')
    if not stem:
        stem = 'plugin_' + hashlib.md5(str(name or url).encode('utf-8')).hexdigest()[:8]
    return stem + '.js'


def parse_subscription(text):
    """解析订阅清单 → [{name, url, version}]

    野格式太多，能认的都认：`{plugins:[...]}` / `{pluginList:[...]}` / 裸数组；
    条目里的地址字段可能是 url / srcUrl / downloadUrl。
    """
    try:
        data = json.loads(text if isinstance(text, str) else text.decode('utf-8', 'replace'))
    except Exception as exc:
        raise PluginHostError('订阅内容不是合法 JSON：%s' % exc)
    if isinstance(data, dict):
        for key in ('plugins', 'pluginList', 'data', 'list'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise PluginHostError('订阅里找不到插件列表（需要 plugins / pluginList 字段）')
    if not isinstance(data, list):
        raise PluginHostError('订阅格式不认识：既不是数组也没有插件列表')
    out, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        url = ''
        for key in ('url', 'srcUrl', 'downloadUrl', 'script'):
            if isinstance(item.get(key), str) and item[key].strip():
                url = item[key].strip()
                break
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({
            'name': str(item.get('name') or item.get('title') or plugin_slug(url)).strip(),
            'url': url,
            'version': str(item.get('version') or '').strip(),
        })
    return out


def read_meta():
    """已装插件的来源/版本（文件名 → {name, version, url, source}）"""
    try:
        with open(META_FILE, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_meta(meta):
    try:
        os.makedirs(PLUGIN_DIR, exist_ok=True)
        tmp = META_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        return os.replace(tmp, META_FILE) or True
    except Exception:
        return False


def install_subscription(url, progress=None, force=False, timeout=30):
    """从订阅地址批量装插件 → 报告

    progress(done, total, text)：每处理一个插件回调一次（在后台线程里调，界面自己转成信号）
    返回 {'added','updated','skipped','failed':[(名字, 原因)], 'total', 'names'}
    """
    entries = parse_subscription(_http_get(url, timeout=timeout))
    if not entries:
        raise PluginHostError('订阅里没有可用的插件条目')
    os.makedirs(PLUGIN_DIR, exist_ok=True)
    meta = read_meta()
    report = {'added': 0, 'updated': 0, 'skipped': 0, 'failed': [], 'total': len(entries), 'names': []}
    taken = set(meta.keys())
    for i, entry in enumerate(entries):
        fname = plugin_slug(entry['url'], entry['name'])
        # 两个不同插件算出的文件名撞车时加序号，别互相覆盖
        if fname in taken and (meta.get(fname) or {}).get('url') not in (None, entry['url']):
            stem, n = fname[:-3], 2
            while ('%s-%d.js' % (stem, n)) in taken:
                n += 1
            fname = '%s-%d.js' % (stem, n)
        taken.add(fname)
        path = os.path.join(PLUGIN_DIR, fname)
        old = meta.get(fname) or {}
        if (not force) and old.get('version') and old.get('version') == entry['version'] and os.path.isfile(path):
            report['skipped'] += 1
            if progress:
                progress(i + 1, len(entries), '%s（已是最新）' % entry['name'])
            continue
        try:
            blob = _http_get(entry['url'], timeout=timeout)
            if not blob or len(blob) < 200:
                raise PluginHostError('下载内容异常（%d 字节）' % len(blob or b''))
            tmp = path + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(blob)
            os.replace(tmp, path)
        except Exception as exc:
            report['failed'].append((entry['name'], str(exc)[:120]))
        else:
            report['updated' if old else 'added'] += 1
            report['names'].append(entry['name'])
            meta[fname] = {'name': entry['name'], 'version': entry['version'],
                           'url': entry['url'], 'source': url}
        if progress:
            progress(i + 1, len(entries), entry['name'])
    _write_meta(meta)
    return report


def plugin_titles():
    """文件名 → 中文名（下拉框显示用；没有元数据就返回空）"""
    out = {}
    for fname, info in read_meta().items():
        if isinstance(info, dict) and info.get('name'):
            out[fname] = info['name']
    return out


def read_vars():
    """插件用户变量（Jamendo 的 Client ID、Navidrome 地址账号…）"""
    try:
        with open(VARS_FILE, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_vars(data):
    try:
        os.makedirs(os.path.dirname(VARS_FILE), exist_ok=True)
        tmp = VARS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data or {}, f, ensure_ascii=False, indent=2)
        return os.replace(tmp, VARS_FILE) or True
    except Exception:
        return False


def set_plugin_vars(platform, values):
    """给某个插件写一组变量（键名照插件自己的，比如 jamendo 的 clientId）"""
    data = read_vars()
    cur = data.get(platform)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(values or {})
    data[platform] = cur
    return write_vars(data)
