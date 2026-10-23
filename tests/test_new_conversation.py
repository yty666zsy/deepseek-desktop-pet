# -*- coding: utf-8 -*-
"""新开对话（v6.80）

锁住四件事：
1. 标题栏有「新对话」按钮（折叠时也能点）
2. 点它会清空上下文 + 面板回显 + 待发附件，并把旧对话存档成 txt
3. 存档失败时**不清空**（宁可不重开，也不能丢数据）
4. /new 指令走同一条路径，且这条指令本身不写进历史

运行：python -m pytest tests/test_new_conversation.py -q
"""
import json
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def _pet(tmp_path):
    """离屏桌宠 + 把记忆/存档路径都引到 tmp（绝不碰用户真实聊天记录）"""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import desktop_pet as dp
    p = dp.PetWidget()
    p._save_cfg_value = lambda *a, **k: True
    p._chat_memory_path = lambda: str(tmp_path / 'chat_memory_test.json')
    arch = tmp_path / 'chat_archive'
    arch.mkdir(exist_ok=True)
    p._archive_dir = lambda: str(arch)
    p._notices = []
    p._notify = lambda text, ms=None: p._notices.append(str(text))
    p.show()
    QApplication.instance().processEvents()
    return p


def _seed(p):
    p.chat_history_msgs = [
        {'role': 'user', 'content': '你好'},
        {'role': 'assistant', 'content': '你好呀～'},
    ]
    p.display_msgs = [
        {'who': '我', 'text': '你好', 'ts': '10-21 20:00'},
        {'who': '桌宠', 'text': '你好呀～', 'ts': '10-21 20:00'},
    ]
    p._display_offset = 0


def test_header_has_new_chat_button(tmp_path):
    p = _pet(tmp_path)
    assert hasattr(p, 'chat_new_btn'), '标题栏缺少「新对话」按钮'
    assert p.chat_new_btn.parent() is p.chat_header, '按钮要挂在标题栏上（折叠时也在）'
    assert '新对话' in p.chat_new_btn.text() or 'New chat' in p.chat_new_btn.text()
    assert p.chat_new_btn.toolTip(), '要有 tooltip 说明会自动存档'
    # 折叠后标题栏仍在 → 按钮仍可用
    p._toggle_chat_fold(True)
    assert not p.chat_new_btn.isHidden()


def test_new_conversation_clears_and_archives(tmp_path):
    p = _pet(tmp_path)
    _seed(p)
    p._pending_attachments = [{'icon': '📄', 'name': 'a.txt', 'path': 'a.txt', 'size': '1 KB'}]

    assert p._new_conversation() is True
    assert p.chat_history_msgs == [], '上下文必须清空'
    assert p.display_msgs == [], '面板回显历史必须清空'
    assert p._pending_attachments == [], '待发附件要一起清掉'
    assert p.chat_input.toPlainText() == ''
    assert p._display_offset == 0

    # 记忆文件已落盘为空（不会重启后又冒出来）
    with open(p._chat_memory_path(), encoding='utf-8') as f:
        saved = json.load(f)
    assert saved.get('messages') == [] and saved.get('display') == []

    # 旧对话进存档
    files = os.listdir(p._archive_dir())
    assert len(files) == 1, '旧对话应该存档成一个 txt'
    with open(os.path.join(p._archive_dir(), files[0]), encoding='utf-8') as f:
        body = f.read()
    assert '你好呀' in body and files[0].endswith('.txt')
    assert any('存档' in n or 'archived' in n for n in p._notices)


def test_new_conversation_archive_failure_keeps_history(tmp_path):
    p = _pet(tmp_path)
    _seed(p)

    def boom():
        raise RuntimeError('磁盘写不进去')

    p._archive_chat_file = boom
    assert p._new_conversation() is False, '存档失败要返回 False'
    assert len(p.chat_history_msgs) == 2, '存档失败绝不能清空历史'
    assert len(p.display_msgs) == 2
    assert any('取消' in n or 'cancelled' in n for n in p._notices)


def test_new_conversation_without_archive(tmp_path):
    """archive=False（等价 /clear 的语义）：清空但不写存档"""
    p = _pet(tmp_path)
    _seed(p)
    assert p._new_conversation(archive=False) is True
    assert p.chat_history_msgs == []
    assert os.listdir(p._archive_dir()) == []


def test_slash_new_command(tmp_path):
    p = _pet(tmp_path)
    _seed(p)
    p._pending_attachments = []
    p.chat_input.setPlainText('/new')
    p._on_chat_input()
    assert p.chat_history_msgs == [], '/new 要开新对话'
    assert p.display_msgs == [], '/new 指令本身不该留在历史里'
    assert p.chat_input.toPlainText() == ''
    assert len(os.listdir(p._archive_dir())) == 1


def test_archive_and_clear_delegates(tmp_path):
    """旧入口 _archive_and_clear 仍可用（走同一条路）"""
    p = _pet(tmp_path)
    _seed(p)
    assert p._archive_and_clear() is True
    assert p.chat_history_msgs == []
    assert len(os.listdir(p._archive_dir())) == 1


def test_empty_panel_shows_hint_not_blank(tmp_path):
    """清空后不能留一片空白（原来露出的是滚动区的白色调色板底色）"""
    p = _pet(tmp_path)
    _seed(p)
    p._new_conversation()
    hint = getattr(p, 'chat_empty_hint_lbl', None)
    assert hint is not None, '空对话要有占位提示'
    assert not hint.isHidden()
    assert hint.text().strip()
    # 来了新消息 → 提示自动让位
    p._append_chat('我', '在吗')
    assert hint.isHidden(), '有消息后占位提示要收起'


def test_scroll_area_is_transparent(tmp_path):
    """容器与 viewport 都要透明，否则空白处是白底"""
    p = _pet(tmp_path)
    assert p.chat_history_container.objectName() == 'chatHistoryBox'
    qss = p._panel_qss()
    assert 'chatHistoryBox' in qss and 'chatHistoryViewport' in qss
    assert not p.chat_history_container.autoFillBackground()

