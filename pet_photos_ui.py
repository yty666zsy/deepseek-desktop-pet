# -*- coding: utf-8 -*-
"""pet_photos_ui.py — 「见面照片墙」界面（v6.88）

分工：
    pet_photos.py   内核：meetings.json / 照片复制 / 缩略图缓存 / EXIF 分组 / justified 布局
    本模块           界面：墙面总览 → 单次见面（拍立得）→ 灯箱；另有桌宠旁边的小卡片

为什么全自绘（不嵌现成照片墙）
------------------------------
JS 照片墙（Justified-Gallery / PhotoSwipe / Photo_Wall）要 QtWebEngine，而
`tools/DeepSeekPet.spec` 为压包体已经把 WebEngine/QtQml 全 excludes 了；
GPL 的 TagStudio/beeref 又是 QML + 传染。所以这里：
    · 布局算法借 Justified-Gallery 的思路，落在 pet_photos.justified_rows（纯函数、可单测）
    · 灯箱/悬停/翻页这些交互自己写
    · 拍立得那张相纸是 QPainter 现画的（白边 + 投影 + 轻微歪斜），不往磁盘写合成图

规矩（跟别的面板一套）：
- 颜色一律走 pet_theme token（tests/test_theme_tokens.py 扫硬编码颜色；token 名在
  本文件顶部的 PHOTO_TOKENS 里列全，tests/test_photos.py 会校验它们真的存在）
- 面板懒创建：点了才读 meetings.json、才起缩略图线程
- 缩略图在后台线程预热，绘制时只解码 512px 小图（大图只在你双击看大图时才解码）
"""

import os

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QDateEdit, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QPushButton, QScrollArea, QTextEdit,
                               QVBoxLayout, QWidget)

import pet_photos as ph
import pet_theme as _pt
import pet_window as pwin  # v6.90 每个窗口标题栏的「—」隐藏键
from pet_theme import color as T

# 本模块用到的全部 token（测试会逐个校验它们真的在 pet_theme.DEFAULT_THEME 里）
PHOTO_TOKENS = ('ui_bg', 'ui_text', 'ui_text_dim', 'ui_text_strong', 'dialog_bg', 'list_bg',
                'list_border', 'item_bg', 'ui_btn_bg', 'ui_btn_hover', 'ui_border_soft',
                'ui_btn_alt', 'ui_input_bg', 'ui_accent', 'accent', 'ui_popup_bg',
                'ui_popup_btn_bg', 'ui_popup_btn_hover', 'ui_board_bg', 'input_text', 'ui_stats_bg',
                'ui_photo_paper', 'ui_photo_paper_edge', 'ui_photo_ink', 'ui_photo_shadow',
                'ui_photo_empty', 'ui_photo_header')

_OPEN_DIALOGS = []          # 已打开的照片墙窗口（主题变了要重刷）
_QC_CACHE = {}
_LIB_BASE = []              # 当前界面绑定的照片库根目录（测试里是临时目录）


def _bind_lib(lib):
    """把界面绑定到这个库：照片相对路径按库根目录解析，缩略图缓存也落在库自己的 photos/ 下"""
    try:
        _LIB_BASE[:] = [lib.base_dir]
        _THUMBS.thumb_dir = lib.thumb_dir
    except Exception:
        _LIB_BASE[:] = []
        _THUMBS.thumb_dir = None


def _abs(rel):
    """json 里的相对路径 → 绝对路径（跟着当前绑定的库）"""
    return ph.abs_of(rel, _LIB_BASE[0] if _LIB_BASE else None)


# ---------------------------------------------------------------- 颜色
def _qc(key, alpha=None):
    """token → QColor（token 值可能是纯色也可能是带透明度的 rgba 写法）"""
    ck = (key, alpha)
    got = _QC_CACHE.get(ck)
    if got is not None:
        return QColor(got)
    v = str(T(key) or '').strip()
    col = QColor(v)
    if not col.isValid():
        txt = v.replace(' ', '')
        try:
            if txt.lower().startswith('rgba(') or txt.lower().startswith('rgb('):   # theme-exempt（解析 token 值，不是写死颜色）
                body = txt[txt.index('(') + 1:txt.rindex(')')]
                parts = [p for p in body.replace('/', ',').split(',') if p != '']
                r, g, b = (int(float(parts[0])), int(float(parts[1])), int(float(parts[2])))
                a = float(parts[3]) if len(parts) > 3 else 1.0
                if a <= 1.0:
                    a = int(max(0.0, min(1.0, a)) * 255)
                col = QColor(r, g, b, int(a))
            else:
                col = QColor(str(T('ui_text')))
        except Exception:
            col = QColor(str(T('ui_text')))
    if alpha is not None:
        col.setAlphaF(max(0.0, min(1.0, float(alpha))))
    if col.isValid():
        _QC_CACHE[ck] = col.name(QColor.HexArgb)
    return col


def _tk_of(pet, key):
    """宿主有 _tk 就用宿主的（切换主题时它自己管），没有就退回唯一源"""
    try:
        if pet is not None:
            return pet._tk(key)
    except Exception:
        pass
    return T(key)


# ---------------------------------------------------------------- 样式
def wall_qss(pet=None):
    """墙面 / 见面 / 卡片共用的一套控件样式（颜色全走 token）"""
    g = lambda k: _tk_of(pet, k)      # noqa: E731（只是取值别名）
    return (
        "QDialog { background:%s; }" % g('ui_bg')
        + "QLabel { color:%s; font-size:12px; background:transparent; }" % g('ui_text')
        + "QScrollArea { background:%s; border:1px solid %s; border-radius:10px; }"
          % (g('ui_input_bg'), g('ui_border_soft'))
        + "QScrollArea > QWidget { background:%s; }" % g('ui_input_bg')
        # ↑ viewport 也得单独上色：不然墙面中间是系统白底（深色面板里一眼就露）
        + "QScrollBar:vertical { background:transparent; width:10px; margin:2px; }"
        + "QScrollBar::handle:vertical { background:%s; border-radius:5px; min-height:28px; }"
          % g('ui_btn_bg')
        + "QScrollBar::handle:vertical:hover { background:%s; }" % g('ui_btn_hover')
        + "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        + "QPushButton { background:%s; color:%s; border:none; border-radius:6px;"
          " padding:6px 12px; font-size:12px; }" % (g('ui_btn_bg'), g('ui_text_strong'))
        + "QPushButton:hover { background:%s; }" % g('ui_btn_hover')
        + "QPushButton:disabled { background:%s; color:%s; }" % (g('ui_btn_alt'), g('ui_text_dim'))
        + "QLineEdit, QTextEdit { background:%s; color:%s; border:1px solid %s;"
          " border-radius:6px; padding:4px 6px; font-size:12px; }"
          % (g('ui_input_bg'), g('ui_text'), g('ui_border_soft'))
        + "QLineEdit:focus, QTextEdit:focus { border:1px solid %s; }" % g('ui_accent')
        + "QMenu { background:%s; color:%s; border:1px solid %s; }"
          % (g('ui_popup_bg'), g('ui_text'), g('ui_border_soft'))
        + "QMenu::item { padding:5px 18px; }"
        + "QMenu::item:selected { background:%s; }" % g('ui_popup_btn_hover')
    )


def card_qss(pet=None):
    """桌宠旁边那张小卡片"""
    g = lambda k: _tk_of(pet, k)      # noqa: E731
    return ("QLabel { background:transparent; }"
            + "QPushButton { background:%s; color:%s; border:none; border-radius:6px;"
              " padding:4px 10px; font-size:11px; }" % (g('ui_popup_btn_bg'), g('ui_text'))
            + "QPushButton:hover { background:%s; }" % g('ui_popup_btn_hover'))


def _tint_viewport(scroll, pet=None):
    """给滚动区那块 viewport 上色

    QScrollArea 的 viewport 是个独立子控件，样式表里 `QScrollArea { background }` 管不到它，
    于是深色面板中间会露出一大块**系统白底**（v6.88 真实截图里一眼就看到了）。这里按主题刷调色板。
    """
    try:
        from PySide6.QtGui import QPalette
        col = _qc('ui_input_bg')
        targets = [scroll.viewport(), scroll.widget()]
        for w in targets:
            if w is None:
                continue
            pal = w.palette()
            pal.setColor(QPalette.Window, col)
            pal.setColor(QPalette.Base, col)   # 底色走 Base 角色，光设 Window 不管用
            w.setPalette(pal)
            w.setAutoFillBackground(True)      # 自绘控件先铺一层主题底色，再画照片
    except Exception:
        pass


def refresh_open():
    """主题变化时重刷已打开的照片墙窗口（跟回忆相册一套做法）"""
    alive = []
    for w in list(_OPEN_DIALOGS):
        try:
            if not w.isVisible():
                continue
            fn = getattr(w, 'apply_theme', None)
            if callable(fn):
                fn()
            alive.append(w)
        except RuntimeError:
            continue
        except Exception:
            continue
    _OPEN_DIALOGS[:] = alive


_pt.subscribe(refresh_open)


# ---------------------------------------------------------------- 缩略图缓存
class ThumbCache(object):
    """rel → QPixmap 的小 LRU（缩略图只有几十 KB，留几百张不会撑爆内存）"""

    def __init__(self, limit=600, thumb_dir=None):
        self.limit = limit
        self.thumb_dir = thumb_dir
        self._d = {}
        self._order = []
        self._thumbs = {}

    def thumb_of(self, rel):
        """照片相对路径 → 缩略图绝对路径（做不出来返回原图，UI 那边会自己缩放）"""
        got = self._thumbs.get(rel)
        if got:
            return got
        src = _abs(rel)
        thumb = ph.ensure_thumb(src, ph.THUMB_SIZE, self.thumb_dir) if src else ''
        out = thumb or src
        if out:
            self._thumbs[rel] = out
        return out

    def pixmap(self, rel):
        got = self._d.get(rel)
        if got is not None:
            return got
        path = self.thumb_of(rel)
        pm = QPixmap()
        if path and os.path.isfile(path):
            pm = QPixmap(path)
        self._d[rel] = pm
        self._order.append(rel)
        while len(self._order) > self.limit:
            old = self._order.pop(0)
            self._d.pop(old, None)
        return pm

    def clear(self):
        self._d.clear()
        self._order = []
        self._thumbs.clear()


_THUMBS = ThumbCache()


class ThumbPump(QObject):
    """缩略图慢速泵：每 30ms 补几张，全在 UI 线程里做

    为什么不用后台线程：进程退出时后台线程还在 PIL 里解码，跟解释器析构抢命会
    偶发 access violation（v6.88 实测：离屏跑完 out 之后随机崩）。文件层一张
    ~10-40ms，每帧补几张已经够快，还不用关心线程安全。
    """

    def __init__(self, parent=None, batch=6, interval=30):
        super(ThumbPump, self).__init__(parent)
        self.batch = batch
        self._queue = []
        self.changed = None          # 有进展就回调（一般是重绘）
        self._timer = QTimer(self)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self.step)

    def feed(self, rels):
        self._queue = [r for r in (rels or []) if r and r not in _THUMBS._thumbs]
        if self._queue:
            self._timer.start()

    def step(self):
        for rel in self._queue[:self.batch]:
            try:
                _THUMBS.thumb_of(rel)
            except Exception:
                pass
        self._queue = self._queue[self.batch:]
        if callable(self.changed):
            try:
                self.changed()
            except Exception:
                pass
        if not self._queue:
            self._timer.stop()

    def busy(self):
        return bool(self._queue)

    def stop(self):
        self._timer.stop()
        self._queue = []


# ---------------------------------------------------------------- 墙面（justified 自绘）
class WallView(QWidget):
    """墙面：按「每次见面」分组，组内按 justified 铺满（等高不等宽、不变形不留洞）"""

    meetingClicked = Signal(str)             # 点分组标题
    photoClicked = Signal(str, str)          # (mid, rel) 单击
    photoActivated = Signal(str, str)        # 双击 → 灯箱
    contextAsked = Signal(object, QPoint)    # 右键（payload, 全局坐标）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._meetings = []
        self._hits = []
        self._heads = []
        self._hover = -1
        self._target_h = ph.cfg_int(ph.CFG_TARGET_H, 190) or 190
        self._pump = ThumbPump(self, batch=8, interval=30)
        self._pump.changed = self.update

    # ---------- 数据 ----------
    def set_meetings(self, meetings):
        self._meetings = list(meetings or [])
        self._hover = -1
        self.relayout()

    def set_target_h(self, h):
        self._target_h = max(90, min(400, int(h or 190)))
        self.relayout()

    # ---------- 布局 ----------
    def relayout(self):
        width = max(220, self.width())
        pad, gap, head_h = 12, 8, 26
        inner = max(80, width - pad * 2)
        y = pad
        hits, heads = [], []
        for m in self._meetings:
            photos = ph.meeting_photos(m)
            n = len(photos)
            date = ph.pretty_date(m.get('date'))
            title = str(m.get('title') or '')
            label = ('📷 %s' % date) + ('  ·  %s' % title if title and title not in date else '')
            place = str(m.get('place') or '')
            sub = ('%d 张' % n) + ('  ·  %s' % place if place else '')
            heads.append({'rect': QRect(pad, y, inner, head_h), 'mid': m.get('id'),
                          'label': label, 'sub': sub, 'empty': n == 0})
            y += head_h + 4
            if not photos:
                y += 26          # 空见面：留一行写「还没有照片」
                continue
            sizes = [((p.get('w') or 4), (p.get('h') or 3)) for p in photos]
            lay = ph.justified_rows(sizes, inner, self._target_h, gap)
            for row in lay['rows']:
                for it in row['items']:
                    idx = it['i']
                    if idx >= len(photos):
                        continue
                    rect = QRect(pad + it['x'], y + it['y'], it['w'], it['h'])
                    hits.append({'rect': rect, 'mid': m.get('id'),
                                 'rel': photos[idx].get('file')})
            y += lay['height'] + 16
        self._hits, self._heads = hits, heads
        self.setMinimumHeight(max(240, y + pad))
        self.updateGeometry()
        self.update()
        self._pump.feed([h['rel'] for h in hits])

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.relayout()

    # ---------- 绘制 ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        clip = ev.rect()
        if not self._meetings:
            self._draw_empty(p)
            return
        for h in self._heads:
            if not h['rect'].intersects(clip):
                continue
            f = p.font()
            f.setBold(True)
            f.setPointSizeF(10.5)
            p.setFont(f)
            p.setPen(QPen(_qc('ui_photo_header')))
            p.drawText(h['rect'], int(Qt.AlignLeft | Qt.AlignVCenter), h['label'])
            f.setBold(False)
            f.setPointSizeF(9.0)
            p.setFont(f)
            p.setPen(QPen(_qc('ui_text_dim')))
            p.drawText(h['rect'], int(Qt.AlignRight | Qt.AlignVCenter), h['sub'])
            if h.get('empty'):
                rr = QRect(h['rect'].x(), h['rect'].bottom() + 2, h['rect'].width(), 24)
                p.setPen(QPen(_qc('ui_text_dim')))
                p.drawText(rr, int(Qt.AlignLeft | Qt.AlignVCenter), '（还没有照片，点这行打开它加点）')
        for i, hit in enumerate(self._hits):
            r = hit['rect']
            if not r.intersects(clip):
                continue
            pm = _THUMBS.pixmap(hit['rel'])
            path = QPainterPath()
            path.addRoundedRect(QRectF(r), 7, 7)
            p.save()
            p.setClipPath(path)
            if pm.isNull():
                p.fillRect(r, _qc('ui_photo_empty'))
                f = p.font()
                f.setPointSizeF(9.0)
                p.setFont(f)
                p.setPen(QPen(_qc('ui_text_dim')))
                p.drawText(r, int(Qt.AlignCenter), '读不出这张')
            else:
                scaled = pm.scaled(r.size(), Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
                dx = r.x() + (r.width() - scaled.width()) // 2
                dy = r.y() + (r.height() - scaled.height()) // 2
                p.drawPixmap(dx, dy, scaled)
            p.restore()
            if i == self._hover:
                p.setPen(QPen(_qc('ui_accent'), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 7, 7)

    def _draw_empty(self, p):
        f = p.font()
        f.setPointSizeF(12.0)
        p.setFont(f)
        p.setPen(QPen(_qc('ui_text_dim')))
        p.drawText(self.rect(), int(Qt.AlignCenter),
                   '还没有照片\n\n把照片（或装着照片的文件夹）拖进这个窗口，\n'
                   '会按拍摄日期自动分成「每一次见面」')

    # ---------- 命中 ----------
    def _hit_at(self, pos):
        for i, h in enumerate(self._hits):
            if h['rect'].contains(pos):
                return i, h
        for h in self._heads:
            if h['rect'].contains(pos):
                return -1, h
        return -2, None

    def mousePressEvent(self, ev):
        idx, hit = self._hit_at(ev.pos())
        if hit is None:
            return
        if ev.button() == Qt.RightButton and idx >= 0:
            self.contextAsked.emit(dict(hit), ev.globalPos())
            return
        if ev.button() == Qt.LeftButton:
            if idx >= 0:
                self.photoClicked.emit(hit.get('mid'), hit.get('rel'))
            else:
                self.meetingClicked.emit(hit.get('mid'))

    def mouseDoubleClickEvent(self, ev):
        idx, hit = self._hit_at(ev.pos())
        if idx >= 0 and ev.button() == Qt.LeftButton:
            self.photoActivated.emit(hit.get('mid'), hit.get('rel'))
        elif hit is not None and ev.button() == Qt.LeftButton:
            self.meetingClicked.emit(hit.get('mid'))

    def mouseMoveEvent(self, ev):
        idx, _hit = self._hit_at(ev.pos())
        if idx != self._hover:
            self._hover = idx
            self.update()

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(ev)


# ---------------------------------------------------------------- 拍立得贴墙
class PolaroidView(QWidget):
    """单次见面：一张张拍立得贴在墙上（白相纸 + 投影 + 轻微歪斜，歪多少由文件名定死）"""

    CARD_W = 196
    photoClicked = Signal(int)
    photoDoubleClicked = Signal(int)
    contextAsked = Signal(int, QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._photos = []
        self._cells = []
        self._sel = -1
        self._hover = -1
        self._default_caption = ''
        self._pump = ThumbPump(self, batch=4, interval=30)
        self._pump.changed = self.update

    def set_photos(self, photos, default_caption=''):
        """default_caption：图注空着时相纸上写什么（一般是这次见面的日期）"""
        self._photos = list(photos or [])
        self._default_caption = str(default_caption or '')
        if self._sel >= len(self._photos):
            self._sel = -1
        self.relayout()

    def selected_rel(self):
        if 0 <= self._sel < len(self._photos):
            return self._photos[self._sel].get('file')
        return ''

    def relayout(self):
        width = max(220, self.width())
        pad_x, pad_y, gap = 16, 18, 18
        n = max(1, len(self._photos))
        per_row = max(1, (width - pad_x * 2 + gap) // (self.CARD_W + gap))
        cells = []
        for i, p in enumerate(self._photos):
            col = i % per_row
            row = i // per_row
            cw, ch = self.CARD_W, self._paper_h()
            cx = pad_x + col * (cw + gap) + cw // 2
            cy = pad_y + row * (ch + gap) + ch // 2 + (14 if col % 2 else 0)
            cells.append((cx, cy))
        self._cells = cells
        rows = (n + per_row - 1) // per_row
        self.setMinimumHeight(pad_y * 2 + rows * (self._paper_h() + gap) + 24)
        self.updateGeometry()
        self.update()
        self._pump.feed([p.get('file') for p in self._photos])

    def _paper_h(self):
        return int(self.CARD_W * 0.78) + 44     # 照片区 + 下面写字的相纸边

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.relayout()

    def _angle(self, rel):
        """稳定的歪斜角度（-3…3 度）：同一张照片每次都一样，不会重绘就乱转"""
        try:
            seed = sum(ord(c) for c in str(rel or '')) % 7
        except Exception:
            seed = 0
        return seed - 3.0

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if not self._photos:
            f = p.font()
            f.setPointSizeF(11.0)
            p.setFont(f)
            p.setPen(QPen(_qc('ui_text_dim')))
            p.drawText(self.rect(), int(Qt.AlignCenter), '这次见面还没照片\n\n点下面「📂 添加照片」或直接把照片拖进来')
            return
        cw, ch = self.CARD_W, self._paper_h()
        for i, photo in enumerate(self._photos):
            if i >= len(self._cells):
                break
            cx, cy = self._cells[i]
            p.save()
            p.translate(cx, cy)
            p.rotate(self._angle(photo.get('file')))
            paper = QRectF(-cw / 2.0, -ch / 2.0, cw, ch)
            # 投影（往右下偏一点，像贴在墙上）
            p.setPen(Qt.NoPen)
            p.setBrush(_qc('ui_photo_shadow'))
            p.drawRoundedRect(paper.translated(3, 5), 3, 3)
            # 相纸
            p.setBrush(_qc('ui_photo_paper'))
            p.setPen(QPen(_qc('ui_photo_paper_edge'), 1))
            p.drawRoundedRect(paper, 3, 3)
            # 照片
            img = paper.adjusted(9, 9, -9, -(ch - (ch - 44) ) + 0)
            img = QRectF(paper.left() + 9, paper.top() + 9, cw - 18, ch - 44 - 9)
            pm = _THUMBS.pixmap(photo.get('file'))
            if pm.isNull():
                p.fillRect(img, _qc('ui_photo_empty'))
            else:
                path = QPainterPath()
                path.addRect(img)
                p.save()
                p.setClipPath(path)
                scaled = pm.scaled(img.size().toSize(), Qt.KeepAspectRatioByExpanding,
                                   Qt.SmoothTransformation)
                p.drawPixmap(int(img.left() + (img.width() - scaled.width()) / 2),
                             int(img.top() + (img.height() - scaled.height()) / 2), scaled)
                p.restore()
            # 相纸下面那行字（图注优先，没写就写这次见面的日期）
            cap = str(photo.get('caption') or '').strip() or self._default_caption
            f = p.font()
            f.setPointSizeF(8.5)
            p.setFont(f)
            p.setPen(QPen(_qc('ui_photo_ink')))
            cap_rect = QRectF(paper.left() + 10, img.bottom() + 2,
                              cw - 20, max(12.0, paper.bottom() - img.bottom() - 6))
            p.drawText(cap_rect, int(Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap), cap)
            # 选中/悬停描边
            if i == self._sel:
                p.setPen(QPen(_qc('ui_accent'), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(paper.adjusted(-1, -1, 1, 1), 4, 4)
            elif i == self._hover:
                p.setPen(QPen(_qc('ui_accent'), 1))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(paper.adjusted(-1, -1, 1, 1), 4, 4)
            p.restore()

    # ---------- 命中（拍立得有旋转，用外接框近似就够了） ----------
    def _hit_at(self, pos):
        cw, ch = self.CARD_W, self._paper_h()
        for i, (cx, cy) in reversed(list(enumerate(self._cells))):
            box = QRect(int(cx - cw / 2), int(cy - ch / 2), cw, ch)
            if box.contains(pos):
                return i
        return -1

    def mousePressEvent(self, ev):
        i = self._hit_at(ev.pos())
        if i < 0:
            self._sel = -1
            self.update()
            return
        self._sel = i
        self.setFocus()
        self.update()
        if ev.button() == Qt.RightButton:
            self.contextAsked.emit(i, ev.globalPos())
        elif ev.button() == Qt.LeftButton:
            self.photoClicked.emit(i)

    def mouseDoubleClickEvent(self, ev):
        i = self._hit_at(ev.pos())
        if i >= 0 and ev.button() == Qt.LeftButton:
            self.photoDoubleClicked.emit(i)

    def mouseMoveEvent(self, ev):
        i = self._hit_at(ev.pos())
        if i != self._hover:
            self._hover = i
            self.update()

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Left, Qt.Key_Right) and self._photos:
            step = -1 if ev.key() == Qt.Key_Left else 1
            self._sel = max(0, min(len(self._photos) - 1,
                                   (self._sel if self._sel >= 0 else 0) + step))
            self.update()
            return
        super().keyPressEvent(ev)


# ---------------------------------------------------------------- 灯箱
class PhotoLightbox(QDialog):
    """点开看大图：左右翻页、Esc 关（照片墙唯一「全屏」的地方，不记忆缩放位置）"""

    def __init__(self, photos, index=0, title='', parent=None):
        super(PhotoLightbox, self).__init__(parent)
        pwin.add_hide_button(self)      # v6.90：普通窗口 + 标题栏「—」（Tool 窗口系统不画它）
        self.setWindowTitle('📷 ' + (title or '照片'))
        self.resize(880, 660)
        self._photos = list(photos or [])
        self._i = max(0, min(len(self._photos) - 1, int(index or 0)))
        self._title = title
        self.setStyleSheet(wall_qss(getattr(parent, 'pet', None)))
        self.setFocusPolicy(Qt.StrongFocus)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        self.lb_info = QLabel('')
        self.lb_info.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lb_info)
        self.view = QLabel('')
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setMinimumSize(320, 240)
        self.view.setStyleSheet('QLabel { background:%s; border-radius:8px; }' % _tk_of(
            getattr(parent, 'pet', None), 'ui_input_bg'))
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.btn_prev = QPushButton('‹ 上一张')
        self.btn_next = QPushButton('下一张 ›')
        self.btn_open = QPushButton('在资源管理器打开')
        self.btn_close = QPushButton('关闭')
        for b in (self.btn_prev, self.btn_next):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.btn_prev.clicked.connect(lambda: self.nav(-1))
        self.btn_next.clicked.connect(lambda: self.nav(1))
        self.btn_close.clicked.connect(self.close)
        self.btn_open.clicked.connect(self._open_folder)
        _OPEN_DIALOGS.append(self)
        self.apply_theme = lambda: self.setStyleSheet(wall_qss(getattr(parent, 'pet', None)))
        self._render()

    def _render(self):
        if not self._photos:
            self.lb_info.setText('（没有照片）')
            self.view.setText('')
            return
        photo = self._photos[self._i]
        rel = photo.get('file')
        src = _abs(rel)
        pm = QPixmap()
        if src and os.path.isfile(src):
            pm = QPixmap(src)
        if pm.isNull():
            pm = _THUMBS.pixmap(rel)
        if not pm.isNull():
            box = self.view.size() - QSize(8, 8)
            self.view.setPixmap(pm.scaled(box, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        else:
            self.view.setText('这张读不出来')
        name = str(photo.get('name') or '') or os.path.basename(str(rel or ''))
        cap = str(photo.get('caption') or '').strip()
        self.lb_info.setText('第 %d / %d 张　%s%s' % (self._i + 1, len(self._photos), name,
                                                     ('　·　' + cap) if cap else ''))
        self.btn_prev.setEnabled(self._i > 0)
        self.btn_next.setEnabled(self._i < len(self._photos) - 1)

    def nav(self, step):
        if not self._photos:
            return
        self._i = max(0, min(len(self._photos) - 1, self._i + int(step)))
        self._render()

    def _open_folder(self):
        if self._photos:
            src = _abs(self._photos[self._i].get('file'))
            if src and os.path.isfile(src):
                try:
                    import pet_convert as cv
                    cv.open_folder(os.path.dirname(src))
                except Exception:
                    pass

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._render()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_Escape:
            self.close()
            return
        if k in (Qt.Key_Left, Qt.Key_PageUp):
            self.nav(-1)
            return
        if k in (Qt.Key_Right, Qt.Key_PageDown, Qt.Key_Space):
            self.nav(1)
            return
        super().keyPressEvent(ev)


# ---------------------------------------------------------------- 单次见面
class MeetingDialog(QDialog):
    """一次见面：上面能改日期/标题/地点/便签，下面是拍立得贴墙"""

    def __init__(self, lib, mid, parent=None, pet=None, on_saved=None):
        super(MeetingDialog, self).__init__(parent)
        self.lib = lib
        self.mid = mid
        self.pet = pet
        self.on_saved = on_saved
        _bind_lib(lib)
        pwin.add_hide_button(self)      # v6.90：标题栏「—」隐藏键
        self.setAcceptDrops(True)
        self.resize(760, 620)
        self.setStyleSheet(wall_qss(pet))
        _OPEN_DIALOGS.append(self)

        m = lib.get(mid) or {}
        self.setWindowTitle('📷 ' + (m.get('title') or '见面'))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        top = QHBoxLayout()
        self.ed_date = QLineEdit(m.get('date') or ph.today_str())
        self.ed_date.setFixedWidth(96)
        self.ed_date.setPlaceholderText('YYYY-MM-DD')
        self.ed_date.setToolTip('这次见面的日期（改完点保存）')
        top.addWidget(QLabel('日期'))
        top.addWidget(self.ed_date)
        self.ed_title = QLineEdit(m.get('title') or '')
        self.ed_title.setPlaceholderText('给这次见面起个名字，例如「第一次一起看电影」')
        top.addWidget(QLabel('标题'))
        top.addWidget(self.ed_title, 1)
        lay.addLayout(top)

        top2 = QHBoxLayout()
        self.ed_place = QLineEdit(m.get('place') or '')
        self.ed_place.setPlaceholderText('在哪儿（可选）')
        self.ed_mood = QLineEdit(m.get('mood') or '')
        self.ed_mood.setPlaceholderText('那天的心情（可选）')
        top2.addWidget(QLabel('地点'))
        top2.addWidget(self.ed_place, 1)
        top2.addWidget(QLabel('心情'))
        top2.addWidget(self.ed_mood, 1)
        lay.addLayout(top2)

        self.ed_note = QTextEdit(m.get('note') or '')
        self.ed_note.setPlaceholderText('想记住的细节（可选）')
        self.ed_note.setFixedHeight(58)
        lay.addWidget(self.ed_note)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.pol = PolaroidView()
        self.pol.photoDoubleClicked.connect(self._lightbox)
        self.pol.contextAsked.connect(self._photo_menu)
        self.scroll.setWidget(self.pol)
        _tint_viewport(self.scroll, self.pet)   # setWidget 会把 viewport 底色重置，所以放在它之后
        lay.addWidget(self.scroll, 1)
        self.setAcceptDrops(True)

        row = QHBoxLayout()
        self.lb_count = QLabel('')
        row.addWidget(self.lb_count)
        row.addStretch(1)
        self.btn_add = QPushButton('📂 添加照片')
        self.btn_light = QPushButton('🔍 看大图')
        self.btn_open = QPushButton('📁 打开文件夹')
        self.btn_save = QPushButton('💾 保存')
        self.btn_del = QPushButton('🗑 删掉这次见面')
        for b in (self.btn_add, self.btn_light, self.btn_open, self.btn_save, self.btn_del):
            row.addWidget(b)
        lay.addLayout(row)
        self.btn_add.clicked.connect(self._add_photos)
        self.btn_light.clicked.connect(lambda: self._lightbox(self._selected_index()))
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_save.clicked.connect(lambda: self._save(close=False))
        self.btn_del.clicked.connect(self._delete)

        self._reload()

    # ---------- 主题 ----------
    def apply_theme(self):
        """主题换了：重刷样式表，并重新给滚动区上色（不然中间又变回系统白底）"""
        self.setStyleSheet(wall_qss(self.pet))
        _tint_viewport(getattr(self, 'scroll', None), self.pet)

    # ---------- 数据 ----------
    def _reload(self):
        m = self.lib.get(self.mid) or {}
        photos = ph.meeting_photos(m)
        # 相纸上默认写「这次见面的日期」——拍立得就该有那天的日期
        self.pol.set_photos(photos, ph.pretty_date(m.get('date')))
        n_files = sum(1 for p in photos if os.path.isfile(_abs(p.get('file'))))
        missing = len(photos) - n_files
        txt = '共 %d 张照片' % len(photos)
        if missing:
            txt += '（%d 张找不到文件了）' % missing
        self.lb_count.setText(txt)
        if not self.ed_title.text().strip():
            self.ed_title.setText(m.get('title') or '')

    def _selected_index(self):
        rel = self.pol.selected_rel()
        if not rel:
            return 0
        for i, p in enumerate(self.pol._photos):
            if p.get('file') == rel:
                return i
        return 0

    # ---------- 操作 ----------
    def _save(self, close=False):
        date = ph.norm_date(self.ed_date.text())
        if not date:
            m = self.lib.get(self.mid) or {}
            date = ph.norm_date(m.get('date')) or ph.today_str()
            self.ed_date.setText(date)
        self.lib.update_meeting(self.mid, date=date,
                                title=self.ed_title.text().strip(),
                                place=self.ed_place.text().strip(),
                                mood=self.ed_mood.text().strip(),
                                note=self.ed_note.toPlainText().strip())
        self.setWindowTitle('📷 ' + (self.ed_title.text().strip() or '见面'))
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception:
                pass
        if close:
            self.close()

    def _add_photos(self):
        files, _f = QFileDialog.getOpenFileNames(self, '选照片（可多选）', '',
                                                 '图片 (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff *.heic *.avif);;所有文件 (*)')
        if not files:
            return
        res = self.lib.add_photos(self.mid, files)
        self._reload()
        self._report(res)

    def _report(self, res):
        bits = []
        if res.get('added'):
            bits.append('加了 %d 张' % res['added'])
        if res.get('dups'):
            bits.append('%d 张已经在墙上了' % res['dups'])
        if res.get('skipped'):
            bits.append('%d 个跳过（%s）' % (len(res['skipped']), res['skipped'][0][1]))
        if res.get('error'):
            bits.append(res['error'])
        if bits:
            self.lb_count.setText(self.lb_count.text() + '　·　' + '，'.join(bits))

    def _lightbox(self, index):
        photos = ph.meeting_photos(self.lib.get(self.mid) or {})
        if not photos:
            return
        dlg = PhotoLightbox(photos, index, title=self.ed_title.text().strip(), parent=self)
        dlg.exec()

    def _open_folder(self):
        m = self.lib.get(self.mid) or {}
        rel = ph.meeting_photos(m)[0].get('file') if ph.meeting_photos(m) else ''
        target = os.path.dirname(_abs(rel)) if rel else self.lib.photos_dir
        try:
            import pet_convert as cv
            cv.open_folder(target)
        except Exception:
            pass

    def _delete(self):
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle('删掉这次见面')
        box.setText('删掉「%s」？' % (self.ed_title.text().strip() or '这次见面'))
        box.setInformativeText('会把它在照片墙里的 %d 张照片副本一起删掉（你自己电脑里的原图不动）。'
                               % len(ph.meeting_photos(self.lib.get(self.mid) or {})))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        self.lib.delete_meeting(self.mid, delete_files=True)
        if callable(self.on_saved):
            try:
                self.on_saved()
            except Exception:
                pass
        self.close()

    def _photo_menu(self, index, gpos):
        photos = ph.meeting_photos(self.lib.get(self.mid) or {})
        if not (0 <= index < len(photos)):
            return
        rel = photos[index].get('file')
        menu = QMenu(self)
        act_big = menu.addAction('🔍 看大图')
        act_folder = menu.addAction('📁 在资源管理器打开')
        menu.addSeparator()
        act_rm = menu.addAction('🗑 从这次见面移除')
        picked = menu.exec(gpos)
        if picked == act_big:
            self._lightbox(index)
        elif picked == act_folder:
            src = _abs(rel)
            try:
                import pet_convert as cv
                cv.open_folder(os.path.dirname(src))
            except Exception:
                pass
        elif picked == act_rm:
            self.lib.remove_photo(self.mid, rel, delete_file=True)
            self._reload()
            if callable(self.on_saved):
                try:
                    self.on_saved()
                except Exception:
                    pass

    # ---------- 拖放 ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super(MeetingDialog, self).dragEnterEvent(e)

    def dropEvent(self, e):
        # QMimeData 必须由调用方保活，否则 GC 后野指针 → access violation（踩过）
        md = e.mimeData()
        paths = []
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if p and (os.path.isfile(p) or os.path.isdir(p)):
                        paths.append(p)
        if paths:
            res = self.lib.add_photos(self.mid, paths)
            self._reload()
            self._report(res)
            e.accept()
            return
        super(MeetingDialog, self).dropEvent(e)

    def closeEvent(self, ev):
        try:
            self._save(close=False)
        except Exception:
            pass
        super(MeetingDialog, self).closeEvent(ev)


# ---------------------------------------------------------------- 墙面面板
class WallPanel(QDialog):
    """见面照片墙总览：拖照片进来 → 按天自动分组成一次次见面 → 点开看那天的拍立得"""

    def __init__(self, ctrl, parent=None):
        super(WallPanel, self).__init__(parent)
        self.ctrl = ctrl
        self.lib = ctrl.lib
        self.pet = getattr(ctrl, 'pet', None)
        self._query = ''
        _bind_lib(self.lib)
        pwin.add_hide_button(self)      # v6.90：标题栏「—」隐藏键
        self.setWindowTitle('📷 见面照片墙')
        self.resize(900, 680)
        self.setAcceptDrops(True)
        self.setStyleSheet(wall_qss(self.pet))
        _OPEN_DIALOGS.append(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        self.lb_stats = QLabel('')
        lay.addWidget(self.lb_stats)

        row = QHBoxLayout()
        self.btn_add = QPushButton('📂 添加照片')
        self.btn_new = QPushButton('＋ 新建一次见面')
        self.btn_next = QPushButton('📅 下次见面')
        self.btn_small = QPushButton('A－')
        self.btn_big = QPushButton('A＋')
        for b in (self.btn_add, self.btn_new, self.btn_next):
            row.addWidget(b)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText('搜「标题 / 地点 / 便签 / 日期」…')
        self.ed_search.setClearButtonEnabled(True)
        row.addWidget(self.ed_search, 1)
        row.addWidget(self.btn_small)
        row.addWidget(self.btn_big)
        lay.addLayout(row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.wall = WallView()
        self.scroll.setWidget(self.wall)
        _tint_viewport(self.scroll, self.pet)   # setWidget 会把 viewport 底色重置，所以放在它之后
        lay.addWidget(self.scroll, 1)

        self.lb_hint = QLabel('拖照片（或装着照片的文件夹）进窗口就能加；单击照片看大图，'
                              '双击照片打开那次见面，点日期那行也能打开，右键有更多')
        self.lb_hint.setWordWrap(True)
        lay.addWidget(self.lb_hint)

        self.btn_add.clicked.connect(self.ctrl.pick_and_import)
        self.btn_new.clicked.connect(self._new_meeting)
        self.btn_next.clicked.connect(self._edit_next)
        self.btn_small.clicked.connect(lambda: self._zoom(-20))
        self.btn_big.clicked.connect(lambda: self._zoom(20))
        self.ed_search.textChanged.connect(self._on_search)
        self.wall.meetingClicked.connect(self._open_meeting)
        self.wall.photoClicked.connect(self._lightbox_at)
        self.wall.photoActivated.connect(lambda mid, _rel: self._open_meeting(mid))
        self.wall.contextAsked.connect(self._photo_menu)
        self.refresh()

    # ---------- 刷新 ----------
    def refresh(self):
        try:
            meetings = self.lib.search(self._query) if self._query else self.lib.meetings()
            self.wall.set_meetings(meetings)
            self.lb_stats.setText('📷 ' + self.lib.stats_line())
        except Exception as exc:
            self.lb_stats.setText('照片墙读不出来：%s' % str(exc)[:80])

    def _on_search(self, text):
        self._query = str(text or '').strip()
        self.refresh()

    def _zoom(self, delta):
        self.wall.set_target_h(self.wall._target_h + int(delta))
        ph.cfg_write(ph.CFG_TARGET_H, self.wall._target_h)

    # ---------- 操作 ----------
    def _open_meeting(self, mid):
        if not mid:
            return
        dlg = MeetingDialog(self.lib, mid, parent=self, pet=self.pet,
                            on_saved=self._after_change)
        dlg.show()
        return dlg

    def _after_change(self):
        self.refresh()
        ctrl = self.ctrl
        try:
            ctrl.notify_changed()
        except Exception:
            pass

    def _lightbox_at(self, mid, rel):
        m = self.lib.get(mid) or {}
        photos = ph.meeting_photos(m)
        idx = 0
        for i, p in enumerate(photos):
            if p.get('file') == rel:
                idx = i
                break
        if photos:
            PhotoLightbox(photos, idx, title=str(m.get('title') or ''), parent=self).show()

    def _new_meeting(self):
        m = self.lib.create_meeting(ph.today_str())
        dlg = self._open_meeting(m.get('id'))
        self.refresh()
        try:
            self.ctrl.notify_changed()
        except Exception:
            pass
        return dlg

    def _edit_next(self):
        dlg = NextMeetingDialog(self.lib, parent=self, pet=self.pet)
        if dlg.exec():
            self.refresh()
            try:
                self.ctrl.notify_changed()
            except Exception:
                pass

    def _photo_menu(self, payload, gpos):
        mid = (payload or {}).get('mid')
        rel = (payload or {}).get('rel')
        if not mid or not rel:
            return
        menu = QMenu(self)
        act_open = menu.addAction('📷 打开这次见面')
        act_big = menu.addAction('🔍 看大图')
        act_folder = menu.addAction('📁 在资源管理器打开')
        menu.addSeparator()
        act_rm = menu.addAction('🗑 从这次见面移除')
        picked = menu.exec(gpos)
        if picked == act_open:
            self._open_meeting(mid)
        elif picked == act_big:
            self._lightbox_at(mid, rel)
        elif picked == act_folder:
            try:
                import pet_convert as cv
                cv.open_folder(os.path.dirname(_abs(rel)))
            except Exception:
                pass
        elif picked == act_rm:
            self.lib.remove_photo(mid, rel, delete_file=True)
            self.refresh()
            try:
                self.ctrl.notify_changed()
            except Exception:
                pass

    # ---------- 主题 ----------
    def apply_theme(self):
        """主题换了：重刷样式表，并重新给滚动区上色（不然中间又变回系统白底）"""
        self.setStyleSheet(wall_qss(self.pet))
        _tint_viewport(getattr(self, 'scroll', None), self.pet)

    # ---------- 拖放 ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super(WallPanel, self).dragEnterEvent(e)

    def dropEvent(self, e):
        md = e.mimeData()          # 保活（QMimeData 被 GC 会崩）
        paths = []
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if p and (os.path.isfile(p) or os.path.isdir(p)):
                        paths.append(p)
        if paths:
            self.ctrl.import_files(paths)
            e.accept()
            return
        super(WallPanel, self).dropEvent(e)


# ---------------------------------------------------------------- 下次见面 / 倒计时
class NextMeetingDialog(QDialog):
    """设「下次见面」的日子（倒计时用）。日期清空 = 不设。"""

    def __init__(self, lib, parent=None, pet=None):
        super(NextMeetingDialog, self).__init__(parent)
        self.lib = lib
        pwin.add_hide_button(self)      # v6.90：标题栏「—」隐藏键
        self.setWindowTitle('📅 下次见面')
        self.setStyleSheet(wall_qss(pet))
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        cur = ph.norm_date(ph.cfg_text(ph.CFG_NEXT_DATE))
        lay.addWidget(QLabel('下次见面的日子（设了就显示倒计时；清掉日期 = 不设）'))
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat('yyyy-MM-dd')
        from PySide6.QtCore import QDate
        if cur:
            y, mo, d = [int(x) for x in cur.split('-')]
            self.date_edit.setDate(QDate(y, mo, d))
        else:
            self.date_edit.setDate(QDate.currentDate())
        lay.addWidget(self.date_edit)
        lay.addWidget(QLabel('备注（可选，例如「去植物园」）'))
        self.ed_note = QLineEdit(ph.cfg_text(ph.CFG_NEXT_NOTE))
        lay.addWidget(self.ed_note)
        self.lb_tip = QLabel('')
        lay.addWidget(self.lb_tip)
        row = QHBoxLayout()
        self.btn_clear = QPushButton('清掉')
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        self.btn_ok = QPushButton('保存')
        self.btn_cancel = QPushButton('取消')
        row.addWidget(self.btn_ok)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)
        self.btn_clear.clicked.connect(self._clear)
        self.btn_ok.clicked.connect(self._save)
        self.btn_cancel.clicked.connect(self.reject)
        self.date_edit.dateChanged.connect(lambda *_: self._update_tip())
        self._update_tip()

    def _update_tip(self):
        d = self.date_edit.date().toString('yyyy-MM-dd')
        n = ph.days_between(ph.today_str(), d)
        if n is None:
            self.lb_tip.setText('')
        elif n > 0:
            self.lb_tip.setText('距今天还有 %d 天' % n)
        elif n == 0:
            self.lb_tip.setText('就是今天！')
        else:
            self.lb_tip.setText('这个日子已经过了 %d 天' % (-n))

    def _save(self):
        ph.cfg_write(ph.CFG_NEXT_DATE, self.date_edit.date().toString('yyyy-MM-dd'))
        ph.cfg_write(ph.CFG_NEXT_NOTE, self.ed_note.text().strip())
        self.accept()

    def _clear(self):
        ph.cfg_delete(ph.CFG_NEXT_DATE)
        ph.cfg_delete(ph.CFG_NEXT_NOTE)
        self.accept()


# ---------------------------------------------------------------- 桌宠旁边的小卡片
class PhotoCard(QWidget):
    """贴在桌宠边上的小卡片：最近几张 + 倒计时，点一下打开整面墙"""

    W, H = 232, 208

    def __init__(self, ctrl, parent=None):
        super(PhotoCard, self).__init__(parent)
        self.ctrl = ctrl
        self.pet = getattr(ctrl, 'pet', None)
        _bind_lib(ctrl.lib)
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(self.W, self.H)
        self.setStyleSheet(card_qss(self.pet))
        self._drag_off = None
        self._thumbs = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        head = QHBoxLayout()
        self.lb_title = QLabel('📷 见面')
        self.lb_title.setStyleSheet('color:%s; font-size:12px; font-weight:bold;'
                                    % _tk_of(self.pet, 'ui_photo_header'))
        head.addWidget(self.lb_title)
        head.addStretch(1)
        self.btn_close = QLabel('✕')
        self.btn_close.setStyleSheet('color:%s; font-size:12px;' % _tk_of(self.pet, 'ui_text_dim'))
        self.btn_close.setCursor(Qt.PointingHandCursor)
        self.btn_close.mousePressEvent = lambda e: self.hide()
        head.addWidget(self.btn_close)
        lay.addLayout(head)

        shots = QHBoxLayout()
        shots.setSpacing(4)
        self.shots = []
        for _i in range(3):
            lb = QLabel('')
            lb.setFixedSize(66, 66)
            lb.setAlignment(Qt.AlignCenter)
            lb.setStyleSheet('QLabel { background:%s; border-radius:6px; }'
                             % _tk_of(self.pet, 'ui_photo_empty'))
            lb.setCursor(Qt.PointingHandCursor)
            lb.mousePressEvent = lambda e: self.ctrl.open_wall()
            shots.addWidget(lb)
            self.shots.append(lb)
        shots.addStretch(1)
        lay.addLayout(shots)

        self.lb_stats = QLabel('')
        self.lb_stats.setWordWrap(True)
        self.lb_stats.setStyleSheet('color:%s; font-size:11px;' % _tk_of(self.pet, 'ui_text_dim'))
        lay.addWidget(self.lb_stats)

        self.lb_down = QLabel('')
        self.lb_down.setWordWrap(True)
        self.lb_down.setStyleSheet('color:%s; font-size:12px; font-weight:bold;'
                                   % _tk_of(self.pet, 'ui_accent_soft'))
        lay.addWidget(self.lb_down)
        lay.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.btn_add = QPushButton('＋ 照片')
        self.btn_open = QPushButton('打开墙面')
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_open)
        lay.addLayout(row)
        self.btn_add.clicked.connect(self.ctrl.pick_and_import)
        self.btn_open.clicked.connect(self.ctrl.open_wall)
        self.refresh()

    # ---------- 数据 ----------
    def refresh(self):
        lib = self.ctrl.lib
        st = lib.stats()
        self.lb_stats.setText(lib.stats_line())
        d = st.get('days_to_next')
        if d is None:
            self.lb_down.setText('设个下次见面的日子吧（📅）')
        elif d > 0:
            self.lb_down.setText('距下次见面还有 %d 天%s'
                                 % (d, ('　·　' + st.get('next_note')) if st.get('next_note') else ''))
        elif d == 0:
            self.lb_down.setText('就是今天见面！')
        else:
            self.lb_down.setText('约好的日子已过 %d 天' % (-d))
        rels = []
        for m in lib.meetings():
            for p in ph.meeting_photos(m):
                rels.append(p.get('file'))
            if len(rels) >= 3:
                break
        rels = rels[:3]
        for i, lb in enumerate(self.shots):
            if i < len(rels):
                pm = _THUMBS.pixmap(rels[i])
                if not pm.isNull():
                    lb.setPixmap(pm.scaled(QSize(66, 66), Qt.KeepAspectRatioByExpanding,
                                           Qt.SmoothTransformation))
                    lb.setToolTip(rels[i])
                    continue
            lb.clear()

    def apply_theme(self):
        self.setStyleSheet(card_qss(self.pet))
        self.lb_title.setStyleSheet('color:%s; font-size:12px; font-weight:bold;'
                                    % _tk_of(self.pet, 'ui_photo_header'))
        self.btn_close.setStyleSheet('color:%s; font-size:12px;' % _tk_of(self.pet, 'ui_text_dim'))
        self.lb_stats.setStyleSheet('color:%s; font-size:11px;' % _tk_of(self.pet, 'ui_text_dim'))
        self.lb_down.setStyleSheet('color:%s; font-size:12px; font-weight:bold;'
                                   % _tk_of(self.pet, 'ui_accent_soft'))
        self.update()

    # ---------- 画个圆角纸片 ----------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        # 卡片底下用的是「悬浮窗」那套底色（近乎不透明），叠在壁纸/桌宠上也看得清
        p.setBrush(_qc('ui_stats_bg'))
        p.setPen(QPen(_qc('ui_photo_paper_edge'), 1))
        p.drawRoundedRect(box, 12, 12)

    # ---------- 拖动 / 记忆位置 ----------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_off = ev.globalPos() - self.frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):
        if self._drag_off is not None and ev.buttons() & Qt.LeftButton:
            self.move(ev.globalPos() - self._drag_off)
            ev.accept()

    def mouseReleaseEvent(self, ev):
        if self._drag_off is not None:
            self._drag_off = None
            try:
                ph.cfg_write(ph.CFG_CARD_POS, [int(self.x()), int(self.y())])
            except Exception:
                pass
            ev.accept()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Escape:
            self.hide()
            return
        super(PhotoCard, self).keyPressEvent(ev)


# ---------------------------------------------------------------- 控制器
class PhotoController(QObject):
    """照片墙控制器：懒创建面板/卡片，导入照片，导入后通知宿主（写回忆 + 加好感）"""

    notice = Signal(str)

    def __init__(self, pet=None, base_dir=None):
        super(PhotoController, self).__init__()
        self.pet = pet
        self.lib = ph.PhotoLibrary(base_dir) if base_dir else ph.default_library()
        _bind_lib(self.lib)
        self.on_meetings = None      # 宿主挂的回调：新见面/导入完 → 记一条回忆 + 加点好感
        self._panel = None
        self._card = None

    # ---------- 窗口 ----------
    def panel(self, files=None):
        if self._panel is None:
            self._panel = WallPanel(self)
        pwin.present(self._panel)       # v6.90：隐藏着的面板要能一键叫回来
        if files:
            self.import_files(files)
        else:
            self._panel.refresh()
        return self._panel

    def open_wall(self):
        return self.panel()

    def card(self):
        """桌宠旁边那张小卡片：没有就造，藏着的就翻出来"""
        if self._card is None:
            self._card = PhotoCard(self)
            self._place_card()
        self._card.refresh()
        if self._card.isVisible():
            self._card.hide()
        else:
            self._card.show()
            self._card.raise_()
            self._card.activateWindow()
            QTimer.singleShot(700, self._card.refresh)   # 缩略图是后台生成的，过一会儿再刷一次
        return self._card

    def _place_card(self):
        """第一次出来贴着桌宠放（用户拖过就记位置，以后按记忆的位置）"""
        card = self._card
        if not card:
            return
        try:
            pos = ph.cfg_read().get(ph.CFG_CARD_POS)
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                card.move(int(pos[0]), int(pos[1]))
                return
        except Exception:
            pass
        try:
            geo = self.pet.frameGeometry() if self.pet is not None else None
            if geo is not None and geo.width() > 0:
                scr = None
                try:
                    scr = self.pet.screen().availableGeometry() if self.pet.screen() else None
                except Exception:
                    scr = None
                cw, ch = card.width(), card.height()
                # 优先放桌宠左边；左边出屏幕就放右边；都不行就贴上方（桌宠可能贴着屏幕边）
                cands = [(geo.x() - cw - 16, geo.y() + 40),
                         (geo.x() + geo.width() + 16, geo.y() + 40),
                         (geo.x(), geo.y() - ch - 16)]
                for x, y in cands:
                    if scr is not None:
                        if x < scr.left() or x + cw > scr.right() or y < scr.top() or y + ch > scr.bottom():
                            continue
                    card.move(max(0, x), max(0, y))
                    return
                x = max(0, geo.x() - cw - 16)
                card.move(x, max(0, geo.y() + 40))
                return
        except Exception:
            pass
        card.move(80, 80)

    def meeting(self, mid):
        """打开某一次见面（墙面里点标题走这里）"""
        p = self.panel()
        return p._open_meeting(mid)

    # ---------- 导入 ----------
    def pick_and_import(self):
        """点按钮选照片/文件夹（一次选一堆，自动按天分组）"""
        from PySide6.QtWidgets import QFileDialog
        parent = self._panel or (self._card if self._card is not None else None)
        files, _f = QFileDialog.getOpenFileNames(
            parent, '选照片（可多选；也可以直接拖进窗口）', '',
            '图片 (*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff *.heic *.avif);;所有文件 (*)')
        if files:
            return self.import_files(files)
        return None

    def import_files(self, paths, date=''):
        """导入照片（复制进 photos/<日期>/），完成后通知宿主"""
        try:
            res = self.lib.import_paths(paths, date=date)
        except Exception as exc:
            self.notice.emit('📷 导入失败：%s' % str(exc)[:100])
            return None
        bits = []
        if res.get('added'):
            bits.append('加了 %d 张照片' % res['added'])
        if res.get('dups'):
            bits.append('%d 张之前就在了' % res['dups'])
        if res.get('skipped'):
            bits.append('%d 个没用上（%s）' % (len(res['skipped']), res['skipped'][0][1]))
        if res.get('over'):
            bits.append('数量太多，只处理了前 %d 个' % ph.MAX_IMPORT)
        if not bits:
            bits.append(res.get('error') or '没找到能加的照片')
        self.notice.emit('📷 ' + '，'.join(bits))
        self.notify_changed(res.get('meetings') or [])
        return res

    def notify_changed(self, mids=None):
        """数据变了：面板/卡片重新读，再让宿主记回忆 + 加好感"""
        try:
            if self._panel is not None and self._panel.isVisible():
                self._panel.refresh()
        except Exception:
            pass
        try:
            if self._card is not None and self._card.isVisible():
                self._card.refresh()
        except Exception:
            pass
        if mids and callable(self.on_meetings):
            try:
                self.on_meetings(mids, self.lib)
            except TypeError:
                try:
                    self.on_meetings(mids)      # 兼容只收一个参数的宿主
                except Exception:
                    pass
            except Exception:
                pass
