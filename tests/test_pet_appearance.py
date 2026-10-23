# -*- coding: utf-8 -*-
"""人物大小 / 透明度可调（v6.81）

锁住四件事：
1. 大小有上下限（不会缩到看不见、也不会撑爆面板）
2. 改大小后窗口高度跟着变（面板高 + 立绘 + 60），立绘底边中点不动（不跳）
3. 透明度只作用在立绘上，100% 时彻底摘掉 effect（不做无谓离屏合成）
4. 调节面板能开出来，两个滑块范围正确

运行：python -m pytest tests/test_pet_appearance.py -q
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _pet(tmp_path):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import desktop_pet as dp
    p = dp.PetWidget()
    p._saved_cfg = {}
    p._save_cfg_value = lambda k, v=None, **kw: p._saved_cfg.__setitem__(k, v) or True
    p._chat_memory_path = lambda: str(tmp_path / 'chat_memory_test.json')
    p.show()
    QApplication.instance().processEvents()
    return p


def test_clamp_size_and_opacity(tmp_path):
    p = _pet(tmp_path)
    assert p._clamp_pet_size(10) == p.PET_SIZE_MIN, '太小要夹到下限'
    assert p._clamp_pet_size(9999) == p.PET_SIZE_MAX, '太大要夹到上限'
    assert p._clamp_pet_size('abc') == p.PET_SIZE_DEFAULT, '非数字回退出厂值'
    assert p._clamp_pet_size('300') == 300, '数字字符串也要认'
    assert p._clamp_pet_opacity(1) == 1.0
    assert p._clamp_pet_opacity(70) == 0.7, '0~100 的百分比要能认'
    assert p._clamp_pet_opacity(0) == p.PET_OPACITY_MIN, '不能调到完全看不见'
    assert p._clamp_pet_opacity(1.4) == 1.0


def test_apply_size_resizes_label_and_window(tmp_path):
    from PySide6.QtWidgets import QApplication
    p = _pet(tmp_path)
    p._apply_pet_size(320)
    QApplication.instance().processEvents()
    assert p.pet_size == 320
    assert p.pet_label.width() == 320 and p.pet_label.height() == 320
    assert p.height() == p.chat_panel.height() + 320 + 60, '窗口高 = 面板高 + 立绘 + 60'
    assert p._saved_cfg.get('pet_size') == 320, '要写进配置（重启后还在）'
    # 再调回 260
    p._apply_pet_size(260)
    QApplication.instance().processEvents()
    assert p.pet_label.width() == 260
    assert p._saved_cfg.get('pet_size') == 260


def test_apply_size_keeps_bottom_center(tmp_path):
    from PySide6.QtWidgets import QApplication
    p = _pet(tmp_path)
    p._apply_pet_size(240)
    QApplication.instance().processEvents()
    cx0 = p.x() + p.width() // 2
    bottom0 = p.y() + p.height()
    p._apply_pet_size(340)
    QApplication.instance().processEvents()
    assert abs((p.x() + p.width() // 2) - cx0) <= 2, '底边中点横向不该跑'
    assert abs((p.y() + p.height()) - bottom0) <= 2, '底边不该跳'


def test_opacity_only_pet_and_clears_at_full(tmp_path):
    p = _pet(tmp_path)
    p._apply_pet_opacity(0.6)
    eff = p.pet_stack.graphicsEffect()
    assert eff is not None, '非 100% 要挂透明度效果'
    assert abs(eff.opacity() - 0.6) < 1e-6
    assert p._saved_cfg.get('pet_opacity') == 0.6
    # 管的是立绘容器，不是整个窗口/面板
    assert p.graphicsEffect() is None
    assert p.chat_panel.graphicsEffect() is None
    p._apply_pet_opacity(1.0)
    assert p.pet_stack.graphicsEffect() is None, '100% 时要把 effect 摘掉'


def test_appearance_dialog_opens_with_sliders(tmp_path):
    from PySide6.QtWidgets import QSlider
    p = _pet(tmp_path)
    dlg = p._open_appearance_dialog()
    assert dlg is not None
    sliders = dlg.findChildren(QSlider)
    assert len(sliders) == 2, '应该有大小 + 透明度两个滑块'
    size_s, op_s = sliders[0], sliders[1]
    assert (size_s.minimum(), size_s.maximum()) == (p.PET_SIZE_MIN, p.PET_SIZE_MAX)
    assert op_s.minimum() == int(p.PET_OPACITY_MIN * 100) and op_s.maximum() == 100
    # 拖滑块 → 立绘立刻变（不需要点确定）
    size_s.setValue(300)
    assert p.pet_size == 300
    op_s.setValue(80)
    assert abs(p.pet_opacity - 0.8) < 1e-6
    dlg.close()


def test_header_has_style_button(tmp_path):
    p = _pet(tmp_path)
    assert hasattr(p, 'chat_style_btn'), '标题栏要有 🎨 入口按钮'
    assert p.chat_style_btn.parent() is p.chat_header
    assert p.chat_style_btn.toolTip()


def _ink_bbox(pm):
    """立绘彩色像素的包围盒（用来量“人到底多大”，而不是只看画布多大）"""
    from PySide6.QtGui import QImage
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    left, top, right, bottom = w, h, -1, -1
    for y in range(0, h):
        for x in range(0, w):
            if (img.pixel(x, y) >> 24) & 0xFF > 40:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right < 0:
        return None
    return (right - left + 1, bottom - top + 1)


def test_size_change_repaints_pet_proportionally(tmp_path):
    """改大小必须按新尺寸**重画那一帧**（旧 bug：QLabel 里还是旧 pixmap → 人没变/被裁）"""
    from PySide6.QtWidgets import QApplication
    p = _pet(tmp_path)
    app = QApplication.instance()
    big = 320
    small = 160
    p._apply_pet_size(big)
    app.processEvents()
    pm_big = p.pet_label.pixmap()
    assert pm_big is not None and (pm_big.width(), pm_big.height()) == (big, big), '画布要跟着新尺寸'
    p._apply_pet_size(small)
    app.processEvents()
    pm_small = p.pet_label.pixmap()
    assert pm_small is not None and (pm_small.width(), pm_small.height()) == (small, small)

    bb_big = _ink_bbox(pm_big)
    bb_small = _ink_bbox(pm_small)
    assert bb_big and bb_small, '立绘应该真的画出了东西'
    # 等比：宽高各自按尺寸比例缩（±8% 容差），长宽比保持
    ratio = small / big
    assert abs(bb_small[0] / bb_big[0] - ratio) < 0.08, '人物宽度要按比例缩'
    assert abs(bb_small[1] / bb_big[1] - ratio) < 0.08, '人物高度要按比例缩'
    ar_big = bb_big[0] / bb_big[1]
    ar_small = bb_small[0] / bb_small[1]
    assert abs(ar_big - ar_small) < 0.06, '长宽比不能变形（必须等比例）'
