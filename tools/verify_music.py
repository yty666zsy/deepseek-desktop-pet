# -*- coding: utf-8 -*-
"""音乐功能实机验证：开面板 → 搜歌 → 播放 → 头顶歌词（并截图）

用法：.venv\\Scripts\\python.exe tools\\verify_music.py [插件platform] [关键词]
默认插件取面板里记住的那个，取不到就用 qq（订阅装好后 QQ 音乐最稳）。
会真的播一小段（音量压到 0，不出声），验完自动收掉。
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication

import desktop_pet as dp

want = sys.argv[1] if len(sys.argv) > 1 else ''
keyword = sys.argv[2] if len(sys.argv) > 2 else '周杰伦 晴天'

app = QApplication(sys.argv[:1])
p = dp.PetWidget()
p.show()
app.processEvents()

panel = p.open_music_panel()
if panel is None:
    print('FAIL: 面板没打开')
    sys.exit(1)

names = [(panel.plugin_box.itemText(i), panel.plugin_box.itemData(i)) for i in range(panel.plugin_box.count())]
print('插件 %d 个，前 10：%s' % (len(names), [n for n, _ in names[:10]]))

target = want or p._music.last_plugin()
idx = panel.plugin_box.findData(target) if target else -1


def _find(want):
    """按平台名/文件名/显示名模糊找一项（下拉框的值是插件文件名）"""
    if not want:
        return -1
    want = want.lower()
    for i in range(panel.plugin_box.count()):
        if want in str(panel.plugin_box.itemData(i) or '').lower() or \
           want in str(panel.plugin_box.itemText(i) or '').lower():
            return i
    return -1


for fallback in (target, 'qq-source', 'netease-source', 'kugou-source', 'qq', 'netease', 'kugou', 'kuwo'):
    if idx >= 0:
        break
    idx = _find(fallback)
if idx < 0:
    idx = 0
if idx < 0:
    print('FAIL: 一个可用插件都没有，先点「➕ 订阅」')
    sys.exit(1)
panel.plugin_box.setCurrentIndex(idx)
plugin = panel.plugin_box.currentData()
print('使用插件:', panel.plugin_box.currentText(), '|', plugin)

c = p._music
c.player.set_volume(0)          # 验证不出声
if not c.lyric_on():            # 验证必须真的在测歌词，别被上次关掉的开关骗过去
    panel.lyric_btn.setChecked(True)
    print('歌词开关原本是关的，已打开')

panel.search_edit.setText(keyword)
panel.do_search()
t0 = time.time()
while time.time() - t0 < 35 and panel.table.rowCount() == 0:
    app.processEvents()
    time.sleep(0.1)
print('搜索结果:', panel.table.rowCount(), '条 |', panel.status.text())
if panel.table.rowCount() == 0:
    print('FAIL: 没搜到东西')
    c.shutdown()
    sys.exit(1)
print('第一条:', panel.table.item(0, 0).text(), '-', panel.table.item(0, 1).text())

panel.grab().save(os.path.join(SHOTS, 'v683_music_panel.png'))
print('已存 screenshots/v683_music_panel.png')

c.play_search_result(0)
t0 = time.time()
lyric_seen = ''
while time.time() - t0 < 50:
    app.processEvents()
    time.sleep(0.2)
    if c.lyric.isVisible() and len(c.lyric.text() or '') > 2:
        lyric_seen = c.lyric.text()
        break
pos = None
try:
    pos = c.player._ipc.get('time-pos') if c.player._ipc else None
except Exception:
    pass
print('播放:', c.player.playing, '| 进度:', pos, '| 歌词句数:', len(c._lines), '| 头顶:', lyric_seen)

p.grab().save(os.path.join(SHOTS, 'v683_lyric_bubble.png'))
print('已存 screenshots/v683_lyric_bubble.png')

c.player.set_volume(c.volume())
c.shutdown()
app.processEvents()
print('OK' if lyric_seen else 'WARN: 没看到头顶歌词')
