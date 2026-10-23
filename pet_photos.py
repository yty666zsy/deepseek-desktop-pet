# -*- coding: utf-8 -*-
"""pet_photos.py — 「见面照片墙」内核（v6.88）

为什么自己做，不嵌现成的
------------------------
GitHub 上照片墙一抓一把，但**没有一个能 import 进 PySide6 桌宠**：
    · Justified-Gallery（1.7k★ MIT）/ PhotoSwipe（25k★ MIT）/ Maicius PhotoWall（MIT）/
      liaoyanqing666 Photo_Wall（Apache-2.0 恋爱照片墙模板）—— 全是 JS/CSS，要嵌就得开
      QtWebEngine；而 `tools/DeepSeekPet.spec` 为了把包体从 342MB 压到 ~210MB，已经把
      QtWebEngineCore / QtWebEngineWidgets / QtQml / QtQuick **全 excludes** 了 —— 路封死。
    · TagStudio（7.2k★）/ beeref（816★）—— GPL-3.0 传染 + 界面是 QML（同样被 excludes）。
    · PhotoPrism / Immich / Lychee —— AGPL + 要起服务端，为一张照片墙背一整套太重。
于是只借两样**算法/产品结构**，其余自己写：
    1. justified 行布局（Justified-Gallery 的核心，移植成下面的 justified_rows 纯函数）
    2. 「一次相册 = 一个故事」（simple-photo-gallery / Photo_Wall 的结构）→ 一次见面 = 一面墙
拍立得那张相纸是 Qt 里现画的（pet_photos_ui.PolaroidView），不往磁盘写合成图。

本模块只管数据与算法（无 Qt import，纯逻辑可单测）：
    · meetings.json 读写（原子写；文件坏了当空库，不炸也不静默丢数据）
    · 照片导入（一律**复制**进 photos/<日期>/；原图被移动/删除也不会裂图）
    · 缩略图缓存（photos/.thumbs；键含 路径+修改时间+尺寸，原图改了自动失效）
    · EXIF 拍摄日期（自动建议「按天分组 = 一次见面」）
    · justified 行布局纯函数
    · 见面统计 + 下次见面倒计时

约定
----
- 照片在 json 里存**相对 BASE_DIR 的路径**（`photos/2026-10-21/183012_a3f9c1.jpg`），
  整个项目目录搬到别处也不失效。
- 日期一律按字面算（EXIF 里没有时区），不跟 UTC 换算。
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
MEETINGS_PATH = os.path.join(BASE_DIR, 'meetings.json')
PHOTOS_DIR = os.path.join(BASE_DIR, 'photos')
THUMB_DIRNAME = '.thumbs'

# 认得的图片后缀（跟转换内核保持一套说法；真正能不能解码由 Pillow 现场说了算）
IMAGE_EXTS = ('jpg', 'jpeg', 'jpe', 'jfif', 'png', 'webp', 'bmp', 'gif', 'tif', 'tiff',
              'avif', 'heic', 'heif', 'jxl', 'jp2', 'ico', 'ppm', 'pgm', 'pbm', 'tga',
              'dds', 'pcx', 'psd')

THUMB_SIZE = 512          # 缩略图长边上限（墙面/拍立得都够用，解码只要几毫秒）
MAX_PHOTOS_PER_MEETING = 800   # 单次见面上限（防一次拖进整个相册把墙撑爆）
MAX_IMPORT = 3000         # 一次导入的文件数上限（超了先别闷头搬）

_DATE_RE = re.compile(r'^(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})')
_MEETING_FIELDS = ('date', 'title', 'place', 'note', 'mood')

# 配置键（都带 pw_ 前缀，跟 cv_/dl_/mu_ 一套命名）
CFG_TARGET_H = 'pw_target_h'      # 墙面基准行高
CFG_NEXT_DATE = 'pw_next_date'    # 下次见面日期（倒计时用）
CFG_NEXT_NOTE = 'pw_next_note'    # 下次见面备注（例如「去植物园」）
CFG_XP = 'pw_xp'                  # 见面是否加好感度
CFG_CARD = 'pw_card'              # 桌面卡片是否显示
CFG_CARD_POS = 'pw_card_pos'      # 卡片位置 [x, y]


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


def cfg_delete(key):
    """删掉一个键（验证脚本收尾用：本来没有的键别留个 null 在用户配置里）"""
    try:
        cfg = cfg_read()
        if key not in cfg:
            return True
        cfg.pop(key, None)
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def cfg_text(key, default=''):
    v = cfg_read().get(key)
    return default if v is None else str(v)


def cfg_int(key, default=0):
    try:
        v = cfg_read().get(key)
        return default if v is None else int(v)
    except Exception:
        return default


def cfg_bool(key, default=True):
    v = cfg_read().get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ('0', 'false', 'no', 'off', '')


# ---------------------------------------------------------------- 日期
def today_str():
    return time.strftime('%Y-%m-%d')


def norm_date(text, default=''):
    """`2026-10-21` / `2026/10/21` / `2026年10月21日` / `20261021` / `2026:10:21 18:30`
    → `2026-10-21`；不像日期就返回 default。"""
    s = str(text or '').strip()
    if not s:
        return default
    m = _DATE_RE.match(s)
    if not m:
        return default
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1970 <= y <= 2200 and 1 <= mo <= 12 and 1 <= d <= 31):
            return default
        return '%04d-%02d-%02d' % (y, mo, d)
    except Exception:
        return default


def _to_struct(date_str):
    try:
        return time.strptime(str(date_str)[:10], '%Y-%m-%d')
    except Exception:
        return None


def days_between(a, b):
    """b - a 的天数（负数表示 b 在 a 之前）；任一端不是日期返回 None"""
    sa, sb = _to_struct(a), _to_struct(b)
    if not sa or not sb:
        return None
    ta = time.mktime(sa) + 12 * 3600     # 加半天，避开夏令时/取整导致差一天
    tb = time.mktime(sb) + 12 * 3600
    return int(round((tb - ta) / 86400.0))


def weekday_cn(date_str):
    s = _to_struct(date_str)
    if not s:
        return ''
    return '周' + '一二三四五六日'[int(s.tm_wday)]


def pretty_date(date_str):
    """`2026-10-21` → `2026年10月21日 周三`（卡片/标题用）"""
    s = _to_struct(date_str)
    if not s:
        return str(date_str or '')
    return '%d年%d月%d日 %s' % (s.tm_year, s.tm_mon, s.tm_mday, weekday_cn(date_str))


# ---------------------------------------------------------------- 图片基本信息
def ext_of(path):
    return os.path.splitext(str(path or ''))[1].lower().lstrip('.')


def is_image(path):
    return ext_of(path) in IMAGE_EXTS


def _pil():
    try:
        from PIL import Image
        return Image
    except Exception:
        return None


def probe(path):
    """读真实像素尺寸 → (w, h)；读不出来返回 (0, 0)（不抛异常）"""
    Image = _pil()
    if not Image:
        return (0, 0)
    try:
        with Image.open(path) as im:
            return (int(im.width or 0), int(im.height or 0))
    except Exception:
        return (0, 0)


def _parse_exif_dt(value):
    """EXIF 的 `2026:10:21 18:30:12` → `2026-10-21`"""
    return norm_date(str(value or '').replace(':', '-', 2), '')


def exif_date(path):
    """EXIF 拍摄日期 → `YYYY-MM-DD`；没有/读不出返回 ''。
    依次找 DateTimeOriginal(36867) / DateTimeDigitized(36868) / DateTime(306)，
    主 IFD 找不到就去 Exif 子 IFD 找（有些相机只写在子 IFD 里）。"""
    Image = _pil()
    if not Image:
        return ''
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            if not ex:
                return ''
            sub = {}
            try:
                sub = ex.get_ifd(0x8769) or {}
            except Exception:
                sub = {}
            for tag in (36867, 36868, 306):
                for src in (ex, sub):
                    try:
                        got = _parse_exif_dt(src.get(tag))
                    except Exception:
                        got = ''
                    if got:
                        return got
    except Exception:
        pass
    return ''


def file_date(path):
    """按文件修改时间猜日期（没 EXIF 时兜底）"""
    try:
        return time.strftime('%Y-%m-%d', time.localtime(os.path.getmtime(path)))
    except Exception:
        return ''


def photo_date(path):
    """这张照片属于哪一天：EXIF 优先，其次文件修改时间，最后今天"""
    return exif_date(path) or file_date(path) or today_str()


def group_by_date(paths):
    """把一堆图片按「拍摄日期」分组 → [('2026-10-21', [路径…]), …]（日期正序）
    这就是「自动建议：同一天 = 一次见面」的算法，手动改分组在 UI 里做。"""
    buckets = {}
    for p in (paths or []):
        if not p or not is_image(p):
            continue
        buckets.setdefault(photo_date(p), []).append(p)
    return [(d, sorted(buckets[d])) for d in sorted(buckets)]


# ---------------------------------------------------------------- 哈希 / 缩略图
def sha1_file(path, chunk=1 << 20):
    """文件 sha1（去重与缩略图缓存都用它）；失败返回 ''"""
    h = hashlib.sha1()
    try:
        with open(path, 'rb') as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return ''


def thumb_path(src, size=THUMB_SIZE, thumb_dir=None):
    """缩略图缓存路径（键=绝对路径+修改时间+尺寸，原图改了自动换新键）"""
    src = os.path.abspath(str(src or ''))
    thumb_dir = thumb_dir or default_thumb_dir()
    try:
        st = os.stat(src)
        stamp = '%d_%d' % (int(st.st_mtime), int(st.st_size))
    except Exception:
        stamp = '0_0'
    kind = os.path.splitext(src)[1].lower().replace('.', '') or 'img'
    key = hashlib.sha1(('%s|%s|%d' % (src.lower(), stamp, int(size))).encode('utf-8')).hexdigest()[:20]
    return os.path.join(thumb_dir, '%s_%d.%s' % (key, int(size), 'png' if kind == 'png' else 'jpg'))


def default_thumb_dir():
    return os.path.join(PHOTOS_DIR, THUMB_DIRNAME)


def ensure_thumb(src, size=THUMB_SIZE, thumb_dir=None):
    """确保缩略图存在，返回缩略图路径；做不出来返回 ''（UI 那边就该显示占位块）"""
    src = os.path.abspath(str(src or ''))
    if not os.path.isfile(src):
        return ''
    out = thumb_path(src, size, thumb_dir)
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    Image = _pil()
    if not Image:
        return ''
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with Image.open(src) as im:
            im = im.convert('RGB')
            im.thumbnail((int(size), int(size)), Image.LANCZOS)
            if out.lower().endswith('.png'):
                im.save(out, 'PNG')
            else:
                im.save(out, 'JPEG', quality=86, optimize=True)
        return out if os.path.isfile(out) else ''
    except Exception:
        return ''


def clear_thumbs(thumb_dir=None):
    """清空缩略图缓存（返回删掉的个数）"""
    thumb_dir = thumb_dir or default_thumb_dir()
    n = 0
    try:
        for name in os.listdir(thumb_dir):
            try:
                os.remove(os.path.join(thumb_dir, name))
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


# ---------------------------------------------------------------- 路径换算
def rel_to_base(path, base=None):
    """绝对路径 → 相对 BASE_DIR 的 posix 风格路径（存 json 用）"""
    base = base or BASE_DIR
    try:
        return os.path.relpath(os.path.abspath(str(path)), base).replace('\\', '/')
    except Exception:
        return str(path or '').replace('\\', '/')


def abs_of(rel, base=None):
    """json 里的相对路径 → 绝对路径（已经是绝对路径就原样返回）"""
    base = base or BASE_DIR
    rel = str(rel or '')
    if not rel:
        return ''
    if os.path.isabs(rel):
        return rel
    return os.path.normpath(os.path.join(base, rel.replace('/', os.sep)))


# ---------------------------------------------------------------- 布局算法
def justified_rows(sizes, width, target_h=190, gap=8, max_last_scale=1.4):
    """把一串图片尺寸排成「等高铺满」的行 —— Justified-Gallery 的核心算法（移植成纯函数）。

    sizes          [(w, h), …] 真实像素；0/负数按 1 处理（坏图不该把整行算崩）
    width          可用宽度
    target_h       基准行高（行高由宽度反推，只有最后一行才直接用它）
    gap            间距
    max_last_scale 最后一行「一张图被拉得比基准高多少倍」就别拉了，改按基准高左对齐
    返回           {'rows': [{'y', 'h', 'items': [{'i','x','y','w','h'}]}], 'height': 总高}

    规则：一行里所有图**同高**，宽度按各自比例分，正好铺满 width（最后一行除外）；
         当「再塞一张就比基准行高还矮」时收行 —— 所以宽图窄图混着也不会变形、不会留洞。
    """
    width = max(40, int(width or 0))
    target_h = max(20, int(target_h or 0))
    gap = max(0, int(gap or 0))
    sizes = list(sizes or ())
    rows, cur, cur_sum, y = [], [], 0.0, 0

    def _flush(force_h=0, stretch=True):
        nonlocal cur, cur_sum, y
        if not cur:
            return
        n = len(cur)
        avail = max(1.0, float(width - gap * (n - 1)))
        if force_h:
            h = float(force_h)
            ws = [r * h for _i, r in cur]          # 指定了行高 → 宽度按比例自己长，不硬撑满
        else:
            h = avail / cur_sum
            ws = [avail * (r / cur_sum) for _i, r in cur]
            if ws:
                ws[-1] += avail - sum(ws)          # 抹平浮点误差，右侧不留缝
        hh = max(1, int(round(h)))
        items, x = [], 0.0
        for k, (idx, _r) in enumerate(cur):
            items.append({'i': idx, 'x': int(round(x)), 'y': y,
                          'w': max(1, int(round(ws[k]))), 'h': hh})
            x += ws[k] + gap
        rows.append({'y': y, 'h': hh, 'items': items})
        y += hh + gap
        cur, cur_sum = [], 0.0

    for i, size in enumerate(sizes):
        try:
            w, h = int(size[0] or 1), int(size[1] or 1)
        except Exception:
            w, h = 1, 1
        ratio = max(1, w) / float(max(1, h))
        cur.append((i, ratio))
        cur_sum += ratio
        avail = width - gap * (len(cur) - 1)
        if avail <= 0:
            continue
        if avail / cur_sum <= target_h:
            _flush()
    if cur:
        avail = max(1.0, float(width - gap * (len(cur) - 1)))
        if avail / cur_sum > target_h * max_last_scale:
            _flush(force_h=target_h, stretch=False)   # 末尾剩太少：按基准高排、左边对齐
        else:
            _flush()
    return {'rows': rows, 'height': (rows[-1]['y'] + rows[-1]['h']) if rows else 0}


# ---------------------------------------------------------------- 见面数据
def _new_id(date_str):
    """见面的 id：日期 + 时间戳尾数（人可读，也不容易撞）"""
    return 'm%s-%s' % (str(date_str or today_str()).replace('-', ''),
                       str(int(time.time() * 1000))[-5:])


def _default_title(date_str, n=0):
    """默认标题：知道是第几回就写「第 N 次见面」，不知道就退回日期"""
    if n and int(n) > 0:
        return '第 %d 次见面' % int(n)
    return '%s 的见面' % pretty_date(date_str)


def normalize_meeting(d, base=None):
    """把一条见面记录补全成标准形状（缺字段/坏类型都不炸）"""
    if not isinstance(d, dict):
        return None
    date = norm_date(d.get('date')) or today_str()
    out = {
        'id': str(d.get('id') or _new_id(date)),
        'date': date,
        'title': str(d.get('title') or _default_title(date)),
        'place': str(d.get('place') or ''),
        'note': str(d.get('note') or ''),
        'mood': str(d.get('mood') or ''),
        'created': str(d.get('created') or time.strftime('%Y-%m-%d %H:%M')),
        'affection_at': d.get('affection_at'),
        'photos': [],
    }
    seen = set()
    for p in (d.get('photos') or []):
        if isinstance(p, str):
            p = {'file': p}
        if not isinstance(p, dict):
            continue
        rel = str(p.get('file') or '').replace('\\', '/').strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        try:
            w, h = int(p.get('w') or 0), int(p.get('h') or 0)
        except Exception:
            w, h = 0, 0
        out['photos'].append({'file': rel, 'w': w, 'h': h,
                              'name': str(p.get('name') or ''),        # 原来的文件名（灯箱/提示里给人看）
                              'caption': str(p.get('caption') or ''),
                              'added': str(p.get('added') or '')})
    return out


def meeting_photos(meeting):
    """一条见面里的照片列表（永远是 list，坏数据当空）"""
    if not isinstance(meeting, dict):
        return []
    return [p for p in (meeting.get('photos') or []) if isinstance(p, dict) and p.get('file')]


def _sort_key(m):
    return (str(m.get('date') or ''), str(m.get('created') or ''))


class PhotoLibrary:
    """见面照片库：meetings.json + photos/ 目录。

    线程安全（UI 线程与导入线程都会碰），写盘一律原子写。
    """

    def __init__(self, base_dir=None, meetings_path=None, photos_dir=None):
        self.base_dir = base_dir or BASE_DIR
        self.meetings_path = meetings_path or (
            MEETINGS_PATH if not base_dir else os.path.join(base_dir, 'meetings.json'))
        self.photos_dir = photos_dir or (
            PHOTOS_DIR if not base_dir else os.path.join(base_dir, 'photos'))
        self._lock = threading.RLock()
        self._meetings = self._load()

    @property
    def thumb_dir(self):
        """缩略图缓存目录（跟着这个库的照片目录走，换库不串）"""
        return os.path.join(self.photos_dir, THUMB_DIRNAME)

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(self.meetings_path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except Exception:
            return []          # 坏文件当空库：不让一张破 json 把照片墙整个打死
        if not isinstance(data, list):
            return []
        out = [normalize_meeting(m, self.base_dir) for m in data]
        out = [m for m in out if m]
        out.sort(key=_sort_key, reverse=True)
        return out

    def _save_locked(self):
        try:
            os.makedirs(os.path.dirname(self.meetings_path) or '.', exist_ok=True)
            tmp = self.meetings_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._meetings, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.meetings_path)      # 原子写，防崩溃写坏
            return True
        except Exception:
            return False

    def save(self):
        with self._lock:
            return self._save_locked()

    # ---------- 读 ----------
    def meetings(self):
        with self._lock:
            return list(self._meetings)

    def count(self):
        with self._lock:
            return len(self._meetings)

    def photo_count(self):
        with self._lock:
            return sum(len(meeting_photos(m)) for m in self._meetings)

    def get(self, mid):
        with self._lock:
            for m in self._meetings:
                if m.get('id') == mid:
                    return m
        return None

    def find_by_date(self, date_str):
        d = norm_date(date_str)
        with self._lock:
            for m in self._meetings:
                if d and m.get('date') == d:
                    return m
        return None

    def search(self, query):
        """按标题/地点/便签/日期找见面（本地即时，不联网）"""
        q = str(query or '').strip().lower()
        with self._lock:
            if not q:
                return list(self._meetings)
            out = []
            for m in self._meetings:
                blob = ' '.join([str(m.get(k) or '') for k in ('title', 'place', 'note', 'mood', 'date')]).lower()
                if q in blob:
                    out.append(m)
            return out

    # ---------- 写 ----------
    def _unique_id(self, mid):
        """同一天里连开两次见面时别撞 id（撞了照片会归到别人家去）"""
        ids = {m.get('id') for m in self._meetings}
        if mid not in ids:
            return mid
        n = 2
        while ('%s-%d' % (mid, n)) in ids:
            n += 1
        return '%s-%d' % (mid, n)

    def create_meeting(self, date='', title='', place='', note='', mood='', save=True):
        date = norm_date(date) or today_str()
        with self._lock:
            ttl = str(title or '').strip() or _default_title(date, len(self._meetings) + 1)
            m = normalize_meeting({'date': date, 'title': ttl, 'place': place,
                                   'note': note, 'mood': mood}, self.base_dir)
            m['id'] = self._unique_id(m.get('id'))
            self._meetings.append(m)
            self._meetings.sort(key=_sort_key, reverse=True)
            if save:
                self._save_locked()
        return m

    def update_meeting(self, mid, save=True, **fields):
        with self._lock:
            m = self.get(mid)
            if not m:
                return None
            for k in _MEETING_FIELDS:
                if k in fields and fields[k] is not None:
                    m[k] = norm_date(fields[k]) if k == 'date' else str(fields[k])
            if 'affection_at' in fields:
                m['affection_at'] = fields['affection_at']
            self._meetings.sort(key=_sort_key, reverse=True)
            if save:
                self._save_locked()
            return m

    def delete_meeting(self, mid, delete_files=True):
        """删掉一次见面；delete_files=True 时连 photos/ 里的副本一起删"""
        with self._lock:
            m = self.get(mid)
            if not m:
                return False
            self._meetings = [x for x in self._meetings if x.get('id') != mid]
            if delete_files:
                for p in meeting_photos(m):
                    _remove_quiet(abs_of(p.get('file'), self.base_dir))
            self._save_locked()
            return True

    def remove_photo(self, mid, rel, delete_file=True):
        with self._lock:
            m = self.get(mid)
            if not m:
                return False
            before = len(m['photos'])
            m['photos'] = [p for p in m['photos'] if p.get('file') != rel]
            if len(m['photos']) == before:
                return False
            if delete_file:
                _remove_quiet(abs_of(rel, self.base_dir))
            self._save_locked()
            return True

    def move_photo(self, rel, from_mid, to_mid):
        """把一张照片挪到另一次见面（磁盘文件不动，只换归属）"""
        with self._lock:
            src_m, dst_m = self.get(from_mid), self.get(to_mid)
            if not src_m or not dst_m:
                return False
            photo = None
            for p in src_m['photos']:
                if p.get('file') == rel:
                    photo = p
                    break
            if not photo:
                return False
            src_m['photos'] = [p for p in src_m['photos'] if p.get('file') != rel]
            if all(p.get('file') != rel for p in dst_m['photos']):
                dst_m['photos'].append(photo)
            self._meetings.sort(key=_sort_key, reverse=True)
            self._save_locked()
            return True

    # ---------- 导入 ----------
    def _digest_index(self):
        """照片文件名里带 sha1 前 6 位（导入时命名的），拿文件名就能查重，不用重算哈希"""
        idx = {}
        with self._lock:
            for m in self._meetings:
                for p in meeting_photos(m):
                    base = os.path.basename(str(p.get('file') or ''))
                    parts = base.split('_')
                    if len(parts) >= 2:
                        key = os.path.splitext(parts[1])[0]
                        if len(key) >= 6:
                            idx[key] = p.get('file')
        return idx

    def import_paths(self, paths, date='', title='', split_by_date=True, save=True):
        """把文件/文件夹导入照片墙（复制，不动原图）。

        split_by_date=True 时按拍摄日期自动分组成「一次见面」（已存在同一天的见面就并进去）；
        给了 date 就全部塞进那一天。

        返回 {'added': n, 'dups': n, 'skipped': [(路径, 原因)], 'meetings': [id…], 'over': bool}
        """
        files = expand_image_paths(paths)
        res = {'added': 0, 'dups': 0, 'skipped': [], 'meetings': [], 'over': False,
               'error': ''}
        # 用户明确点名的文件里，不是图片的直接说清（拖了个 txt 进来也得有交代）
        for p in (paths or []):
            p = str(p or '')
            if p and os.path.isfile(p) and not is_image(p):
                res['skipped'].append((p, '不是图片'))
        if not files:
            res['error'] = '没找到图片文件'
            return res
        if len(files) > MAX_IMPORT:
            res['over'] = True
            files = files[:MAX_IMPORT]
        idx = self._digest_index()
        buckets = {}
        fixed = norm_date(date)
        for src in files:
            digest = sha1_file(src)
            if not digest:
                res['skipped'].append((src, '读不出文件'))
                continue
            key = digest[:6]
            if key in idx:
                res['dups'] += 1
                continue
            day = fixed or (photo_date(src) if split_by_date else today_str())
            rel, status, err = self._copy_one(src, day, digest)
            if status == 'ok':
                idx[key] = rel
                res['added'] += 1
                buckets.setdefault(day, []).append((rel, os.path.basename(src)))
            elif status == 'dup':
                res['dups'] += 1
            else:
                res['skipped'].append((src, err or '跳过'))
        touched = []
        for day, rels in sorted(buckets.items()):
            m = self.find_by_date(day) if split_by_date else None
            if m is None:
                m = self.create_meeting(day, title=title, save=False)
            for rel, name in rels:
                w, h = probe(abs_of(rel, self.base_dir))
                m['photos'].append({'file': rel, 'w': w, 'h': h, 'name': name, 'caption': '',
                                    'added': time.strftime('%Y-%m-%d %H:%M')})
            m['photos'] = m['photos'][-MAX_PHOTOS_PER_MEETING:]
            touched.append(m.get('id'))
        with self._lock:
            self._meetings.sort(key=_sort_key, reverse=True)
            if save and touched:
                self._save_locked()
        res['meetings'] = touched
        return res

    def add_photos(self, mid, paths, save=True):
        """往**已有**的某次见面里加照片（不新建见面）。返回形状同 import_paths。"""
        res = {'added': 0, 'dups': 0, 'skipped': [], 'meetings': [], 'over': False, 'error': ''}
        files = expand_image_paths(paths)
        if not files:
            res['error'] = '没找到图片文件'
            return res
        if len(files) > MAX_IMPORT:
            res['over'] = True
            files = files[:MAX_IMPORT]
        with self._lock:
            m = self.get(mid)
            if not m:
                res['error'] = '找不到这次见面'
                return res
            day = norm_date(m.get('date')) or today_str()
            idx = self._digest_index()
            owned = {p.get('file') for p in meeting_photos(m)}
            for src in files:
                digest = sha1_file(src)
                if not digest:
                    res['skipped'].append((src, '读不出文件'))
                    continue
                key = digest[:6]
                if key in idx:
                    res['dups'] += 1
                    continue
                rel, status, err = self._copy_one(src, day, digest)
                if status == 'ok':
                    idx[key] = rel
                    owned.add(rel)
                    w, h = probe(abs_of(rel, self.base_dir))
                    m['photos'].append({'file': rel, 'w': w, 'h': h,
                                        'name': os.path.basename(src), 'caption': '',
                                        'added': time.strftime('%Y-%m-%d %H:%M')})
                    res['added'] += 1
                elif status == 'dup':
                    res['dups'] += 1
                else:
                    res['skipped'].append((src, err or '跳过'))
            if len(m['photos']) > MAX_PHOTOS_PER_MEETING:
                res['over'] = True
                m['photos'] = m['photos'][-MAX_PHOTOS_PER_MEETING:]
            if save:
                self._save_locked()
            res['meetings'] = [m.get('id')]
        return res

    def _copy_one(self, src, day, digest=''):
        """复制一张照片进 photos/<day>/ → (相对路径, ok|dup|skip|error, 说明)"""
        src = os.path.abspath(str(src or ''))
        if not os.path.isfile(src):
            return '', 'skip', '文件不存在'
        if not is_image(src):
            return '', 'skip', '不是图片'
        digest = digest or sha1_file(src)
        if not digest:
            return '', 'error', '读不出文件'
        dest_dir = os.path.join(self.photos_dir, day)
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except Exception as exc:
            return '', 'error', '建不了目录：%s' % exc
        stem = '%s_%s' % (time.strftime('%H%M%S', time.localtime(_mtime(src))), digest[:6])
        ext = '.' + (ext_of(src) or 'jpg')
        dest = os.path.join(dest_dir, stem + ext)
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(dest_dir, '%s_%d%s' % (stem, n, ext))
        if n > 1:
            return '', 'dup', '同名照片已存在'
        try:
            shutil.copy2(src, dest)
        except Exception as exc:
            return '', 'error', '复制失败：%s' % exc
        dest = _convert_if_unreadable(dest)
        return rel_to_base(dest, self.base_dir), 'ok', ''

    # ---------- 统计 / 倒计时 ----------
    def stats(self, today=None):
        """墙面顶部那行数字 + 下次见面倒计时（不联网、纯算）"""
        today = norm_date(today) or today_str()
        with self._lock:
            ms = list(self._meetings)
        dates = sorted(str(m.get('date') or '') for m in ms if m.get('date'))
        first = dates[0] if dates else ''
        last = dates[-1] if dates else ''
        nd = norm_date(cfg_text(CFG_NEXT_DATE))
        return {
            'meetings': len(ms),
            'photos': sum(len(meeting_photos(m)) for m in ms),
            'first': first,
            'last': last,
            'days_together': days_between(first, today),
            'days_since_last': days_between(last, today),
            'next': nd,
            'next_note': cfg_text(CFG_NEXT_NOTE),
            'days_to_next': days_between(today, nd) if nd else None,
            'today': today,
        }

    def stats_line(self, today=None):
        """统计压成一句人话（卡片/面板标题共用）"""
        s = self.stats(today)
        if not s['meetings']:
            return '还没记下见面——把照片拖进来就行'
        bits = ['%d 次见面' % s['meetings'], '%d 张照片' % s['photos']]
        d = s['days_to_next']
        since = s['days_since_last']
        if d is not None:
            if d > 0:
                bits.append('距下次见面还有 %d 天' % d)
            elif d == 0:
                bits.append('就是今天见面！')
            else:
                bits.append('约好的日子已过 %d 天' % (-d))
        elif since is not None:
            # 照片日期可能填在未来（提前记好约好的那次），别显示成负数
            bits.append('距上次见面 %d 天' % since if since >= 0 else '最近一次记在 %d 天后' % (-since))
        return ' · '.join(bits)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return time.time()


def _remove_quiet(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def _convert_if_unreadable(dest):
    """Pillow 读不出来的格式（HEIC/HEIF/AVIF 之类）→ 借 v6.87 的转换内核转成 jpg 再存。

    读不出来也不删原副本（万一转换也没成，用户至少还有一张能在别处打开的图）。
    """
    if probe(dest)[0]:
        return dest
    try:
        import pet_convert as cv
        res = cv.convert_file(dest, 'jpg', outdir=os.path.dirname(dest), quality=92)
        out = (res or {}).get('out') or ''
        if res and res.get('ok') and out and os.path.isfile(out) \
                and os.path.abspath(out) != os.path.abspath(dest) and probe(out)[0]:
            _remove_quiet(dest)
            return out
    except Exception:
        pass
    return dest


def expand_image_paths(paths, recursive=True, max_files=MAX_IMPORT):
    """把「文件 + 文件夹」摊平成图片文件列表（去重、保序、上限保护）"""
    out, seen = [], set()

    def _add(p):
        p = os.path.abspath(str(p or ''))
        if not p or p in seen:
            return
        if os.path.isfile(p):
            if is_image(p):
                seen.add(p)
                out.append(p)
        elif os.path.isdir(p) and recursive:
            try:
                for name in sorted(os.listdir(p)):
                    _add(os.path.join(p, name))
            except Exception:
                pass

    for p in (paths or []):
        _add(p)
        if len(out) >= max_files:
            break
    return out[:max_files]


# ---------------------------------------------------------------- 便捷入口
_DEFAULT_LIB = []


def default_library():
    """进程内共享一个库（UI 各处拿同一个，避免两次读到不同内存态）"""
    if not _DEFAULT_LIB:
        _DEFAULT_LIB.append(PhotoLibrary())
    return _DEFAULT_LIB[0]


def reset_default_library(base_dir=None):
    """换一个库（测试/验证脚本用）"""
    _DEFAULT_LIB[:] = [PhotoLibrary(base_dir)] if base_dir else []
    return default_library() if base_dir else None
