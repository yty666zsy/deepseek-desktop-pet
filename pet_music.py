# -*- coding: utf-8 -*-
"""pet_music.py — 本地音乐播放内核（v6.79）

为什么这么做：不去 pip 装音频库，直接驱动 mpv.exe 的 JSON IPC（命名管道）。
好处是 mp3/flac/m4a/ogg/opus/wav 全放得动，将来接 MusicFree 插件的网络流
（mp3 / m3u8）也不用改内核——mpv 本身就能拉流。

对外三样东西：
    MusicPlayer  播放器（播放列表 / 播放暂停 / 上下首 / 进度 / 音量 / 歌词）
    read_track   读一个音频文件的标题、歌手、时长、封面（mutagen，已在依赖里）
    parse_lrc    解析 .lrc 歌词

mpv 路径优先级：config.json 的 music_mpv_path → PATH → 常见安装目录
"""
import json
import os
import random
import re
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

# --------------------------------------------------------------------------
# 找 mpv
# --------------------------------------------------------------------------
_CANDIDATE_DIRS = [
    r'C:\Program Files\MPV Player',
    r'C:\Program Files\mpv',
    r'C:\Program Files (x86)\MPV Player',
    os.path.expandvars(r'%LOCALAPPDATA%\Programs\mpv'),
    os.path.expandvars(r'%LOCALAPPDATA%\Microsoft\WinGet\Links'),
]

AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.aac', '.wav', '.ogg', '.opus', '.wma', '.ape', '.webm')


def find_mpv():
    """返回 mpv.exe 路径，找不到返回 ''"""
    try:
        import pet_storage  # noqa: F401  （保持一致的项目内依赖，失败也不影响）
    except Exception:
        pass
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
    try:
        with open(cfg_path, encoding='utf-8') as f:
            p = (json.load(f) or {}).get('music_mpv_path') or ''
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    for d in os.environ.get('PATH', '').split(os.pathsep):
        p = os.path.join(d, 'mpv.exe')
        if os.path.isfile(p):
            return p
    for d in _CANDIDATE_DIRS:
        p = os.path.join(d, 'mpv.exe')
        if os.path.isfile(p):
            return p
    return ''


def is_audio(path):
    return os.path.splitext(str(path))[1].lower() in AUDIO_EXTS


# --------------------------------------------------------------------------
# 曲目信息 / 歌词
# --------------------------------------------------------------------------
class Track(object):
    """一首歌。path 可以是本地文件，也可以是 http(s) 直链（MusicFree 插件给的音源就是直链）"""

    __slots__ = ('path', 'title', 'artist', 'album', 'duration', 'cover', 'lyrics',
                 'headers', 'plugin', 'extra')

    def __init__(self, path, title='', artist='', album='', duration=0.0, cover=None, lyrics=None,
                 headers=None, plugin='', extra=None):
        self.path = path
        self.title = title or os.path.splitext(os.path.basename(str(path)))[0]
        self.artist = artist
        self.album = album
        self.duration = duration
        self.cover = cover          # QPixmap 或 None
        self.lyrics = lyrics or []  # [(秒, 文本)]
        self.headers = headers or None   # 拉流需要的请求头（bilibili 要 Referer，否则 403）
        self.plugin = plugin             # 来源插件名（回查歌词时要知道问哪个插件）
        self.extra = extra               # 插件原始 item（getLyric/getMediaSource 要原样回传）

    def lyric_at(self, pos):
        """当前时间该显示哪句词，没有就返回 ''"""
        if not self.lyrics:
            return ''
        cur = ''
        for t, text in self.lyrics:
            if t <= pos:
                cur = text
            else:
                break
        return cur

    def label(self):
        if self.artist:
            return '%s - %s' % (self.title, self.artist)
        return self.title

    def __repr__(self):
        return '<Track %s>' % self.label()


_TAG_KEYS = {
    'title': ('title', 'TIT2', '\xa9nam'),
    'artist': ('artist', 'TPE1', '\xa9ART'),
    'album': ('album', 'TALB', '\xa9alb'),
}


def _first_tag(tags, names):
    for n in names:
        v = tags.get(n)
        if not v:
            continue
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ''
        s = str(v).strip()
        if s:
            return s
    return ''


def _cover_from_tags(path, audio):
    """从常见格式里抠出内嵌封面，返回 bytes 或 None"""
    if not audio:
        return None
    try:
        if hasattr(audio, 'pictures') and audio.pictures:      # FLAC / OGG
            return bytes(audio.pictures[0].data) or None
    except Exception:
        pass
    try:
        tags = audio.tags or {}
        for key in list(tags.keys()):
            item = tags.get(key)
            if hasattr(item, 'data') and item.__class__.__name__.lower().startswith('apic'):  # ID3
                return bytes(item.data) or None
    except Exception:
        pass
    try:
        covr = (audio.tags or {}).get('covr')                  # MP4 / M4A
        if covr:
            return bytes(covr[0])
    except Exception:
        pass
    return None


def read_track(path):
    """读一个音频文件的信息；读不到就用文件名兜底（不抛异常）"""
    t = Track(path)
    if not str(path).startswith(('http://', 'https://')):
        try:
            import mutagen
            audio = mutagen.File(path)
        except Exception:
            audio = None
        if audio is not None:
            try:
                tags = audio.tags or {}
                t.title = _first_tag(tags, _TAG_KEYS['title']) or t.title
                t.artist = _first_tag(tags, _TAG_KEYS['artist'])
                t.album = _first_tag(tags, _TAG_KEYS['album'])
            except Exception:
                pass
            try:
                t.duration = float(getattr(audio.info, 'length', 0) or 0)
            except Exception:
                pass
            data = _cover_from_tags(path, audio)
            if data:
                try:
                    from PySide6.QtGui import QPixmap
                    pm = QPixmap()
                    if pm.loadFromData(data):
                        t.cover = pm
                except Exception:
                    pass
        lrc = os.path.splitext(path)[0] + '.lrc'
        if os.path.isfile(lrc):
            try:
                with open(lrc, encoding='utf-8', errors='ignore') as f:
                    t.lyrics = parse_lrc(f.read())
            except Exception:
                pass
    return t


_LRC_TIME = re.compile(r'\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]')


def _stamp_sec(m):
    """一个时间戳 → 秒"""
    mm, ss, frac = m.group(1), m.group(2), m.group(3) or '0'
    try:
        return round(int(mm) * 60 + int(ss) + int(frac) / (10 ** len(frac)), 3)
    except Exception:
        return None


def parse_lrc(text):
    """解析 LRC 文本 → [(秒, 词)]，按时间排序。

    要同时认两种真实世界里都存在的写法：
      A) [00:01.00][00:05.00] 同一句词  → 这句词在这两个时间点各出现一次
      B) [00:01.00]第一句[00:05.00]第二句  → 一整行塞了整首歌（歌词站常见，还常夹大量空格填充）
    判据：如果“相邻时间戳之间还有非空文本”，那就是 B，按段拆开；否则按 A 重复。
    不做这个判断的话，B 格式会把整首歌的文本都安到第一个时间点上（实测踩到过）。
    """
    out = []
    for line in str(text or '').splitlines():
        stamps = list(_LRC_TIME.finditer(line))
        if not stamps:
            continue
        segs = []
        for i, m in enumerate(stamps):
            start = m.end()
            end = stamps[i + 1].start() if i + 1 < len(stamps) else len(line)
            segs.append(re.sub(r'\s+', ' ', line[start:end]).strip())
        interleaved = any(segs[:-1])
        if interleaved:
            for m, body in zip(stamps, segs):
                sec = _stamp_sec(m)
                if body and sec is not None:
                    out.append((sec, body))
        else:
            body = segs[-1] if segs else ''
            if not body:
                continue
            for m in stamps:
                sec = _stamp_sec(m)
                if sec is not None:
                    out.append((sec, body))
    out.sort(key=lambda x: x[0])
    return out


# --------------------------------------------------------------------------
# mpv IPC
# --------------------------------------------------------------------------
class MpvError(RuntimeError):
    pass


class MpvIpc(object):
    """mpv.exe 的 JSON IPC 客户端（Windows 命名管道）"""

    def __init__(self, exe=None, on_event=None):
        self.exe = exe or find_mpv()
        self.on_event = on_event
        self._proc = None
        self._pipe = None
        self._handle = None
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._replies = {}
        self._rid = 0
        self._dead = True
        self._pipe_name = 'pet_music_%d_%d' % (os.getpid(), random.randint(1000, 9999))

    # ---- 生命周期 ----
    def _log_path(self):
        """mpv 自己的日志（网络流为什么断、跳到哪儿、解码报什么错，都在这儿）"""
        logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        try:
            os.makedirs(logs, exist_ok=True)
            path = os.path.join(logs, 'mpv.log')
            # 只留最近一次启动之后的，别让它无限长大
            if os.path.isfile(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                os.replace(path, path + '.old')
            return path
        except Exception:
            return 'NUL'

    def start(self, timeout=6.0):
        if not self.exe or not os.path.isfile(self.exe):
            raise MpvError('找不到 mpv.exe（先装了 shinchiro.mpv 或在 config.json 里配 music_mpv_path）')
        if not self._dead:
            return self
        args = [
            self.exe,
            '--idle=yes',
            '--no-video',
            '--no-terminal',
            # 原来这里是 --really-quiet，把一切信息都吞了 —— 网络流半路断掉时（音源只给试听片段、
            # 直链过期、防盗链限流）根本无从查起。改成只压掉啰嗦的 info，其余落一份日志备查。
            '--msg-level=all=error,cplayer=info,ffmpeg=error',
            '--log-file=' + self._log_path(),
            '--volume=70',
            '--keep-open=no',
            '--input-ipc-server=\\\\.\\pipe\\' + self._pipe_name,
        ]
        flags = 0
        if sys.platform == 'win32':
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      stdin=subprocess.DEVNULL, creationflags=flags)
        pipe_path = r'\\.\pipe\\' + self._pipe_name
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                self._pipe = open(pipe_path, 'r+b', buffering=0)
                break
            except Exception as e:      # 管道还没建好
                last = e
                time.sleep(0.08)
        if self._pipe is None:
            self._kill()
            raise MpvError('连不上 mpv 的 IPC 管道：%s' % last)
        self._dead = False
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _kill(self):
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass
        self._proc = None

    def close(self):
        self._dead = True
        try:
            if self._pipe:
                self._pipe.close()
        except Exception:
            pass
        self._pipe = None
        self._kill()

    @property
    def alive(self):
        return (not self._dead) and self._proc is not None and self._proc.poll() is None

    # ---- 收发 ----
    def _available(self):
        """管道里现在有多少字节可读（0 = 暂时没数据）。

        Windows 的管道是同步句柄：一个线程阻塞在 ReadFile 上会把写操作也卡住，
        所以这里用 PeekNamedPipe 轮询，只在有数据时才读，读写互不阻塞。
        """
        if sys.platform == 'win32':
            try:
                import ctypes
                from ctypes import wintypes
                if self._handle is None:
                    import msvcrt
                    self._handle = msvcrt.get_osfhandle(self._pipe.fileno())
                avail = wintypes.DWORD(0)
                ok = ctypes.windll.kernel32.PeekNamedPipe(
                    wintypes.HANDLE(self._handle), None, 0, None,
                    ctypes.byref(avail), None)
                return int(avail.value) if ok else 0
            except Exception:
                return 0
        import select
        try:
            r, _, _ = select.select([self._pipe], [], [], 0.05)
            return 4096 if r else 0
        except Exception:
            return 0

    def _reader(self):
        buf = b''
        while not self._dead:
            n = self._available()
            if n <= 0:
                time.sleep(0.02)
                if not self.alive:
                    break
                continue
            try:
                chunk = self._pipe.read(n)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode('utf-8', 'ignore'))
                except Exception:
                    continue
                rid = msg.get('request_id')
                if rid is not None:
                    ev = self._replies.pop(rid, None)
                    if ev is not None:
                        ev['msg'] = msg
                        ev['event'].set()
                elif 'event' in msg and self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception:
                        pass
        self._dead = True

    def command(self, *args, **kwargs):
        """发一条命令，返回 data 字段（异步命令返回 None）"""
        timeout = kwargs.pop('timeout', 3.0)
        if not self.alive:
            raise MpvError('mpv 没在跑')
        with self._lock:
            self._rid += 1
            rid = self._rid
            ev = {'event': threading.Event(), 'msg': None}
            self._replies[rid] = ev
            payload = json.dumps({'command': list(args), 'request_id': rid}).encode('utf-8') + b'\n'
            try:
                with self._wlock:
                    self._pipe.write(payload)
            except Exception as e:
                self._replies.pop(rid, None)
                raise MpvError('写管道失败：%s' % e)
        if not ev['event'].wait(timeout):
            self._replies.pop(rid, None)
            raise MpvError('mpv 没回话：%s' % (args,))
        msg = ev['msg'] or {}
        if msg.get('error') not in (None, 'success'):
            raise MpvError(str(msg.get('error')))
        return msg.get('data')

    def get(self, prop, default=None):
        try:
            return self.command('get_property', prop)
        except Exception:
            return default

    def set(self, prop, value):
        try:
            return self.command('set_property', prop, value)
        except Exception:
            return None


# --------------------------------------------------------------------------
# 播放器
# --------------------------------------------------------------------------
class MusicPlayer(QObject):
    """播放列表 + 播放控制的对外门面（GUI 线程里用；IPC 读写自带锁）"""

    trackChanged = Signal(object)        # Track
    positionChanged = Signal(float, float)   # 当前秒, 总秒
    playingChanged = Signal(bool)
    playlistChanged = Signal()
    failed = Signal(str)                 # 打不开/放不了
    interrupted = Signal(str)            # 放到一半被掐断（音源断流、直链过期、只给试听片段）
    _mpvEvent = Signal(object)           # mpv 事件跨线程转发（读线程 emit → GUI 线程处理）

    def __init__(self, parent=None, exe=None):
        super(MusicPlayer, self).__init__(parent)
        self.tracks = []
        self.index = -1
        self._ipc = None
        self._exe = exe
        self._playing = False
        self._volume = 70
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._mpvEvent.connect(self._handle_mpv_event)

    # ---- 内部 ----
    def _ensure_ipc(self):
        if self._ipc is not None and self._ipc.alive:
            return self._ipc
        self._ipc = MpvIpc(exe=self._exe, on_event=self._on_mpv_event).start()
        self._ipc.set('volume', self._volume)
        return self._ipc

    def _on_mpv_event(self, msg):
        """读线程里调用——只做转发，Qt 控件操作必须回 GUI 线程"""
        try:
            self._mpvEvent.emit(msg)
        except Exception:
            pass

    def _handle_mpv_event(self, msg):
        """GUI 线程里处理 mpv 事件"""
        ev = msg.get('event')
        if ev == 'end-file':
            reason = (msg.get('reason') or '')
            # 一首放完了自动下一首；手动 loadfile 造成的 'stop' 不算
            if reason in ('eof', 'error', 'quit'):
                self._auto_next()
        elif ev == 'file-loaded':
            self._set_playing(True)

    def _auto_next(self):
        cb = getattr(self, 'auto_next_callback', None)
        if callable(cb):
            try:
                cb()
                return
            except Exception:
                pass
        if not self.tracks:
            return
        if self.index + 1 < len(self.tracks):
            self.play_index(self.index + 1)
        else:
            self._set_playing(False)

    def _set_playing(self, v):
        if v != self._playing:
            self._playing = v
            self.playingChanged.emit(v)
            if v:
                self._timer.start()
            else:
                self._timer.stop()

    def _tick(self):
        if not self._ipc or not self._ipc.alive:
            return
        pos = self._ipc.get('time-pos') or 0.0
        dur = self._ipc.get('duration') or (self.current.duration if self.current else 0.0)
        try:
            self.positionChanged.emit(float(pos or 0), float(dur or 0))
        except Exception:
            pass

    # ---- 属性 ----
    @property
    def current(self):
        if 0 <= self.index < len(self.tracks):
            return self.tracks[self.index]
        return None

    @property
    def playing(self):
        return self._playing

    # ---- 对外操作 ----
    def add(self, paths, play_now=False):
        added = []
        for p in paths:
            p = str(p)
            if os.path.isfile(p) and is_audio(p):
                self.tracks.append(read_track(p))
                added.append(p)
        if added:
            self.playlistChanged.emit()
            if play_now or self.index < 0:
                self.play_index(self.index if self.index >= 0 else 0)
        return len(added)

    def set_playlist(self, paths):
        self.tracks = []
        self.index = -1
        self.add(paths, play_now=True)

    # ---- v6.82：接 MusicFree 插件音源（网络直链 + 请求头） ----
    def _apply_headers(self, ipc, tr):
        """把 Track.headers 交给 mpv（bilibili 之类不带 Referer 会 403）；没有则清空，别串到下一首"""
        fields = []
        for k, v in (getattr(tr, 'headers', None) or {}).items():
            if v is None or str(v).strip() == '':
                continue
            fields.append('%s: %s' % (k, v))
        try:
            ipc.set('http-header-fields', fields)
        except Exception:
            pass

    def add_tracks(self, tracks, play_now=False):
        """直接塞 Track 对象（本地文件/网络直链都行）"""
        tracks = [t for t in (tracks or []) if t is not None]
        if not tracks:
            return 0
        first = len(self.tracks)
        self.tracks.extend(tracks)
        self.playlistChanged.emit()
        if play_now:
            self.play_index(first)
        elif self.index < 0:
            self.play_index(0)
        return len(tracks)

    def set_tracks(self, tracks, start=0):
        """整表替换并从 start 开始播（搜索结果双击就用它）"""
        self.tracks = list(tracks or [])
        self.index = -1
        self.playlistChanged.emit()
        if self.tracks:
            self.play_index(start)

    def play_index(self, i):
        if not self.tracks:
            return
        i = max(0, min(i, len(self.tracks) - 1))
        self.index = i
        tr = self.tracks[i]
        try:
            ipc = self._ensure_ipc()
            self._apply_headers(ipc, tr)
            ipc.command('loadfile', tr.path, 'replace')
            ipc.set('pause', False)
        except Exception as e:
            self.failed.emit(str(e))
            self._set_playing(False)
            return
        self._set_playing(True)
        self._timer.start()
        self.trackChanged.emit(tr)
        if tr.duration <= 0:
            QTimer.singleShot(600, self._fill_duration)

    def _fill_duration(self):
        if not self._ipc:
            return
        dur = self._ipc.get('duration')
        if dur and self.current and self.current.duration <= 0:
            self.current.duration = float(dur)

    def toggle(self):
        if not self.tracks:
            return
        if self.index < 0:
            self.play_index(0)
            return
        try:
            self._ensure_ipc()
        except Exception as e:
            self.failed.emit(str(e))
            return
        paused = bool(self._ipc.get('pause', False))
        self._ipc.set('pause', not paused)
        self._set_playing(paused)

    def pause(self):
        if self._ipc:
            self._ipc.set('pause', True)
        self._set_playing(False)

    def stop(self):
        if self._ipc:
            try:
                self._ipc.command('stop')
            except Exception:
                pass
        self._set_playing(False)
        self._timer.stop()

    def next(self):
        if self.index + 1 < len(self.tracks):
            self.play_index(self.index + 1)

    def prev(self):
        # 播过 3 秒就回到开头，否则上一首
        pos = 0.0
        if self._ipc:
            pos = self._ipc.get('time-pos') or 0.0
        if pos and float(pos) > 3.0:
            self.seek(0)
        elif self.index > 0:
            self.play_index(self.index - 1)
        else:
            self.seek(0)

    def seek(self, sec):
        if self._ipc:
            try:
                self._ipc.command('seek', max(0.0, float(sec)), 'absolute')
            except Exception:
                pass

    def set_volume(self, v):
        self._volume = max(0, min(100, int(v)))
        if self._ipc:
            self._ipc.set('volume', self._volume)

    @property
    def volume(self):
        return self._volume

    def shutdown(self):
        self._timer.stop()
        if self._ipc:
            self._ipc.close()
            self._ipc = None
