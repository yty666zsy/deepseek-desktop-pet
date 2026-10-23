# -*- coding: utf-8 -*-
"""pet_ledger_ui.py — 「记账」界面（v6.89，使用者拍板 1A 2A 3A 4A 5A 6A）

分工：
    pet_ledger.py     内核：ledger.json / 本地语句解析 / 统计 / 预算 / 导出（纯逻辑、无 Qt）
    本模块             界面：账本面板（图表 + 明细）、记一笔对话框、预算对话框、控制器

为什么图表全自绘
----------------
拍板 5A：分类环形 + 近 6 月柱状 + 明细表。图表库（pyqtgraph / matplotlib）会给便携包
加十几 MB 和一堆 Qt 绑定坑，而这三种图 QPainter 各几十行就能画完 —— 跟照片墙、数独
一个路子（`tools/DeepSeekPet.spec` 里 WebEngine/QtQml 早已 excludes，不能回头）。

规矩（跟别的面板一套）：
- 颜色一律走 pet_theme token（tests/test_theme_tokens.py 会扫硬编码颜色，本文件不许出现
  十六进制色值或 rgba 函数写法；用到的 token 在 LEDGER_TOKENS 里列全，测试会校验其存在）
- 面板懒创建：点了才读 ledger.json
- 金额方向用颜色说话：支出 `ui_red`、收入 `ui_green`、图表配色从 accent/gold/blue 里轮
- 记账高频但不该打断：确认信息走桌宠头顶气泡（不用模态框弹窗）
"""

import os

from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDateEdit, QDialog,
                               QDoubleSpinBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import QDate

import pet_ledger as lg
import pet_theme as _pt
import pet_window as pwin  # v6.90 每个窗口标题栏的「—」隐藏键

T = _pt.color

# 本界面用到的 token（测试会校验它们都在 pet_theme 里真存在）
LEDGER_TOKENS = ('ui_bg', 'ui_text', 'ui_text_dim', 'ui_text_strong', 'ui_text_muted',
                 'ui_input_bg', 'ui_border_soft', 'ui_btn_bg', 'ui_btn_hover', 'ui_btn_alt',
                 'ui_accent', 'accent', 'ui_gold', 'ui_red', 'ui_green', 'ui_blue',
                 'ui_accent_soft', 'ui_popup_bg', 'ui_popup_btn_hover', 'ui_stats_bg',
                 'list_bg', 'list_border', 'item_bg', 'input_text')

# 环形图分段配色（都是主题 token，切主题自动跟着变；顺序＝从多到少）
SLICE_TOKENS = ('ui_accent', 'ui_gold', 'ui_green', 'ui_blue', 'ui_red', 'ui_accent_soft',
                'ui_text_dim')

MAX_TABLE_ROWS = 400        # 明细表最多显示多少行（再多就靠筛选，别让表格卡住）
_OPEN_DIALOGS = []          # 打开着的面板（主题变了要重刷）
_QC_CACHE = {}


# ---------------------------------------------------------------- 颜色
def _qc(key, alpha=None):
    """token → QColor（token 值可能是纯色，也可能是带透明度的 rgba 写法）"""
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
    """宿主有 _tk 就用宿主的（切主题时它自己管），没有就退回唯一源"""
    try:
        if pet is not None:
            return pet._tk(key)
    except Exception:
        pass
    return T(key)


def kind_color(kind):
    """支出红、收入绿（全界面统一这一处，改色只改这里）"""
    return _qc('ui_green' if kind == 'income' else 'ui_red')


def _short_money(v):
    """图表上的短金额：12000 → 1.2万；3200 → 3200"""
    v = float(v or 0)
    if abs(v) >= 10000:
        return '%.1f万' % (v / 10000.0)
    if abs(v) >= 1000:
        return '%d' % int(round(v))
    if abs(v - round(v)) < 0.005:
        return '%d' % int(round(v))
    return '%.1f' % v


# ---------------------------------------------------------------- 样式
def ledger_qss(pet=None):
    """面板 / 对话框共用的一套控件样式（颜色全走 token）"""
    g = lambda k: _tk_of(pet, k)      # noqa: E731（只是取值别名）
    return (
        "QDialog { background:%s; }" % g('ui_bg')
        + "QLabel { color:%s; font-size:12px; background:transparent; }" % g('ui_text')
        + "QFrame#lgCard { background:%s; border:1px solid %s; border-radius:10px; }"
          % (g('ui_input_bg'), g('ui_border_soft'))
        + "QPushButton { background:%s; color:%s; border:none; border-radius:6px;"
          " padding:6px 12px; font-size:12px; }" % (g('ui_btn_bg'), g('ui_text_strong'))
        + "QPushButton:hover { background:%s; }" % g('ui_btn_hover')
        + "QPushButton:disabled { background:%s; color:%s; }" % (g('ui_btn_alt'), g('ui_text_dim'))
        + "QPushButton#lgPrimary { background:%s; color:%s; font-weight:bold; }"
          % (g('ui_accent'), g('ui_bg'))
        + "QPushButton#lgPrimary:hover { background:%s; }" % g('accent')
        + "QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background:%s; color:%s;"
          " border:1px solid %s; border-radius:6px; padding:4px 6px; font-size:12px; }"
          % (g('ui_input_bg'), g('input_text'), g('ui_border_soft'))
        + "QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QDoubleSpinBox:focus"
          " { border:1px solid %s; }" % g('ui_accent')
        + "QComboBox QAbstractItemView { background:%s; color:%s; selection-background-color:%s;"
          " outline:none; }" % (g('ui_popup_bg'), g('ui_text'), g('ui_popup_btn_hover'))
        + "QTableWidget { background:%s; color:%s; border:1px solid %s; border-radius:8px;"
          " gridline-color:%s; font-size:12px; }"
          % (g('ui_input_bg'), g('ui_text'), g('ui_border_soft'), g('ui_border_soft'))
        + "QTableWidget::item { padding:2px 6px; }"
        + "QTableWidget::item:selected { background:%s; color:%s; }"
          % (g('ui_popup_btn_hover'), g('ui_text_strong'))
        + "QTableCornerButton::section { background:%s; border:none; }" % g('ui_btn_bg')
        + "QHeaderView { background:%s; border:none; }" % g('ui_btn_bg')
        + "QHeaderView::section { background:%s; color:%s; border:none;"
          " border-bottom:1px solid %s; padding:4px 6px; font-size:11px; }"
          % (g('ui_btn_bg'), g('ui_text_dim'), g('ui_border_soft'))
        + "QScrollBar:vertical { background:%s; width:10px; margin:2px; }"
          % g('ui_input_bg')
        + "QScrollBar::handle:vertical { background:%s; border-radius:5px; min-height:28px; }"
          % g('ui_btn_bg')
        + "QScrollBar::handle:vertical:hover { background:%s; }" % g('ui_btn_hover')
        + "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
        + "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }"
        + "QScrollBar:horizontal { background:%s; height:10px; margin:2px; }"
          % g('ui_input_bg')
        + "QScrollBar::handle:horizontal { background:%s; border-radius:5px; min-width:28px; }"
          % g('ui_btn_bg')
        + "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }"
        + "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:transparent; }"
        + "QMenu { background:%s; color:%s; border:1px solid %s; }"
          % (g('ui_popup_bg'), g('ui_text'), g('ui_border_soft'))
        + "QMenu::item { padding:5px 18px; }"
        + "QMenu::item:selected { background:%s; }" % g('ui_popup_btn_hover')
    )


def _tint_viewport(scroll, pet=None):
    """给滚动区 viewport 上色（QScrollArea 的 viewport 走 QPalette.Base，样式表管不到）"""
    try:
        from PySide6.QtGui import QPalette
        col = _qc('ui_input_bg')
        for w in (scroll.viewport(), scroll.widget()):
            if w is None:
                continue
            pal = w.palette()
            pal.setColor(QPalette.Window, col)
            pal.setColor(QPalette.Base, col)
            w.setPalette(pal)
            w.setAutoFillBackground(True)
    except Exception:
        pass


def refresh_open():
    """主题变化时重刷已打开的账本窗口"""
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


# ---------------------------------------------------------------- 图表：分类环形
class DonutChart(QWidget):
    """分类占比环形图（左环 + 右图例，一次画完，不用另建图例控件）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._total = 0.0
        self.setMinimumHeight(186)
        self.setMinimumWidth(300)

    def set_data(self, rows, total):
        self._rows = list(rows or [])
        self._total = float(total or 0)
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        side = max(60, min(H - 16, int(W * 0.42)))
        box = QRectF(10, (H - side) / 2.0, side, side)
        thick = max(12.0, side * 0.20)

        if not self._rows or self._total <= 0:
            p.setPen(QPen(_qc('ui_text_dim')))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(box.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2))
            f = p.font()
            f.setPointSizeF(9.5)
            p.setFont(f)
            p.drawText(QRectF(0, 0, W, H), int(Qt.AlignCenter), '本月还没支出')
            p.end()
            return

        ang = 90.0                     # 从 12 点方向开始，顺时针
        for i, r in enumerate(self._rows):
            span = 360.0 * float(r.get('amount') or 0) / self._total
            if span <= 0:
                continue
            col = _qc(SLICE_TOKENS[i % len(SLICE_TOKENS)])
            pen = QPen(col)
            pen.setWidthF(thick)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            # drawArc 用 1/16 度；-span 让方向顺时针
            p.drawArc(box.adjusted(thick / 2, thick / 2, -thick / 2, -thick / 2),
                      int(ang * 16), int(-span * 16))
            ang -= span

        # 环中心：本月支出合计
        f = p.font()
        f.setPointSizeF(8.0)
        p.setFont(f)
        p.setPen(QPen(_qc('ui_text_dim')))
        cbox = QRectF(box.x(), box.y() + side * 0.32, side, 18)
        p.drawText(cbox, int(Qt.AlignCenter), '本月支出')
        f.setPointSizeF(13.0)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(_qc('ui_red')))
        p.drawText(QRectF(box.x(), box.y() + side * 0.42, side, 26), int(Qt.AlignCenter),
                   lg.fmt_amount(self._total))

        # 图例
        f.setPointSizeF(8.5)
        f.setBold(False)
        p.setFont(f)
        lx = box.right() + 14
        lw = max(80, W - lx - 6)
        rows = self._rows[:7]
        ly = max(6.0, (H - len(rows) * 20) / 2.0)
        for i, r in enumerate(rows):
            col = _qc(SLICE_TOKENS[i % len(SLICE_TOKENS)])
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawEllipse(QRectF(lx, ly + i * 20 + 5, 8, 8))
            p.setPen(QPen(_qc('ui_text')))
            p.drawText(QRectF(lx + 14, ly + i * 20, lw - 46, 18), int(Qt.AlignLeft | Qt.AlignVCenter),
                       '%s %s' % (lg.cat_icon(r.get('cat')), r.get('cat')))
            p.setPen(QPen(_qc('ui_text_dim')))
            p.drawText(QRectF(lx + lw - 40, ly + i * 20, 40, 18),
                       int(Qt.AlignRight | Qt.AlignVCenter), '%.0f%%' % float(r.get('pct') or 0))
        if len(self._rows) > len(rows):
            p.setPen(QPen(_qc('ui_text_dim')))
            p.drawText(QRectF(lx + 14, ly + len(rows) * 20, lw - 14, 18),
                       int(Qt.AlignLeft | Qt.AlignVCenter), '还有 %d 类…' % (len(self._rows) - len(rows)))
        p.end()


# ---------------------------------------------------------------- 图表：近 6 月柱状
class BarChart(QWidget):
    """近 6 个月支出/收入柱状图（支出粗柱 + 收入细柱，看得见趋势就够）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = []
        self.setMinimumHeight(168)

    def set_data(self, series):
        self._series = list(series or [])
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 8, 8, 20, 18
        plot_h = max(20, H - pad_t - pad_b)
        if not self._series:
            p.setPen(QPen(_qc('ui_text_dim')))
            p.drawText(QRectF(0, 0, W, H), int(Qt.AlignCenter), '还没有数据')
            p.end()
            return

        peak = max([max(float(e or 0), float(i or 0)) for _, e, i in self._series] or [0])
        peak = peak if peak > 0 else 1.0
        n = len(self._series)
        slot = max(24.0, (W - pad_l - pad_r) / float(n))
        base_y = pad_t + plot_h
        # 顶上留出数字标签的地儿，否则最高那根的「8718」会顶出画面
        bar_h = plot_h - 14

        # 基线
        p.setPen(QPen(_qc('ui_border_soft'), 1))
        p.drawLine(int(pad_l), int(base_y), int(W - pad_r), int(base_y))

        f = p.font()
        f.setPointSizeF(8.0)
        p.setFont(f)
        for i, (month, exp, inc) in enumerate(self._series):
            cx = pad_l + slot * (i + 0.5)
            h_exp = bar_h * (float(exp or 0) / peak)
            h_inc = bar_h * (float(inc or 0) / peak)
            bw = min(20.0, slot * 0.42)

            # 支出主柱
            p.setPen(Qt.NoPen)
            p.setBrush(_qc('ui_red'))
            p.drawRoundedRect(QRectF(cx - bw / 2, base_y - h_exp, bw, max(1.0, h_exp)), 3, 3)
            # 收入细柱（贴右边，绿色）
            if inc:
                p.setBrush(_qc('ui_green'))
                p.drawRoundedRect(QRectF(cx + bw / 2 + 1, base_y - h_inc, bw * 0.45,
                                         max(1.0, h_inc)), 2, 2)

            if exp:
                p.setPen(QPen(_qc('ui_text_dim')))
                p.drawText(QRectF(cx - bw / 2 - 12, base_y - h_exp - 15, bw + 24, 13),
                           int(Qt.AlignCenter), _short_money(exp))
            p.setPen(QPen(_qc('ui_text' if i == n - 1 else 'ui_text_dim')))
            p.drawText(QRectF(cx - slot / 2, base_y + 3, slot, 14), int(Qt.AlignCenter),
                       '%d月' % int(str(month)[5:7]))
        p.end()


# ---------------------------------------------------------------- 预算条
class BudgetBars(QWidget):
    """每个设了限额的分类一条：绿=宽裕、金=快到了、红=超了"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self.setMinimumHeight(0)

    def set_data(self, states):
        self._rows = list(states or [])
        self.setMinimumHeight(max(0, len(self._rows) * 24 + 4))
        self.update()

    def paintEvent(self, ev):
        if not self._rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W = self.width()
        f = p.font()
        f.setPointSizeF(8.5)
        p.setFont(f)
        for i, b in enumerate(self._rows):
            y = i * 24 + 2
            pct = float(b.get('pct') or 0)
            if b.get('over'):
                col = _qc('ui_red')
            elif pct >= 80:
                col = _qc('ui_gold')
            else:
                col = _qc('ui_green')
            p.setPen(QPen(_qc('ui_text')))
            p.drawText(QRectF(0, y, 78, 18), int(Qt.AlignLeft | Qt.AlignVCenter),
                       '%s %s' % (lg.cat_icon(b.get('cat')), b.get('cat')))
            # 轨道
            tx, tw = 84.0, max(40.0, W - 84 - 118)
            p.setPen(Qt.NoPen)
            p.setBrush(_qc('ui_btn_alt'))
            p.drawRoundedRect(QRectF(tx, y + 5, tw, 8), 4, 4)
            fill = min(1.0, pct / 100.0)
            if fill > 0:
                p.setBrush(col)
                p.drawRoundedRect(QRectF(tx, y + 5, max(3.0, tw * fill), 8), 4, 4)
            p.setPen(QPen(col if b.get('over') else _qc('ui_text_dim')))
            txt = '%s / %s' % (lg.fmt_amount(b.get('spent')), lg.fmt_amount(b.get('limit')))
            p.drawText(QRectF(W - 120, y, 114, 18), int(Qt.AlignRight | Qt.AlignVCenter), txt)
        p.end()


# ---------------------------------------------------------------- 记一笔
class EntryDialog(QDialog):
    """一笔账的编辑框（记一笔 / 改一笔共用）"""

    def __init__(self, book, entry=None, parent=None, default_date=None):
        super().__init__(parent)
        self.book = book
        self.setWindowTitle('改一笔' if entry else '记一笔')
        pwin.add_hide_button(self)      # v6.90：标题栏「—」隐藏键
        self.setMinimumWidth(360)
        self.setStyleSheet(ledger_qss())
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 12)
        v.setSpacing(8)

        def row(label, w):
            h = QHBoxLayout()
            h.setSpacing(8)
            lb = QLabel(label, self)
            lb.setFixedWidth(52)
            h.addWidget(lb)
            h.addWidget(w, 1)
            v.addLayout(h)

        self.cb_kind = QComboBox(self)
        self.cb_kind.addItem('支出', 'expense')
        self.cb_kind.addItem('收入', 'income')
        row('收支', self.cb_kind)

        self.sp_amount = QDoubleSpinBox(self)
        self.sp_amount.setRange(0.01, 99999999.0)
        self.sp_amount.setDecimals(2)
        self.sp_amount.setSingleStep(1.0)
        self.sp_amount.setSuffix(' 元')
        self.sp_amount.setValue(0.0)
        row('金额', self.sp_amount)

        self.cb_cat = QComboBox(self)
        self.cb_cat.setEditable(True)
        row('分类', self.cb_cat)

        self.de_date = QDateEdit(self)
        self.de_date.setCalendarPopup(True)
        self.de_date.setDisplayFormat('yyyy-MM-dd')
        row('日期', self.de_date)

        self.cb_account = QComboBox(self)
        self.cb_account.setEditable(True)
        self.cb_account.addItem('')
        for a in lg.DEFAULT_ACCOUNTS:
            self.cb_account.addItem(a)
        row('账户', self.cb_account)

        self.ed_note = QLineEdit(self)
        self.ed_note.setPlaceholderText('备注（午饭 / 打车…）')
        row('备注', self.ed_note)

        self.lb_hint = QLabel('', self)
        self.lb_hint.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
        self.lb_hint.setWordWrap(True)
        v.addWidget(self.lb_hint)

        hb = QHBoxLayout()
        hb.addStretch(1)
        b_cancel = QPushButton('取消', self)
        b_cancel.clicked.connect(self.reject)
        b_ok = QPushButton('保存', self)
        b_ok.setObjectName('lgPrimary')
        b_ok.setDefault(True)
        b_ok.clicked.connect(self._on_ok)
        hb.addWidget(b_cancel)
        hb.addWidget(b_ok)
        v.addLayout(hb)

        self.cb_kind.currentIndexChanged.connect(self._fill_cats)
        self._entry = dict(entry or {})
        self._fill_cats()
        self._load()

    def _fill_cats(self):
        """分类下拉跟着收支切换（花出去和收进来本来就不是一套分类）"""
        kind = self.cb_kind.currentData()
        keep = self.cb_cat.currentText()
        self.cb_cat.blockSignals(True)
        self.cb_cat.clear()
        for c in self.book.cats_for(kind):
            self.cb_cat.addItem('%s %s' % (lg.cat_icon(c), c), c)
        self.cb_cat.blockSignals(False)
        idx = self.cb_cat.findData(keep)
        if idx >= 0:
            self.cb_cat.setCurrentIndex(idx)

    def _load(self):
        e = self._entry
        cur = lg.cfg_text(lg.CFG_ACCOUNT, '')
        kind = e.get('kind') or 'expense'
        self.cb_kind.setCurrentIndex(1 if kind == 'income' else 0)
        self._fill_cats()
        if e.get('amount'):
            self.sp_amount.setValue(float(e['amount']))
        d = str(e.get('date') or '') or (lg.today_str() if not e else '')
        if not d and self._entry.get('raw'):
            pass
        if d:
            y, m, dd = [int(x) for x in d.split('-')]
            self.de_date.setDate(QDate(y, m, dd))
        if e.get('cat'):
            idx = self.cb_cat.findData(e['cat'])
            if idx >= 0:
                self.cb_cat.setCurrentIndex(idx)
            else:
                self.cb_cat.setEditText(str(e['cat']))
        acct = e.get('account') or cur
        if acct:
            self.cb_account.setCurrentText(acct)
        self.ed_note.setText(str(e.get('note') or ''))
        if e.get('raw'):
            self.lb_hint.setText('原文：%s' % e['raw'])
        if not self.sp_amount.value():
            self.sp_amount.setFocus()
            self.sp_amount.selectAll()

    def _on_ok(self):
        if self.sp_amount.value() <= 0:
            self.lb_hint.setText('金额还没填（要大于 0）')
            self.sp_amount.setFocus()
            return
        self.accept()

    def value(self):
        """取出用户填的这一笔（分类允许临时手打新分类，会存进账本）"""
        cat = self.cb_cat.currentData() or self.cb_cat.currentText().strip()
        cat = str(cat or '').replace(lg.cat_icon(str(cat)) + ' ', '').strip() or '其他'
        d = self.de_date.date()
        return {
            'date': '%04d-%02d-%02d' % (d.year(), d.month(), d.day()),
            'amount': round(float(self.sp_amount.value()), 2),
            'kind': self.cb_kind.currentData() or 'expense',
            'cat': cat,
            'account': self.cb_account.currentText().strip(),
            'note': self.ed_note.text().strip(),
        }

    def apply_theme(self):
        self.setStyleSheet(ledger_qss())
        self.lb_hint.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))


# ---------------------------------------------------------------- 预算
class BudgetDialog(QDialog):
    """按分类设月度限额（拍板 4A：分类限额 + 超支在头顶轻提醒，不弹窗）"""

    def __init__(self, book, month=None, parent=None):
        super().__init__(parent)
        self.book = book
        self.month = lg.norm_month(month)
        self.setWindowTitle('分类预算 · %s' % lg.month_label(self.month))
        pwin.add_hide_button(self)      # v6.90：标题栏「—」隐藏键
        self.resize(400, 460)
        self.setStyleSheet(ledger_qss())
        self._spins = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 12)
        v.setSpacing(8)
        tip = QLabel('给常花钱的分类设个月度上限；超了桌宠会在头顶轻声说一句，不弹窗打断你。', self)
        tip.setWordWrap(True)
        tip.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
        v.addWidget(tip)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(2, 2, 6, 2)
        iv.setSpacing(4)
        spent = {r['cat']: r['amount'] for r in book.by_cat(self.month)}
        budgets = book.budgets()
        cats = list(book.cats('expense'))
        for c in sorted(spent):
            if c not in cats:
                cats.append(c)
        for cat in cats:
            h = QHBoxLayout()
            h.setSpacing(6)
            lb = QLabel('%s %s' % (lg.cat_icon(cat), cat), inner)
            lb.setFixedWidth(96)
            h.addWidget(lb)
            sp = QDoubleSpinBox(inner)
            sp.setRange(0, 9999999.0)
            sp.setDecimals(0)
            sp.setSingleStep(50)
            sp.setSuffix(' 元/月')
            sp.setValue(float(budgets.get(cat, 0) or 0))
            sp.valueChanged.connect(self._update_total)
            self._spins[cat] = sp
            h.addWidget(sp, 1)
            used = QLabel('已花 %s' % lg.fmt_amount(spent.get(cat, 0)), inner)
            used.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
            used.setFixedWidth(96)
            h.addWidget(used)
            iv.addLayout(h)
        iv.addStretch(1)
        scroll.setWidget(inner)
        _tint_viewport(scroll)
        v.addWidget(scroll, 1)

        self.lb_total = QLabel('', self)
        v.addWidget(self.lb_total)

        hb = QHBoxLayout()
        b_clear = QPushButton('全清', self)
        b_clear.clicked.connect(self._clear_all)
        hb.addWidget(b_clear)
        hb.addStretch(1)
        b_cancel = QPushButton('取消', self)
        b_cancel.clicked.connect(self.reject)
        hb.addWidget(b_cancel)
        b_ok = QPushButton('保存', self)
        b_ok.setObjectName('lgPrimary')
        b_ok.setDefault(True)
        b_ok.clicked.connect(self.accept)
        hb.addWidget(b_ok)
        v.addLayout(hb)
        self._update_total()

    def _update_total(self):
        total = sum(sp.value() for sp in self._spins.values())
        n = len([sp for sp in self._spins.values() if sp.value() > 0])
        self.lb_total.setText('合计 %s / 月（%d 个分类有限额）' % (lg.fmt_amount(total), n))

    def _clear_all(self):
        for sp in self._spins.values():
            sp.setValue(0)
        self._update_total()

    def result_budgets(self):
        return {c: round(float(sp.value()), 2) for c, sp in self._spins.items() if sp.value() > 0}

    def apply_theme(self):
        self.setStyleSheet(ledger_qss())


# ---------------------------------------------------------------- 主面板
class LedgerPanel(QDialog):
    """账本面板：月份切换 → 统计 → 图表 → 预算条 → 明细表"""

    def __init__(self, ctrl, parent=None):
        super().__init__(parent)
        self.ctrl = ctrl
        self.book = ctrl.book
        self.month = lg.norm_month(lg.cfg_text(lg.CFG_MONTH, lg.today_str()[:7]))
        self._rows = []
        self.setWindowTitle('记账 · %s' % lg.month_label(self.month))
        pwin.add_hide_button(self)      # v6.90：标题栏「—」隐藏键
        self.resize(920, 660)
        self.setStyleSheet(ledger_qss())
        _OPEN_DIALOGS.append(self)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)

        # ---- 顶部：月份切换 + 主按钮
        head = QHBoxLayout()
        head.setSpacing(6)
        self.btn_prev = QPushButton('‹', self)
        self.btn_prev.setFixedWidth(30)
        self.btn_prev.setToolTip('上个月')
        self.btn_prev.clicked.connect(lambda: self.shift(-1))
        head.addWidget(self.btn_prev)

        self.cb_month = QComboBox(self)
        self.cb_month.setMinimumWidth(118)
        self.cb_month.currentIndexChanged.connect(self._on_month_pick)
        head.addWidget(self.cb_month)

        self.btn_next = QPushButton('›', self)
        self.btn_next.setFixedWidth(30)
        self.btn_next.setToolTip('下个月')
        self.btn_next.clicked.connect(lambda: self.shift(1))
        head.addWidget(self.btn_next)

        self.btn_today = QPushButton('本月', self)
        self.btn_today.setFixedWidth(54)
        self.btn_today.clicked.connect(lambda: self.goto(lg.today_str()[:7]))
        head.addWidget(self.btn_today)
        head.addStretch(1)

        self.btn_budget = QPushButton('预算', self)
        self.btn_budget.clicked.connect(self.open_budget)
        head.addWidget(self.btn_budget)

        self.btn_export = QPushButton('导出', self)
        self.btn_export.clicked.connect(self._export_menu)
        head.addWidget(self.btn_export)

        self.btn_add = QPushButton('＋ 记一笔', self)
        self.btn_add.setObjectName('lgPrimary')
        self.btn_add.clicked.connect(self.add_entry)
        head.addWidget(self.btn_add)
        v.addLayout(head)

        # ---- 统计行
        self.lb_stats = QLabel('', self)
        f = self.lb_stats.font()
        f.setPointSizeF(10.5)
        self.lb_stats.setFont(f)
        v.addWidget(self.lb_stats)

        # ---- 图表：左环形，右柱状
        charts = QHBoxLayout()
        charts.setSpacing(10)
        self.donut = DonutChart(self)
        self.donut.setMinimumWidth(320)
        charts.addWidget(self.donut, 5)
        self.bars = BarChart(self)
        charts.addWidget(self.bars, 6)
        v.addLayout(charts, 3)

        # ---- 预算条
        self.lb_budget_h = QLabel('预算', self)
        self.lb_budget_h.setStyleSheet('color:%s; font-size:11px;'
                                       % _tk_of(None, 'ui_text_dim'))
        v.addWidget(self.lb_budget_h)
        self.budget_bars = BudgetBars(self)
        v.addWidget(self.budget_bars)

        # ---- 筛选行
        filt = QHBoxLayout()
        filt.setSpacing(6)
        self.ed_search = QLineEdit(self)
        self.ed_search.setPlaceholderText('搜索备注 / 分类 / 账户…')
        self.ed_search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.refresh_table)
        self.ed_search.textChanged.connect(lambda *_: self._search_timer.start())
        filt.addWidget(self.ed_search, 3)

        self.cb_kind = QComboBox(self)
        self.cb_kind.addItem('全部收支', '')
        self.cb_kind.addItem('只看支出', 'expense')
        self.cb_kind.addItem('只看收入', 'income')
        self.cb_kind.currentIndexChanged.connect(lambda *_: self.refresh_table())
        filt.addWidget(self.cb_kind, 1)

        self.cb_cat = QComboBox(self)
        self.cb_cat.currentIndexChanged.connect(lambda *_: self.refresh_table())
        filt.addWidget(self.cb_cat, 1)

        self.lb_count = QLabel('', self)
        self.lb_count.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
        filt.addWidget(self.lb_count)
        v.addLayout(filt)

        # ---- 明细表
        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(['日期', '收支', '分类', '金额', '账户', '备注'])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Fixed)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        hh.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 92)
        self.table.setColumnWidth(1, 46)
        self.table.setColumnWidth(2, 84)
        self.table.setColumnWidth(3, 96)
        self.table.setColumnWidth(4, 78)
        self.table.doubleClicked.connect(lambda *_: self.edit_selected())
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        v.addWidget(self.table, 5)

        # ---- 底部：一句人话汇总
        self.lb_summary = QLabel('', self)
        self.lb_summary.setWordWrap(True)
        self.lb_summary.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
        v.addWidget(self.lb_summary)

        self.refresh()

    # ---------- 月份
    def goto(self, month):
        self.month = lg.norm_month(month)
        lg.cfg_write(lg.CFG_MONTH, self.month)
        self.refresh()

    def shift(self, delta):
        self.goto(lg.shift_month(self.month, delta))

    def _on_month_pick(self, idx):
        m = self.cb_month.itemData(idx)
        if m and m != self.month:
            self.goto(m)

    def _fill_months(self):
        months = self.book.months()
        if self.month not in months:
            months.append(self.month)
        months = sorted(set(months), reverse=True)
        self.cb_month.blockSignals(True)
        self.cb_month.clear()
        for m in months:
            used = sum(1 for e in self.book.entries(m))
            self.cb_month.addItem('%s（%d 笔）' % (lg.month_label(m), used), m)
        idx = self.cb_month.findData(self.month)
        if idx >= 0:
            self.cb_month.setCurrentIndex(idx)
        self.cb_month.blockSignals(False)

    def _fill_cats(self):
        cur = self.cb_cat.currentData() or ''
        cats = sorted({e.get('cat') for e in self.book.entries(self.month) if e.get('cat')})
        self.cb_cat.blockSignals(True)
        self.cb_cat.clear()
        self.cb_cat.addItem('全部分类', '')
        for c in cats:
            self.cb_cat.addItem('%s %s' % (lg.cat_icon(c), c), c)
        idx = self.cb_cat.findData(cur)
        self.cb_cat.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_cat.blockSignals(False)

    # ---------- 刷新
    def refresh(self):
        self.book.reload()
        self.setWindowTitle('记账 · %s' % lg.month_label(self.month))
        self._fill_months()
        self._fill_cats()

        s = self.book.stats(self.month)
        self.lb_stats.setText(
            '支出 <b style="color:%s">%s</b>　收入 <b style="color:%s">%s</b>'
            '　结余 %s　日均 %s　%d 笔'
            % (_tk_of(None, 'ui_red'), lg.fmt_amount(s['expense']),
               _tk_of(None, 'ui_green'), lg.fmt_amount(s['income']),
               lg.fmt_amount(s['balance']), lg.fmt_amount(s['avg_per_day']), s['count']))

        self.donut.set_data(self.book.by_cat(self.month), s['expense'])
        self.bars.set_data(self.book.monthly(6, end_month=self.month))

        states = self.book.budget_state(self.month)
        self.budget_bars.set_data(states)
        self.lb_budget_h.setVisible(True)
        if states:
            over = [b for b in states if b['over']]
            near = [b for b in states if (not b['over']) and b['pct'] >= 80]
            left = sum(b['left'] for b in states if b['left'] > 0)
            tip = '预算：'
            if over:
                tip += '<span style="color:%s">超支 %d 项</span>　' % (
                    _tk_of(None, 'ui_red'), len(over))
            if near:
                tip += '<span style="color:%s">快到了 %d 项</span>　' % (
                    _tk_of(None, 'ui_gold'), len(near))
            tip += '合计还剩 %s' % lg.fmt_amount(left)
            self.lb_budget_h.setText(tip)
        else:
            self.lb_budget_h.setText('预算：还没设（点右上「预算」给分类设个月度上限）')

        self.refresh_table()
        self.lb_summary.setText(self.book.summary_line(self.month))

    def refresh_table(self):
        kind = self.cb_kind.currentData() or ''
        cat = self.cb_cat.currentData() or ''
        rows = self.book.entries(self.month, kind=kind, cat=cat, query=self.ed_search.text())
        self._rows = rows
        shown = rows[:MAX_TABLE_ROWS]
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(shown))
        for i, e in enumerate(shown):
            d = str(e.get('date', ''))
            vals = [d, lg.kind_label(e.get('kind')), '%s %s' % (lg.cat_icon(e.get('cat')),
                                                              e.get('cat')),
                    ('+' if e.get('kind') == 'income' else '-') + lg.fmt_amount(e.get('amount')),
                    str(e.get('account') or ''), str(e.get('note') or '')]
            for j, txt in enumerate(vals):
                it = QTableWidgetItem(txt)
                if j == 3:
                    it.setForeground(kind_color(e.get('kind')))
                    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                elif j in (0, 1):
                    it.setForeground(_qc('ui_text_dim'))
                if j == 0:
                    it.setData(Qt.UserRole, e.get('id'))
                self.table.setItem(i, j, it)
        self.table.setUpdatesEnabled(True)
        n = len(rows)
        extra = ('（只显示前 %d 笔，用搜索缩小范围）' % MAX_TABLE_ROWS) if n > MAX_TABLE_ROWS else ''
        self.lb_count.setText('%d 笔%s' % (n, extra))

    # ---------- 记一笔 / 改一笔
    def add_entry(self):
        dlg = EntryDialog(self.book, None, self)
        dlg.apply_theme()
        if dlg.exec() == QDialog.Accepted:
            self.ctrl.add_entry(dlg.value(), src='manual')
            self.refresh()

    def _selected_id(self):
        r = self.table.currentRow()
        if r < 0:
            return ''
        it = self.table.item(r, 0)
        return str(it.data(Qt.UserRole) or '') if it else ''

    def edit_selected(self):
        eid = self._selected_id()
        e = self.book.get(eid) if eid else None
        if not e:
            return
        dlg = EntryDialog(self.book, e, self)
        dlg.apply_theme()
        if dlg.exec() == QDialog.Accepted:
            self.ctrl.update_entry(eid, dlg.value())
            self.refresh()

    def delete_selected(self):
        eid = self._selected_id()
        if not eid:
            return
        if self.ctrl.delete_entry(eid):
            self.refresh()

    def _row_menu(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        self.table.setCurrentCell(idx.row(), 0)
        m = QMenu(self)
        m.addAction('改一笔…', self.edit_selected)
        m.addAction('删掉这一笔', self.delete_selected)
        m.addSeparator()
        m.addAction('复制成今天的一笔', self._dup_today)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _dup_today(self):
        eid = self._selected_id()
        e = self.book.get(eid) if eid else None
        if not e:
            return
        dup = dict(e)
        dup.pop('id', None)
        dup['date'] = lg.today_str()
        self.ctrl.add_entry(dup, src='manual')
        self.refresh()

    # ---------- 预算 / 导出
    def open_budget(self):
        dlg = BudgetDialog(self.book, self.month, self)
        dlg.apply_theme()
        if dlg.exec() == QDialog.Accepted:
            want = dlg.result_budgets()
            for cat in list(self.book.budgets()):
                if cat not in want:
                    self.book.set_budget(cat, 0)
            for cat, limit in want.items():
                self.book.set_budget(cat, limit)
            self.refresh()

    def _export_menu(self):
        m = QMenu(self)
        m.addAction('导出 Excel（xlsx）…', lambda: self._export('xlsx'))
        m.addAction('导出 CSV…', lambda: self._export('csv'))
        m.addSeparator()
        m.addAction('这个月全导出（不分月）…', lambda: self._export('xlsx', all_months=True))
        m.exec(self.btn_export.mapToGlobal(self.btn_export.rect().bottomLeft()))

    def _export(self, fmt, all_months=False):
        month = '' if all_months else self.month
        name = '记账_%s.%s' % (month or '全部', fmt)
        path, _ = QFileDialog.getSaveFileName(self, '导出账本',
                                             os.path.join(os.path.expanduser('~'), name),
                                             'Excel (*.xlsx);;CSV (*.csv)')
        if not path:
            return
        try:
            real, engine = self.book.export(path, month)
            note = '已导出 %s' % real
            if engine != fmt:
                note += '（没装 WPS/Office，先存成 CSV 了）'
            self.ctrl.notice.emit(note)
            self.lb_summary.setText(note)
        except Exception as e:
            self.lb_summary.setText('导出失败：%s' % e)

    def apply_theme(self):
        _QC_CACHE.clear()
        self.setStyleSheet(ledger_qss())
        self.lb_budget_h.setStyleSheet('color:%s; font-size:11px;' % _tk_of(None, 'ui_text_dim'))
        self.lb_count.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
        self.lb_summary.setStyleSheet('color:%s;' % _tk_of(None, 'ui_text_dim'))
        self.refresh()

    def closeEvent(self, ev):
        try:
            _OPEN_DIALOGS.remove(self)
        except ValueError:
            pass
        super().closeEvent(ev)


# ---------------------------------------------------------------- 控制器
class LedgerController(QObject):
    """把「记账」串起来：面板 / 本地解析 / 记一笔 / 超支提醒

    宿主只需要三件事：
        ctrl.panel()                → 打开面板（懒创建）
        ctrl.add_text('午饭 32')     → 本地解析记账，成功就回一句话
        ctrl.brief()                → 侧栏页签要的几个数字
    """

    notice = Signal(str)         # 要桌宠说一句话（头顶气泡）
    changed = Signal()           # 账本变了（面板/侧栏该刷新）

    def __init__(self, pet=None, book=None, parent=None):
        super().__init__(parent)
        self.pet = pet
        self.book = book or lg.default_book()
        self._panel = None

    # ---------- 面板
    def panel(self, parent=None):
        if self._panel is None:
            self._panel = LedgerPanel(self, parent)
        self._panel.refresh()
        return self._panel

    def show_panel(self, parent=None):
        return pwin.present(self.panel(parent))     # v6.90：隐藏着的面板要能一键叫回来

    # ---------- 记账
    def add_text(self, text, src='chat'):
        """本地优先解析一句话并记上（3A）

        返回 (ok, 回复)：ok=False 时回复里说明缺什么，宿主可以让 AI 再解析一次。
        """
        r = lg.parse_entry(text, cats=self.book.cats())
        if not r.get('ok'):
            return False, '这句话里没认出金额（%s）' % '、'.join(r.get('missing') or ['金额'])
        e = self.add_entry(r, src=src)
        if not e:
            return False, '这笔没记上（金额不合规）'
        return True, lg.line_for_user(e) + self.budget_tail(e)

    def add_entry(self, data, src='chat'):
        """记账（data 可以是本地解析结果，也可以是 AI 给的字段）"""
        acc = str((data or {}).get('account') or '').strip()
        if acc:
            lg.cfg_write(lg.CFG_ACCOUNT, acc)
        e = self.book.add(dict(data or {}, src=src))
        if e:
            self.changed.emit()
            if self._panel is not None:
                try:
                    if self._panel.isVisible():
                        self._panel.refresh()
                except RuntimeError:
                    self._panel = None
        return e

    def update_entry(self, eid, data):
        e = self.book.update(eid, **{k: data[k] for k in
                                     ('date', 'amount', 'kind', 'cat', 'account', 'note')
                                     if k in data})
        if e:
            self.changed.emit()
        return e

    def delete_entry(self, eid):
        ok = self.book.delete(eid)
        if ok:
            self.changed.emit()
        return ok

    # ---------- 预算提醒（拍板 4A：头顶轻提醒，不弹窗）
    def budget_tail(self, entry):
        """刚记的这一笔踩到预算线了吗？踩到就带一句话回去"""
        if not lg.cfg_bool(lg.CFG_ALERT, True):
            return ''
        if not entry or entry.get('kind') == 'income':
            return ''
        month = str(entry.get('date', ''))[:7]
        cat = entry.get('cat')
        for b in self.book.over_budget(month):
            if b['cat'] == cat:
                return '\n⚠ %s 这个月超预算了：花了 %s / 上限 %s（超 %s）' % (
                    cat, lg.fmt_amount(b['spent']), lg.fmt_amount(b['limit']),
                    lg.fmt_amount(-b['left']))
        for b in self.book.near_budget(month, 85):
            if b['cat'] == cat:
                return '\n%s 快到预算了：%s / %s' % (cat, lg.fmt_amount(b['spent']),
                                                lg.fmt_amount(b['limit']))
        return ''

    def check_budget(self, month=None):
        """主动体检（宿主定时/记完账后调用）：该提醒的一句话，没什么就返回 ''"""
        if not lg.cfg_bool(lg.CFG_ALERT, True):
            return ''
        month = lg.norm_month(month)
        over = self.book.over_budget(month)
        if over:
            b = over[0]
            return '%s 超预算了：花了 %s，上限 %s' % (
                b['cat'], lg.fmt_amount(b['spent']), lg.fmt_amount(b['limit']))
        near = self.book.near_budget(month, 90)
        if near:
            b = near[0]
            return '%s 快花完了：%s / %s' % (b['cat'], lg.fmt_amount(b['spent']),
                                          lg.fmt_amount(b['limit']))
        return ''

    # ---------- 给别人的数据
    def summary(self, month=None):
        return self.book.summary_line(month)

    def budget_summary(self, month=None):
        return self.book.budget_line(month)

    def brief(self):
        return self.book.brief(lg.cfg_text(lg.CFG_MONTH, lg.today_str()[:7]))


# ---------------------------------------------------------------- 单例
_CTRL = []


def controller(pet=None, book=None):
    """全局一个控制器（面板状态、上次月份都跟着走）"""
    if not _CTRL or (book is not None and _CTRL[0].book is not book):
        _CTRL[:] = [LedgerController(pet=pet, book=book)]
    elif pet is not None:
        _CTRL[0].pet = pet
    return _CTRL[0]


def open_panel(pet=None, parent=None):
    """宿主入口：打开账本面板"""
    return controller(pet).show_panel(parent or pet)
