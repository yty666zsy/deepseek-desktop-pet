# -*- coding: utf-8 -*-
"""pet_music_lyrics.py — 歌词抓取（v6.82）

三级兜底，谁先有货用谁：
    1) 本地已有：文件内嵌歌词标签 / 同名 .lrc（read_track 已经读了，这里只管网络那部分）
    2) LRCLIB（lrclib.net，开放歌词库，无需注册）—— 按 歌名+歌手+时长 精确匹配，质量最高
    3) MusicFree 歌词插件（歌词网 / 歌词千寻）—— 用插件按关键词搜，挑最像的一条

为什么要清洗标题：插件给的标题多是「【Hi-Res无损】｜《晴天》- 周杰伦 -'故事的小黄花'」这种，
直接丢给歌词库匹配率极低，所以先抠出书名号里的歌名 + 歌手名。

线程：网络请求全部在后台线程做（LyricFetcher.done 信号回 GUI 线程），绝不卡住宠物。
"""
import difflib
import json
import re
import threading
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, Signal

from pet_music import parse_lrc

LRCLIB_API = 'https://lrclib.net/api'
UA = 'desktop-pet/6.82 (https://github.com/maotoumao/MusicFree plugin bridge user)'

_BRACKETS = re.compile(r'[【\[（(][^】\]）)]*[】\]）)]')
_QUOTED = re.compile(r'《([^》]+)》')
_SEP = re.compile(r'\s*[-–—|｜/]\s*')
# 常见的“画质/版本”噪声词：不去掉的话「晴天MV 2160P修复版」这种永远匹配不上歌词库
# 注意：不能用 \b —— 中文也算“词字符”，「晴天MV」里 M 前面没有边界，\bmv\b 匹配不上（实测踩过）
_NOISE = re.compile(
    r'(?i)(?:(?<![A-Za-z0-9])(?:4k|8k|1080p|2160p|720p|480p|mv|m/v|official|hi-?res|hq|hd|'
    r'full|ver\.?|version|lyric?s?|live|remaster(?:ed)?|audio)(?![A-Za-z0-9])'
    r'|修复版|高清|超清|无损|完整版|纯享|歌词版|官方|现场版|重制版|珍藏版|字幕版|高音质)')


def strip_noise(text):
    """去掉「4K / MV / 修复版 / 无损」这类噪声词，剩下的才是歌名本体"""
    s = _NOISE.sub(' ', str(text or ''))
    s = re.sub(r'\s+', ' ', s).strip(' -–—|｜/·。,.')
    return s


def clean_title(raw):
    """从插件标题里抠出歌名：「【4K】《晴天》- 周杰伦」→（'晴天', '周杰伦'）"""
    text = str(raw or '').strip()
    if not text:
        return '', ''
    title, artist = '', ''
    m = _QUOTED.search(text)
    if m:
        title = m.group(1).strip()
        # 书名号后面的「- 歌手」最常见
        tail = text[m.end():]
        parts = [p for p in _SEP.split(tail.strip(' -–—|｜/')) if p.strip()]
        if parts:
            artist = parts[0].strip()
    text = _BRACKETS.sub(' ', text)
    if not title:
        parts = [p.strip() for p in _SEP.split(text) if p.strip()]
        if parts:
            title = parts[0]
            if len(parts) > 1 and not artist:
                artist = parts[1]
    title = re.sub(r'\s+', ' ', title).strip(' -–—|｜/《》')
    artist = re.sub(r'\s+', ' ', artist).strip(' -–—|｜/《》')
    title = strip_noise(title) or title
    artist = strip_noise(artist) or artist
    return title, artist


def _http_json(url, timeout=8.0):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def lrclib_lookup(title, artist, duration=0.0, album=''):
    """LRCLIB 精确查询 → rawLrc（没同步歌词就退回纯文本）

    时长常常是“视频长度”而不是“歌曲长度”（bilibili 上的 MV 就带片头），
    所以带时长查不到时会去掉时长再查一次 —— 不然明明有歌词也匹配不上。
    """
    if not title:
        return ''

    def _get(with_duration):
        params = {'track_name': title, 'artist_name': artist or ''}
        if album:
            params['album_name'] = album
        if with_duration and duration and duration > 0:
            params['duration'] = str(int(round(duration)))
        data = _http_json(LRCLIB_API + '/get?' + urllib.parse.urlencode(params))
        if isinstance(data, dict):
            lrc = data.get('syncedLyrics') or ''
            if not lrc and data.get('plainLyrics'):
                lrc = data['plainLyrics']       # 没时间戳也比没有强（会按整段显示）
            return lrc
        return ''

    for with_duration in (True, False):
        if not with_duration and not duration:
            break
        try:
            lrc = _get(with_duration)
            if lrc.strip():
                return lrc
        except Exception:
            continue
    return ''


def lrclib_search(title, artist='', duration=0.0):
    """LRCLIB 模糊搜索 → 挑最像的一条（歌名相似度 + 歌手命中 + 时长接近）"""
    if not title:
        return ''
    q = (title + ' ' + artist).strip()
    try:
        data = _http_json(LRCLIB_API + '/search?' + urllib.parse.urlencode({'q': q}))
    except Exception:
        return ''
    if not isinstance(data, list) or not data:
        return ''
    best, best_score = None, 0.0
    for item in data[:20]:
        if not isinstance(item, dict):
            continue
        name = str(item.get('trackName') or '')
        art = str(item.get('artistName') or '')
        score = difflib.SequenceMatcher(None, title.lower(), name.lower()).ratio()
        if artist and artist.lower() in art.lower():
            score += 0.25
        dur = item.get('duration') or 0
        if duration and dur:
            diff = abs(float(dur) - float(duration))
            if diff <= 3:
                score += 0.25
            elif diff <= 8:
                score += 0.1
        if score > best_score:
            best, best_score = item, score
    if best is None or best_score < 0.62:
        return ''
    return best.get('syncedLyrics') or best.get('plainLyrics') or ''


def fetch_via_plugin(host, plugin, item, title='', artist=''):
    """用 MusicFree 歌词插件取歌词：按「歌名 歌手」搜 lyric，挑最像的一条再取 rawLrc"""
    if host is None or not plugin:
        return ''
    query = ('%s %s' % (title, artist)).strip()
    if not query:
        return ''
    try:
        hits = host.search(plugin, query, kind='lyric') or []
    except Exception:
        return ''
    if not hits:
        return ''
    best, best_score = None, 0.0
    want = (title or '').lower()
    for h in hits[:15]:
        name = str(h.get('title') or '').lower()
        score = difflib.SequenceMatcher(None, want, name).ratio() if want else 0.0
        if artist and artist.lower() in str(h.get('artist') or '').lower():
            score += 0.2
        if score > best_score:
            best, best_score = h, score
    if best is None or best_score < 0.55:
        return ''
    try:
        raw, _trans = host.lyric(plugin, best)
        return raw or ''
    except Exception:
        return ''


LYRIC_PLUGINS = ('歌词网', '歌词千寻', 'geciwang', 'geciqianxun')


def find_lyric_plugin(host):
    """在已装插件里找能搜歌词的（歌词专用插件优先）

    返回**文件名**而不是 platform：同名音源可能装了好几套，只有文件名是唯一的。
    """
    if host is None:
        return ''
    try:
        plugins = host.list_plugins() or []
    except Exception:
        return ''
    fallback = ''
    for p in plugins:
        if p.get('error'):
            continue
        name = p.get('platform') or ''
        stats = p.get('supportedSearchType') or []
        if 'lyric' in stats or name in LYRIC_PLUGINS:
            key = p.get('file') or name
            if name in LYRIC_PLUGINS:
                return key
            fallback = fallback or key
    return fallback


def _name_candidates(title, artist=''):
    """列出要尝试的（歌名, 歌手）组合——插件标题格式太野，得多试几个"""
    raw = str(title or '').strip()
    artist = str(artist or '').strip()
    out = []
    t2, a2 = clean_title(raw)
    if t2 and a2:
        out.append((t2, artist or a2))
        out.append((a2, t2))         # 插件标题可能是「歌手 - 歌名」，反着也试一次
    elif t2:
        out.append((t2, artist))
    out.append((raw, artist))
    for base in (raw, t2):
        cleaned = strip_noise(base)
        if cleaned and cleaned != base:
            out.append((cleaned, artist or a2))     # 去掉「4K/MV/修复版」后再试一次
    t3, a3 = strip_noise(t2), strip_noise(a2)
    if t3 and a3 and (t3, a3) != (t2, a2):
        out.append((t3, a3))
        out.append((a3, t3))
    if artist and t2:
        out.append((artist, t2))
    seen, uniq = set(), []
    for t, a in out:
        if not t:
            continue
        key = (t.lower(), a.lower())
        if key in seen:
            continue
        seen.add(key)
        uniq.append((t, a))
    return uniq


def fetch_lyrics(host, title, artist='', duration=0.0, album='', plugin='', item=None):
    """总入口（阻塞，调用方放后台线程）→ rawLrc 文本，取不到返回 ''"""
    cands = _name_candidates(title, artist)
    if not cands:
        return ''

    # ① LRCLIB 精确 → ② LRCLIB 模糊，逐个候选试
    for t, a in cands:
        lrc = lrclib_lookup(t, a, duration, album)
        if lrc.strip():
            return lrc
    for t, a in cands:
        lrc = lrclib_search(t, a, duration)
        if lrc.strip():
            return lrc

    # ③ 歌词插件兜底
    lp = plugin if plugin and plugin in LYRIC_PLUGINS else find_lyric_plugin(host)
    if lp:
        for t, a in cands[:3]:
            lrc = fetch_via_plugin(host, lp, item, t, a)
            if lrc.strip():
                return lrc
    return ''


class LyricFetcher(QObject):
    """后台取歌词；done(标题, rawLrc) 回 GUI 线程"""

    done = Signal(str, str)

    def __init__(self, host=None, parent=None):
        super(LyricFetcher, self).__init__(parent)
        self.host = host

    def start(self, title, artist='', duration=0.0, album='', plugin='', item=None):
        def work():
            try:
                lrc = fetch_lyrics(self.host, title, artist, duration, album, plugin, item)
            except Exception:
                lrc = ''
            try:
                self.done.emit(str(title or ''), lrc or '')
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()
