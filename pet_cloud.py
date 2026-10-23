# -*- coding: utf-8 -*-
"""pet_cloud.py — 云朵提示框（v6.77）

对话面板折叠/隐藏时，人物回复不落在可见界面上，用它在一个云朵形状里冒一下。
形状现画：三个圆团 + 底部圆角矩形 + 左下小尾巴（尾巴指向立绘头顶）。
颜色全部由宿主从主题字典传入，本模块不写任何颜色字面量
（tests/test_theme_tokens.py 的"产品代码零硬编码颜色"护栏）。
"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel

from pet_theme import DEFAULT_THEME as _THEME  # 主题唯一源（兜底值也从这里取）


class CloudBubble(QLabel):
    """云朵形状的提示气泡：文字由 QLabel 画，云朵轮廓在 paintEvent 里先铺底"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_theme(_THEME['say_bg'], _THEME['say_text'], _THEME['say_border'])

    def set_theme(self, bg, fg, bd):
        """宿主在创建时与切主题时调用（三个值都是主题 token 字符串）"""
        self._bg, self._fg, self._bd = bg, fg, bd
        self.setStyleSheet('QLabel{background:transparent;color:%s;font-size:12px;'
                           'padding:12px 14px 16px 14px;}' % fg)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(self._bg))
        p.setPen(QPen(QColor(self._bd), 2))
        p.drawPath(self._cloud_path())
        p.end()
        super().paintEvent(event)   # 文字照常由 QLabel 画（背景已在样式里设透明）

    def _cloud_path(self):
        """云朵轮廓：底部圆角矩形 ∪ 三个圆团 ∪ 左下尾巴，取并集得到一条外轮廓"""
        w, h = float(self.width()), float(self.height())
        body = h * 0.62                       # 云身高度（下方留出尾巴）
        path = QPainterPath()
        path.addRoundedRect(QRectF(w * 0.04, body * 0.52, w * 0.92, body * 0.48),
                            h * 0.17, h * 0.17)
        for cx, cy, r in ((0.26, 0.62, 0.20), (0.50, 0.38, 0.27), (0.74, 0.62, 0.20)):
            path.addEllipse(QPointF(w * cx, body * cy), h * r, h * r)
        tail = QPainterPath()
        tail.moveTo(w * 0.22, body * 0.82)
        tail.lineTo(w * 0.14, body * 1.34)
        tail.lineTo(w * 0.40, body * 0.96)
        tail.closeSubpath()
        return path.united(tail)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(210, 92)