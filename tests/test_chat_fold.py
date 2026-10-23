# -*- coding: utf-8 -*-
"""对话面板折叠 + 云朵提示（v6.77）

锁住三件事：
1. 收起时正文隐藏、标题栏留着（否则没地方展开）
2. 只有面板不可见（折叠/隐藏）时新消息才冒云朵
3. 点云朵能展开对话
另外顺带跑一次真绘制（grab），防"能构造但画不出来"。

运行：python -m pytest tests/test_chat_fold.py -q
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _pet():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import desktop_pet as dp
    p = dp.PetWidget()
    p._save_cfg_value = lambda *a, **k: True
    p.show()
    QApplication.instance().processEvents()
    return p


def test_fold_hides_body_keeps_header():
    p = _pet()
    p._apply_chat_fold(False)
    h0 = p.chat_panel.height()
    assert not p.chat_body.isHidden() and not p.chat_header.isHidden()

    p._toggle_chat_fold(True)
    assert p._chat_folded
    assert p.chat_body.isHidden(), '收起后正文必须隐藏'
    assert not p.chat_header.isHidden(), '收起后标题栏要留着（否则没地方展开）'
    assert p.chat_panel.height() == p.CHAT_FOLD_H

    p._toggle_chat_fold(False)
    assert not p._chat_folded
    assert not p.chat_body.isHidden()
    assert p.chat_panel.height() == h0, '展开后要恢复收起前的高度'


def test_cloud_hint_only_when_folded():
    p = _pet()
    p._apply_chat_fold(False)
    p._chat_type_start('我去买东西啦')
    assert p.cloud.isHidden(), '展开状态下不该冒云朵'

    p._toggle_chat_fold(True)
    p._chat_type_start('我去买东西啦')
    assert not p.cloud.isHidden(), '收起状态下新消息要冒云朵'
    assert '买东西' in p.cloud.text()
    assert p.cloud_hide_timer.isActive(), '云朵要能自动消失'


def test_cloud_sits_top_right_of_pet():
    p = _pet()
    p._toggle_chat_fold(True)
    p._show_cloud_hint('有话说')
    pet = p.pet_stack.geometry()
    assert p.cloud.x() + p.cloud.width() > pet.center().x(), '云朵应在立绘右侧'
    assert p.cloud.y() < pet.center().y(), '云朵应在立绘上方'


def test_cloud_click_expands_and_hides():
    p = _pet()
    p._toggle_chat_fold(True)
    p._show_cloud_hint('有话说')
    assert not p.cloud.isHidden()
    p._cloud_clicked()
    assert not p._chat_folded, '点云朵要展开对话'
    assert p.cloud.isHidden(), '点完云朵要收起'


def test_window_height_follows_fold_state():
    """窗口高度要跟着折叠走：折叠 350、面板隐藏 340、否则 面板高+320

    贴边扒边那套逻辑会自己设窗口高度，之前硬编码 560 会把折叠态撑出一大片空白。
    """
    from PySide6.QtWidgets import QApplication
    p = _pet()
    p._apply_chat_fold(False)
    QApplication.instance().processEvents()
    assert p._win_h() == p.chat_panel.height() + p._pet_band_h()
    assert p.height() == p.chat_panel.height() + p._pet_band_h(), '窗口高 = 面板高 + 立绘区（人物大小可调，别写死）'

    p._toggle_chat_fold(True)
    QApplication.instance().processEvents()
    assert p._win_h() == p.CHAT_FOLD_H + p._pet_band_h()
    assert p.height() == p.CHAT_FOLD_H + p._pet_band_h(), '折叠后窗口要变矮'

    p.chat_panel.hide()
    assert p._win_h() == p.pet_size + 80, '只留立绘时也要跟人物大小走'


def test_cloud_paints():
    """真绘制一次：paintEvent 里画云朵的 QPainter 路径不能抛异常"""
    p = _pet()
    p._toggle_chat_fold(True)
    p._show_cloud_hint('绘制检查')
    pm = p.cloud.grab()
    assert not pm.isNull(), '云朵 grab 返回空图（没画出来）'
    assert pm.width() > 100 and pm.height() > 40