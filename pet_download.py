# -*- coding: utf-8 -*-
"""pet_download.py — 「一键下载网页视频」内核（v6.85）

为什么是 yt-dlp：
    视频下载这件事的事实标准（1000+ 站点：B站/YouTube/抖音/小红书/快手/微博/X/IG…），
    几乎所有 GUI（EGM-Downloader、pull-vids、videdown…）都是它的壳。

为什么界面自己写、只搬参数：
    videdown（github.com/cshuangyy/videdown，Electron + Vue3 + TS，MIT）的界面是 Electron，
    塞不进 PySide6（跟 MusicFree 的 React Native 界面一个道理）。所以这里只搬它的**下载参数与进度策略**
    （见 electron/main.ts）：
        -f <选择器> / --merge-output-format mp4 / --newline / --no-playlist / --encoding utf-8
        音视频分流 → 靠 ffmpeg 合并（--ffmpeg-location 指路，避免 PATH 里没有）
        --cookies-from-browser（B站不带 cookie 会 412，1080P 高码率还要登录才有）
        YouTube 需要 --js-runtimes node:<path>（yt-dlp 新版解析签名用）
        音频模式 -x --audio-format mp3 --audio-quality 0 + FFmpegMetadata 写 ID3
        暂停 / 继续 = **杀掉进程，再用同样的参数重跑**（yt-dlp 自己会从 .part 续传，不用我们拼 Range）

纪律：
- 一律子进程 + 超时，不阻塞 GUI 线程；界面只收信号
- 进度不靠猜 "12.3% of ~50MiB" 这种给人看的文本，用 --progress-template 打固定格式的行自己解析
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'downloads')

# Windows 下 pythonw 起子进程会弹黑框，统一加这个标志
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# 画质预设 → (显示名, 值)。值同时是 -f 选择器的键（audio 特判）
QUALITY_CHOICES = (
    ('最高清（音视频自动合并）', 'best'),
    ('1080P 上限', '1080'),
    ('720P 上限', '720'),
    ('480P 上限', '480'),
    ('只要音频（mp3）', 'audio'),
)

# -f 选择器：bv*=最佳视频流，ba=最佳音频流，末尾的 /b 是「实在不行退回单文件」
FORMAT_BY_QUALITY = {
    'best': 'bv*+ba/b',
    '1080': 'bv*[height<=1080]+ba/b[height<=1080]/b',
    '720': 'bv*[height<=720]+ba/b[height<=720]/b',
    '480': 'bv*[height<=480]+ba/b[height<=480]/b',
}

# 进度行的前后缀：前面加个生僻前缀，免得跟插件/ffmpeg 自己的 [download] 行混在一起
PROGRESS_PREFIX = 'PETDL_PROG|'
FILE_PREFIX = 'PETDL_FILE|'
PROGRESS_TEMPLATE = ('download:' + PROGRESS_PREFIX +
                     '%(progress.downloaded_bytes)s|%(progress.total_bytes)s|'
                     '%(progress.total_bytes_estimate)s|%(progress.speed)s|'
                     '%(progress.eta)s|%(progress.status)s')

# 浏览器 cookie：值要正好是 yt-dlp 认的名字
BROWSER_CHOICES = (
    ('', '不用 cookie'),
    ('edge', 'Edge'),
    ('chrome', 'Chrome'),
    ('firefox', 'Firefox'),
    ('brave', 'Brave'),
)

_BROWSER_HINTS = {
    'chrome': (os.path.join('%LOCALAPPDATA%', 'Google', 'Chrome', 'User Data'),),
    'edge': (os.path.join('%LOCALAPPDATA%', 'Microsoft', 'Edge', 'User Data'),),
    'brave': (os.path.join('%LOCALAPPDATA%', 'BraveSoftware', 'Brave-Browser', 'User Data'),),
    'firefox': (os.path.join('%APPDATA%', 'Mozilla', 'Firefox', 'Profiles'),),
}

# 剪贴板监听只认这些站，不然复制个网页链接桌宠就冒出来问「要不要下载」很烦
_VIDEO_HOSTS = (
    'bilibili.com', 'b23.tv', 'youtube.com', 'youtu.be', 'douyin.com', 'iesdouyin.com',
    'kuaishou.com', 'xiaohongshu.com', 'xhslink.com', 'weibo.com', 'weibo.cn',
    'twitter.com', 'x.com', 'instagram.com', 'tiktok.com', 'vimeo.com', 'twitch.tv',
    'ixigua.com', 'acfun.cn', 'youku.com', 'iqiyi.com', 'v.qq.com', 'mgtv.com',
    'dailymotion.com', 'reddit.com', 'facebook.com', 'pinterest.com', 'bilibili.tv',
)

_URL_RE = re.compile(r'https?://[^\s<>"\'）)】]+', re.I)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


# ---------------------------------------------------------------- 配置
def cfg_read():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def cfg_write(key, value):
    """只改一个键（读-改-写，别把别人的字段冲掉）"""
    try:
        cfg = cfg_read()
        cfg[key] = value
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def _cfg_text(key):
    return str(cfg_read().get(key) or '').strip()


# ---------------------------------------------------------------- 找工具
def find_ytdlp():
    """yt-dlp 在哪：配置 → PATH → venv/基础 Python 的 Scripts → 常见装法"""
    hit = _cfg_text('dl_ytdlp_path')
    if hit and os.path.isfile(hit):
        return hit
    for name in ('yt-dlp.exe', 'yt-dlp'):
        hit = shutil.which(name)
        if hit:
            return hit
    cands = []
    # 桌宠跑在 venv 里，但 yt-dlp 常装在基础 Python 的 Scripts 下
    for prefix in (getattr(sys, 'base_prefix', ''), getattr(sys, 'prefix', '')):
        if prefix:
            cands.append(os.path.join(prefix, 'Scripts', 'yt-dlp.exe'))
            cands.append(os.path.join(prefix, 'bin', 'yt-dlp'))
    cands.append(os.path.join(os.path.expanduser('~'), 'AppData', 'Local', 'Programs', 'yt-dlp', 'yt-dlp.exe'))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ''


def find_ffmpeg():
    """ffmpeg 所在目录（--ffmpeg-location 要目录）。高清是音视频两条流，没它合并不了"""
    hit = _cfg_text('dl_ffmpeg_path')
    if hit:
        return hit if os.path.isdir(hit) else os.path.dirname(hit)
    for name in ('ffmpeg.exe', 'ffmpeg'):
        hit = shutil.which(name)
        if hit:
            return os.path.dirname(hit)
    cands = [
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Links'),
        os.path.join(os.environ.get('ProgramData', ''), 'chocolatey', 'bin'),
        'C:\\ffmpeg\\bin',
    ]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, 'ffmpeg.exe')):
            return c
    return ''


def find_node():
    """YouTube 解析要用 JS 运行时（yt-dlp 新版拿不到就得靠 node）"""
    hit = _cfg_text('dl_node_path')
    if hit and os.path.isfile(hit):
        return hit
    return shutil.which('node') or shutil.which('node.exe') or ''


def installed_browsers():
    """本机装了哪些浏览器（只用来决定下拉框里显示哪些，选了不通 yt-dlp 会自己报错）"""
    out = []
    for value, label in BROWSER_CHOICES:
        if not value:
            continue
        for hint in _BROWSER_HINTS.get(value, ()):
            path = os.path.expandvars(hint)
            if os.path.exists(path):
                out.append((value, label))
                break
    return out


def status():
    """→ (能不能用, 一句人话说明)。面板打开时先看这个，缺东西就直接告诉用户怎么办"""
    ytdlp = find_ytdlp()
    ffmpeg = find_ffmpeg()
    if not ytdlp:
        return False, ('没找到 yt-dlp。装一个就好：pip install -U yt-dlp，'
                       '或者把 yt-dlp.exe 丢到 PATH，也可以在 config.json 里写 dl_ytdlp_path')
    if not ffmpeg:
        return False, ('没找到 ffmpeg。高清视频的音、视频是两条流，必须靠 ffmpeg 合并；'
                       '把 ffmpeg.exe 放进 PATH，或在 config.json 里写 dl_ffmpeg_path')
    return True, 'yt-dlp：%s' % os.path.basename(ytdlp)


def default_outdir():
    """默认存到项目里的 downloads/（config.json 的 dl_dir 可改）"""
    hit = _cfg_text('dl_dir')
    return hit or DOWNLOAD_DIR


# ---------------------------------------------------------------- 链接识别
def looks_like_url(text):
    """文本里有没有 http(s) 链接（面板「粘贴」用，宽松）"""
    if not text or len(str(text)) > 4000:
        return ''
    m = _URL_RE.search(str(text))
    if not m:
        return ''
    return m.group(0).rstrip('.,;!?，。；、）)】"\']')


def looks_like_video_url(text):
    """剪贴板监听用：必须是视频站的链接才自动冒头，普通网页链接不打扰"""
    url = looks_like_url(text)
    if not url:
        return ''
    low = url.lower()
    for host in _VIDEO_HOSTS:
        if host in low:
            return url
    return ''


# ---------------------------------------------------------------- 参数
def build_args(url, outdir=None, quality='best', browser='', cookies_file='',
               fmt_id='', out_template='%(title).80s'):
    """拼 yt-dlp 命令行（顺序照 videdown 的 electron/main.ts，它踩过的坑这里不再踩）

    关键几处：
      --no-simulate  + --progress ：--print 会顺手打开安静模式，不显式要求就看不到进度
      --print after_move:FILE|…  ：让 yt-dlp 告诉我们最终文件落在哪（合并后的真路径）
      --windows-filenames         ：标题里有 : / ? 之类字符时自动换成安全字符
    """
    outdir = outdir or default_outdir()
    ytdlp = find_ytdlp() or 'yt-dlp'
    audio_only = (quality == 'audio')
    selector = fmt_id or FORMAT_BY_QUALITY.get(quality, FORMAT_BY_QUALITY['best'])

    args = [ytdlp, '--no-color', '--newline', '--no-playlist', '--no-simulate', '--progress',
            '--encoding', 'utf-8', '--windows-filenames',
            '-P', outdir,
            '-o', str(out_template) + '.%(ext)s',
            '--progress-template', PROGRESS_TEMPLATE,
            '--print', 'after_move:' + FILE_PREFIX + '%(filepath)s']

    ffmpeg_dir = find_ffmpeg()
    if ffmpeg_dir:
        args += ['--ffmpeg-location', ffmpeg_dir]

    if audio_only:
        # 音频模式：抽成 mp3 并写 ID3（videdown 用 FFmpegMetadata 带 -write_id3v1 兼容老播放器）
        args += ['-f', 'ba/b', '-x', '--audio-format', 'mp3', '--audio-quality', '0',
                 '--postprocessor-args', 'FFmpegMetadata:-write_id3v1 1']
    else:
        args += ['-f', selector, '--merge-output-format', 'mp4']

    if cookies_file:
        args += ['--cookies', str(cookies_file)]
    elif browser:
        args += ['--cookies-from-browser', str(browser)]

    node = find_node()
    if node and ('youtube.com' in url or 'youtu.be' in url):
        args += ['--js-runtimes', 'node:' + node]

    args += [url]
    return args


def probe_args(url, browser='', cookies_file=''):
    ytdlp = find_ytdlp() or 'yt-dlp'
    args = [ytdlp, '--no-warnings', '--no-color', '--no-playlist',
            '--dump-single-json', '--no-simulate']
    if cookies_file:
        args += ['--cookies', str(cookies_file)]
    elif browser:
        args += ['--cookies-from-browser', str(browser)]
    node = find_node()
    if node and ('youtube.com' in url or 'youtu.be' in url):
        args += ['--js-runtimes', 'node:' + node]
    args += [url]
    return args


# ---------------------------------------------------------------- 解析进度
def parse_progress(line):
    """把 --progress-template 的行拆成字典；不相干的行返回 None

    yt-dlp 打的格式（我们自己的前缀）：
        PETDL_PROG|<已下字节>|<总字节>|<估算总字节>|<速度>|<剩余秒>|<状态>
        PETDL_FILE|<合并后的文件路径>
    """
    s = _ANSI_RE.sub('', str(line or '')).strip()
    if s.startswith(PROGRESS_PREFIX):
        parts = s[len(PROGRESS_PREFIX):].split('|')
        if len(parts) < 6:
            return None

        def num(x):
            try:
                v = float(x)
                return v if v == v else 0.0     # NaN 也当 0
            except Exception:
                return 0.0

        got, total, est = num(parts[0]), num(parts[1]), num(parts[2])
        return {'downloaded': got, 'total': total or est,
                'speed': num(parts[3]), 'eta': num(parts[4]),
                'status': parts[5].strip()}
    if s.startswith(FILE_PREFIX):
        return {'file': s[len(FILE_PREFIX):].strip()}
    return None


# 浏览器 cookie 读不出来的几种典型报错（Chrome/Edge 开着时会锁 cookie 库，新版还有 App-Bound 加密）
_COOKIE_ERROR_KEYS = (
    'could not copy',
    'failed to decrypt',
    'could not find firefox',
    'could not find chrome',
    'could not find edge',
    'app-bound',
    'dpapi',
)


def is_cookie_error(text):
    """这次失败是不是「cookie 读不出来」造成的（能救：去掉 cookie 重试一次）"""
    low = str(text or '').lower()
    if any(k in low for k in ('app-bound', 'dpapi')):
        return True
    if 'cookie' not in low:
        return False
    return any(k in low for k in _COOKIE_ERROR_KEYS)


_COOKIE_HINT = ('读不到浏览器 cookie（Chrome/Edge 开着时会锁住 cookie 库）：'
                '关掉浏览器再试；这次先不带 cookie 下载')


_ERROR_HINTS = (
    ('sign in to confirm', '这个站要求登录：面板上「浏览器 cookie」选一下 Edge/Chrome 再试'),
    ('this video is unavailable', '视频已删除或地区受限'),
    ('private video', '私有视频，得登录（选浏览器 cookie）'),
    ('unsupported url', '这个链接 yt-dlp 不认识（可能是首页、播放列表页或需要登录的播放页）'),
    ('requires payment', '会员内容，需要账号有权限的 cookie'),
    ('http error 403', '链接被防盗链拒了（B站类站点建议带浏览器 cookie）'),
    ('http error 412', '站点判定为爬虫（B站 412）：带上浏览器 cookie 再试'),
    ('no space left', '磁盘空间不够'),
)


def explain_error(text):
    """把 yt-dlp 的英文报错翻成一句能照做的中文（认不出来就原样截断）"""
    raw = _ANSI_RE.sub('', str(text or '')).strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    err = ''
    for ln in reversed(lines):
        if ln.upper().startswith('ERROR'):
            err = ln[6:].strip() if ln.upper().startswith('ERROR:') else ln
            break
    if not err:
        err = lines[-1] if lines else ''
    low = err.lower()
    for key, hint in _ERROR_HINTS:
        if key in low:
            return '%s（%s）' % (hint, err[:120])
    return err[:200] or '未知错误'


# ---------------------------------------------------------------- 探测
def probe(url, browser='', cookies_file='', timeout=60):
    """解析链接元数据 → {ok, title, uploader, duration, site, heights…}（不下载）

    失败时带一句人话错误，界面直接显示，不用用户看堆栈。
    cookie 读不出来（浏览器开着）会自动去掉 cookie 再解析一次 —— 大部分站不带 cookie 也能解析。
    """
    if not url:
        return {'ok': False, 'error': '先贴个链接'}
    if not find_ytdlp():
        return {'ok': False, 'error': '没找到 yt-dlp'}
    info = _probe_once(url, browser, cookies_file, timeout)
    if not info.get('ok') and (browser or cookies_file) and is_cookie_error(info.get('error')):
        retry = _probe_once(url, '', '', timeout)
        if retry.get('ok'):
            retry['notice'] = _COOKIE_HINT
            return retry
        info['error'] = _COOKIE_HINT
    return info


def _probe_once(url, browser='', cookies_file='', timeout=60):
    try:
        proc = subprocess.run(probe_args(url, browser, cookies_file),
                              capture_output=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': '解析超时（%d 秒）：这个站可能要验证，或者网络卡了' % timeout}
    except Exception as exc:
        return {'ok': False, 'error': '解析失败：%s' % exc}

    out = (proc.stdout or b'').decode('utf-8', 'replace').strip()
    err = (proc.stderr or b'').decode('utf-8', 'replace')
    if proc.returncode != 0 or not out:
        return {'ok': False, 'error': explain_error(err) or '解析失败（退出码 %s）' % proc.returncode}

    info = None
    for blob in (out, out.splitlines()[-1] if out.splitlines() else ''):
        try:
            info = json.loads(blob)
            break
        except Exception:
            continue
    if not isinstance(info, dict):
        return {'ok': False, 'error': '解析失败：返回的不是 JSON'}
    if info.get('_type') == 'playlist':
        entries = [e for e in (info.get('entries') or []) if isinstance(e, dict)]
        if not entries:
            return {'ok': False, 'error': '这是一张播放列表，里面没有可下的条目'}
        info = entries[0]

    heights = set()
    for f in (info.get('formats') or []):
        try:
            h = int(f.get('height') or 0)
        except Exception:
            h = 0
        if h > 0:
            heights.add(h)
    return {
        'ok': True,
        'url': url,
        'title': str(info.get('title') or '').strip(),
        'uploader': str(info.get('uploader') or info.get('channel') or info.get('creator') or '').strip(),
        'duration': float(info.get('duration') or 0),
        'site': str(info.get('extractor_key') or info.get('extractor') or '').strip(),
        'thumbnail': str(info.get('thumbnail') or ''),
        'heights': sorted(heights, reverse=True)[:6],
        'is_live': bool(info.get('is_live')),
        'format_count': len(info.get('formats') or []),
    }


# ---------------------------------------------------------------- 下载任务
class DownloadJob(object):
    """一次下载：跑 yt-dlp 子进程，进度回吐给界面

    暂停/继续照 videdown 的做法：**杀进程 + 用同样参数重跑**。yt-dlp 会把半截文件留在 .part，
    重跑时自己续传（原生能力，不用我们拼 HTTP Range）。
    """

    def __init__(self, url, outdir=None, quality='best', browser='', cookies_file='',
                 fmt_id='', title='', on_event=None):
        self.url = url
        self.outdir = outdir or default_outdir()
        self.quality = quality
        self.browser = browser
        self.cookies_file = cookies_file
        self.fmt_id = fmt_id
        self.title = title or url
        self.state = 'waiting'          # waiting/running/paused/merging/done/error/canceled
        self.percent = 0.0
        self.speed = 0.0
        self.eta = 0.0
        self.got = 0.0
        self.total = 0.0
        self.file = ''
        self.error = ''
        self.started_at = time.time()
        self.on_event = on_event or (lambda job, kind, data: None)
        self._proc = None
        self._thread = None
        self._cancel = False
        self._pause = False
        self._resume = threading.Event()
        self._last_error = ''
        self._cookie_retry = False

    # ---------- 对外 ----------
    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name='pet-download', daemon=True)
        self._thread.start()
        return self

    def pause(self):
        if self.state not in ('running', 'merging', 'waiting'):
            return False
        self._pause = True
        self._kill()
        return True

    def resume(self):
        if self.state != 'paused':
            return False
        self._pause = False
        self._resume.set()
        return True

    def cancel(self):
        self._cancel = True
        self._pause = False
        self._resume.set()
        self._kill()
        return True

    def active(self):
        return self.state in ('waiting', 'running', 'merging', 'paused')

    def summary(self):
        """一行摘要（任务列表用）"""
        if self.state == 'done':
            return '✔ 已完成 · %s' % os.path.basename(self.file or self.title)
        if self.state == 'paused':
            return '⏸ 已暂停 · %d%% · %s' % (int(self.percent), self.title)
        if self.state == 'error':
            return '✕ 失败 · %s' % self.error[:60]
        if self.state == 'canceled':
            return '✕ 已取消 · %s' % self.title
        if self.state == 'merging':
            return '🔧 合并中 · %s' % self.title
        return '⬇ %d%% · %s' % (int(self.percent), self.title)

    # ---------- 内部 ----------
    def _emit(self, kind, data=None):
        try:
            self.on_event(self, kind, data or {})
        except Exception:
            pass

    def _set_state(self, state):
        self.state = state
        self._emit('state', {'state': state})

    def _kill(self):
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def _handle_line(self, line):
        info = parse_progress(line)
        if info is not None:
            if 'file' in info:
                self.file = info['file']
                return
            self.got, self.total = info['downloaded'], info['total']
            self.speed, self.eta = info['speed'], info['eta']
            if self.total > 0:
                pct = min(100.0, self.got * 100.0 / self.total)
                if pct > self.percent:      # 多流（视频流→音频流）时别让进度条往回跳
                    self.percent = pct
            self._emit('progress', {'percent': round(self.percent, 1), 'downloaded': self.got,
                                    'total': self.total, 'speed': self.speed,
                                    'eta': self.eta, 'status': info['status']})
            return
        low = line.lower()
        if '[merger]' in low or '[extractaudio]' in low or '[videoconvert]' in low:
            if self.state != 'merging':
                self._set_state('merging')
        elif low.startswith('error'):
            self._last_error = line

    def _run(self):
        if not os.path.isdir(self.outdir):
            try:
                os.makedirs(self.outdir, exist_ok=True)
            except Exception as exc:
                self.error = '建不了下载目录：%s' % exc
                self._set_state('error')
                self._emit('error', {'error': self.error})
                return

        args = build_args(self.url, self.outdir, self.quality, self.browser,
                          self.cookies_file, self.fmt_id)
        while True:
            if self._cancel:
                self._set_state('canceled')
                return
            self._set_state('running')
            try:
                self._proc = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    encoding='utf-8', errors='replace', bufsize=1)
            except Exception as exc:
                self.error = '启动 yt-dlp 失败：%s' % exc
                self._set_state('error')
                self._emit('error', {'error': self.error})
                return

            proc = self._proc
            try:
                for line in proc.stdout:
                    if self._cancel or self._pause:
                        break
                    if line.strip():
                        self._handle_line(line.rstrip())
            except Exception:
                pass

            if self._cancel:
                self._kill()
                self._set_state('canceled')
                return
            if self._pause:
                self._kill()
                self._set_state('paused')
                self._resume.wait()
                self._resume.clear()
                if self._cancel:
                    self._set_state('canceled')
                    return
                continue        # 同参数重跑 → .part 续传

            try:
                code = proc.wait(timeout=30)
            except Exception:
                code = -1

            if code == 0:
                self.error = ''
                self.percent = 100.0
                self._emit('progress', {'percent': 100.0, 'downloaded': self.total,
                                        'total': self.total, 'speed': 0.0, 'eta': 0.0,
                                        'status': 'finished'})
                self._set_state('done')
                self._emit('done', {'file': self.file,
                                    'title': self.title or os.path.basename(self.file)})
                return

            self.error = explain_error(self._last_error) or ('yt-dlp 退出码 %s' % code)
            # cookie 读不出来（浏览器开着锁了库）→ 去掉 cookie 再跑一次，别让整件事失败
            if not self._cookie_retry and (self.browser or self.cookies_file) \
                    and is_cookie_error(self._last_error or self.error):
                self._cookie_retry = True
                self.browser, self.cookies_file = '', ''
                args = build_args(self.url, self.outdir, self.quality, '', '', self.fmt_id)
                self._emit('notice', {'text': _COOKIE_HINT})
                continue
            self._set_state('error')
            self._emit('error', {'error': self.error})
            return


def open_folder(path):
    """在资源管理器里打开（文件就选中它，目录就进去）"""
    try:
        path = path or default_outdir()
        if os.path.isfile(path):
            if os.name == 'nt':
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
                return True
            path = os.path.dirname(path)
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        if os.name == 'nt':
            os.startfile(path)      # noqa: S606 — 打开本地目录，参数来自我们自己
            return True
        subprocess.Popen(['xdg-open', path])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 给界面用的格式化
def fmt_size(num):
    try:
        num = float(num or 0)
    except Exception:
        return '—'
    if num <= 0:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num < 1024 or unit == 'GB':
            return ('%.1f %s' % (num, unit)) if unit != 'B' else ('%d B' % num)
        num /= 1024.0
    return '—'


def fmt_speed(num):
    try:
        num = float(num or 0)
    except Exception:
        return ''
    if num <= 0:
        return ''
    return fmt_size(num) + '/s'


def fmt_eta(sec):
    try:
        sec = int(float(sec or 0))
    except Exception:
        return ''
    if sec <= 0:
        return ''
    if sec < 60:
        return '%d 秒' % sec
    if sec < 3600:
        return '%d 分 %02d 秒' % (sec // 60, sec % 60)
    return '%d 小时 %02d 分' % (sec // 3600, (sec % 3600) // 60)


def fmt_duration(sec):
    try:
        sec = int(float(sec or 0))
    except Exception:
        return ''
    if sec <= 0:
        return ''
    if sec < 3600:
        return '%d:%02d' % (sec // 60, sec % 60)
    return '%d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)
