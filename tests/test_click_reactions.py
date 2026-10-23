# -*- coding: utf-8 -*-
"""单击立绘的反应（v6.78）

锁住：
1. 反应表不少于 32 种，且至少 32 张不同立绘，立绘文件真实存在
2. pick 不会连着给同一条，也能覆盖整张表
3. 真的点一下会说话 + 换立绘（离屏构造，不依赖桌面）
4. 拖动（位移 > 6px）不算点击，不触发反应

运行：python -m pytest tests/test_click_reactions.py -q
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pet_reactions as reactions  # noqa: E402


def _pet():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import desktop_pet as dp
    p = dp.PetWidget()
    p._save_cfg_value = lambda *a, **k: True
    p._save_position = lambda *a, **k: True
    p.show()
    QApplication.instance().processEvents()
    return p


def test_table_has_32_plus_and_real_images():
    assert len(reactions.REACTIONS) >= 32, '反应少于 32 种'
    keys = [r[0] for r in reactions.REACTIONS if r[0]]
    assert len(set(keys)) >= 32, '不同立绘少于 32 张'

    import desktop_pet as dp
    for key, zh, en in reactions.REACTIONS:
        assert zh and en, '中英文台词都不能空：%r' % (key,)
        if key:
            path = os.path.join(dp.ASSETS, 'flash', 'flash_%s.png' % key)
            assert os.path.exists(path), '立绘文件不存在：%s' % path


def test_pick_never_repeats_last_and_covers_table():
    n = len(reactions.REACTIONS)
    for last in range(n):
        for _ in range(50):
            assert reactions.pick(last) != last, '不该连续两次给同一条'
    seen = {reactions.pick(-1) for _ in range(n * 200)}
    assert seen == set(range(n)), '抽不到全部条目：缺 %s' % (set(range(n)) - seen)


def test_click_says_something_and_swaps_image():
    p = _pet()
    p.sleeping = False
    said = []
    p.say_plain = lambda text, immediate=False: said.append(text)
    swapped = False
    for _ in range(20):        # 有 4 条是"只说话不换图"，多点几次必然换到换图那条
        p.state = 'idle'
        p._on_click()
        if p.state == 'reaction':
            swapped = True
            break
    assert said, '点一下要说点什么'
    assert swapped, '点几下总该换一张反应立绘'
    assert p._last_reaction >= 0


def test_click_36_times_gives_many_different_lines():
    p = _pet()
    p.sleeping = False
    said = []
    p.say_plain = lambda text, immediate=False: said.append(text)
    for _ in range(400):        # 随机抽样要够多才覆盖全表（36 条，抽 400 次基本全覆盖）
        p._click_reaction()
    assert len(set(said)) >= 34, '400 次点击只出了 %d 种台词' % len(set(said))


def test_every_reaction_has_an_image():
    """点了必须有可见反应：每条都得有立绘（不能有"只说话不换图"的）"""
    for key, zh, en in reactions.REACTIONS:
        assert key, '这条反应没有立绘：%s' % zh


def test_click_while_docked_still_reacts():
    """贴在屏幕边缘（竖条形态）时点一下也要有反应（v6.78 修）"""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    p = _pet()
    p.sleeping = False
    p._edge_side, p._edge_mode, p._edge_popped = 'right', 'peek', False
    said = []
    p.say_plain = lambda text, immediate=False: said.append(text)

    def ev(kind, x):
        return QMouseEvent(kind, QPointF(10, 10), QPointF(x, 10),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    p.mousePressEvent(ev(QEvent.MouseButtonPress, 100))
    p.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 101))
    assert said, '扒边时点一下也要有反应'


def test_click_wakes_sleeping_pet():
    p = _pet()
    p.sleeping = True
    said = []
    p.say_plain = lambda text, immediate=False: said.append(text)
    p._click_reaction()
    assert not p.sleeping, '睡着的桌宠被点应该醒'
    assert said, '醒了也要说点什么'


def test_all_scene_actions_are_click_reachable():
    """12 个场景动作（吃饭/看书/听歌…）都能靠点击触发到"""
    from pet_anim import SCENE_ACTIONS
    keys = {r[0] for r in reactions.REACTIONS}
    missing = set(SCENE_ACTIONS) - keys
    assert not missing, '这些场景动作点不到：%s' % missing


def test_drag_does_not_trigger_reaction():
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    p = _pet()
    said = []
    p.say_plain = lambda text, immediate=False: said.append(text)

    def ev(kind, gx):
        return QMouseEvent(kind, QPointF(10, 10), QPointF(gx, 10),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    p.mousePressEvent(ev(QEvent.MouseButtonPress, 100))
    p.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 300))
    assert not said, '拖动不该触发反应'

    p.mousePressEvent(ev(QEvent.MouseButtonPress, 100))
    p.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, 102))
    assert said, '原地点击应该触发反应'
