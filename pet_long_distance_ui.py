# -*- coding: utf-8 -*-
"""pet_long_distance_ui.py — 异地面板：我的城市 / TA 的城市、天气、直线距离（v6.95）

界面只做三件事：
1. 填两座城市（存 config.json 的 ld_my_city / ld_city）
2. 看两边天气 + 两城直线距离（明确写「非路程」）
3. 开关「天气突变提醒」（ld_alert）

取数在后台线程，界面只收结果。颜色一律走主题 token。
"""
import datetime
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

import pet_window as pwin

_TOKEN_KEYS = ('dialog_bg', 'text', 'input_bg', 'input_text', 'list_border',
               'ui_btn_bg', 'ui_btn_hover', 'ui_text', 'ui_text_dim', 'ui_accent')


def _tokens(host):
    import pet_theme
    out = {}
    for key in _TOKEN_KEYS:
        try:
            out[key] = host._tk(key)
        except Exception:
            try:
                out[key] = pet_theme.color(key)
            except Exception:
                out[key] = ''
    return out


class LongDistanceDialog(QDialog):
    loaded = Signal(object)

    def __init__(self, host, parent=None):
        super(LongDistanceDialog, self).__init__(parent)
        self.host = host
        self.svc = host._long_distance()
        cfg = getattr(host, '_cfg', {}) or {}
        self.setWindowTitle('🌤 异地：天气与距离')
        pwin.add_hide_button(self)
        self.resize(480, 340)
        self.setStyleSheet(self._qss())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        head = QLabel('看看 TA 那边什么天气，顺便算算你们隔多远。城市或具体地点都行（校区/小区/街道），越具体越准。')
        head.setWordWrap(True)
        lay.addWidget(head)

        grid = QGridLayout()
        grid.setSpacing(8)
        grid.addWidget(QLabel('我的地点'), 0, 0)
        self.ed_my = QLineEdit(str(cfg.get('ld_my_city') or getattr(host, 'pet_city', '') or ''))
        self.ed_my.setPlaceholderText('留空则用设置里的城市')
        self.ed_my.setClearButtonEnabled(True)
        grid.addWidget(self.ed_my, 0, 1)
        grid.addWidget(QLabel('TA 的地点'), 1, 0)
        self.ed_ta = QLineEdit(str(cfg.get('ld_city') or ''))
        self.ed_ta.setPlaceholderText('例如：青岛市崂山区青岛科技大学')
        self.ed_ta.setClearButtonEnabled(True)
        grid.addWidget(self.ed_ta, 1, 1)
        lay.addLayout(grid)

        row = QHBoxLayout()
        self.btn_save = QPushButton('保存并刷新')
        self.btn_save.clicked.connect(lambda: self._reload(force=True, save=True))
        self.btn_refresh = QPushButton('刷新')
        self.btn_refresh.clicked.connect(lambda: self._reload(force=True))
        self.chk_alert = QCheckBox('天气突变时主动提醒我')
        self.chk_alert.setChecked(bool(cfg.get('ld_alert', True)))
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        row.addWidget(self.chk_alert)
        lay.addLayout(row)

        line = QLabel('')
        line.setFrameShape(QLabel.HLine)
        lay.addWidget(line)

        self.lbl_my = QLabel('我的地点：—')
        self.lbl_ta = QLabel('TA 的地点：—')
        self.lbl_dist = QLabel('直线距离：—')
        self.lbl_status = QLabel('')
        self.lbl_status.setWordWrap(True)
        for w in (self.lbl_my, self.lbl_ta):
            w.setWordWrap(True)
        lay.addWidget(self.lbl_my)
        lay.addWidget(self.lbl_ta)
        lay.addWidget(self.lbl_dist)
        lay.addStretch(1)
        lay.addWidget(self.lbl_status)

        hint = QLabel('提示：填到校区、小区、街道会自动定位到具体地点。提醒只在 8:00–23:00 生效；下雨/下雪、明天明显降温、大风，每天最多提醒 2 次。')
        hint.setWordWrap(True)
        hint.setObjectName('hint')
        lay.addWidget(hint)

        self.loaded.connect(self._apply)
        self._reload()

    def _qss(self):
        t = _tokens(self.host)
        return (
            'QDialog{background:%(dialog_bg)s;}'
            'QLabel{color:%(text)s;font-size:12px;}'
            'QLabel#hint{color:%(ui_text_dim)s;}'
            'QLineEdit{background:%(input_bg)s;color:%(input_text)s;'
            'border:1px solid %(list_border)s;border-radius:6px;padding:5px 8px;font-size:12px;}'
            'QPushButton{background:%(ui_btn_bg)s;color:%(ui_text)s;border:none;border-radius:6px;'
            'padding:6px 12px;font-size:12px;}'
            'QPushButton:hover{background:%(ui_btn_hover)s;}'
            'QPushButton:disabled{color:%(ui_text_dim)s;}'
            'QCheckBox{color:%(text)s;font-size:12px;}'
            'QLabel[frameShape="4"]{color:%(list_border)s;}'
            % t
        )

    def _city_names(self):
        my = self.ed_my.text().strip() or str(getattr(self.host, 'pet_city', '') or '').strip()
        return my, self.ed_ta.text().strip()

    def _save(self):
        my = self.ed_my.text().strip()
        ta = self.ed_ta.text().strip()
        self.host._save_cfg_value('ld_my_city', my)
        self.host._save_cfg_value('ld_city', ta)
        self.host._save_cfg_value('ld_alert', self.chk_alert.isChecked())

    def _reload(self, force=False, save=False):
        my, ta = self._city_names()
        if save:
            self._save()
        if not ta:
            self.lbl_ta.setText('TA 的地点：还没填')
            self.lbl_dist.setText('直线距离：—')
            self.lbl_status.setText('先填 TA 的地点，再点「保存并刷新」。')
            return
        self.btn_save.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.lbl_status.setText('正在查天气…')

        def work():
            try:
                self.loaded.emit(self.svc.snapshot(my, ta, force=force))
            except Exception as e:
                self.loaded.emit({'error': str(e)})

        threading.Thread(target=work, daemon=True).start()

    def _apply(self, snap):
        self.btn_save.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        if not isinstance(snap, dict) or snap.get('error'):
            msg = (snap or {}).get('error') if isinstance(snap, dict) else '取数失败'
            self.lbl_status.setText('没查到：%s' % msg)
            return
        import pet_long_distance as ld
        my, ta = snap['my'], snap['ta']
        self.lbl_my.setText('我的地点 · %s\n%s' % (my['geo']['label'], ld.weather_line(my['weather'])))
        self.lbl_ta.setText('TA 的地点 · %s\n%s' % (ta['geo']['label'], ld.weather_line(ta['weather'])))
        self.lbl_dist.setText('直线距离：约 %s 公里（两点直线，非路程）'
                              % format(int(round(snap['distance_km'])), ','))
        ts = max((my['weather'] or {}).get('updated_at') or 0, (ta['weather'] or {}).get('updated_at') or 0)
        when = datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '—'
        self.lbl_status.setText('数据时间 %s ｜ 天气每 30 分钟自动刷新' % when)
