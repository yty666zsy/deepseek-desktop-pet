# -*- coding: utf-8 -*-
"""音乐功能（v6.82）：MusicFree 插件音源 + mpv 播放 + 头顶歌词

这里锁的是“纯逻辑 + 界面行为”，**不打网络**（搜歌/取音源要靠外网，不适合进单测）：
1. LRC 解析要认得两种真实格式（同一句多时间戳 / 一整行塞整首歌）
2. 插件标题清洗能抠出歌名歌手（`【4K修复】周杰伦 - 晴天MV 2160P修复版` → 晴天/周杰伦）
3. Track 的请求头会变成 mpv 的 http-header-fields，且下一首会清空（不然 Referer 串台 → 403）
4. 头顶歌词跟说话气泡抢地方时，歌词要让位；说完话自己回来
5. 右键菜单和标题栏都有音乐入口；面板能开、插件下拉有货

运行：python -m pytest tests/test_music.py -q
"""
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest


# ---------------------------------------------------------------- 纯函数 / 解析
def test_parse_lrc_standard_multi_stamp():
    from pet_music import parse_lrc
    # 同一句词挂在两个时间戳上：两个时间点各出现一次
    got = parse_lrc('[00:01.00][00:05.00]同一句\n[00:09.00]下一句')
    assert got == [(1.0, '同一句'), (5.0, '同一句'), (9.0, '下一句')]


def test_parse_lrc_interleaved_single_line():
    """歌词站常见坑：一整行塞了整首歌，还夹着大量空格填充

    旧实现把「去掉时间戳后剩下的全部文本」安到每个时间点上，
    结果每句歌词都变成整首歌词；这里必须按段切开。
    """
    from pet_music import parse_lrc
    raw = '[00:01.00]第一句              [00:05.50]第二句              [00:09.00]第三句'
    got = parse_lrc(raw)
    assert [t for t, _ in got] == [1.0, 5.5, 9.0]
    assert [w for _, w in got] == ['第一句', '第二句', '第三句'], '每句只能有自己那一截词'


def test_parse_lrc_ignores_meta_and_garbage():
    from pet_music import parse_lrc
    raw = '[ti:标题]\n[ar:歌手]\n没时间戳的行\n[00:02.30]有词\n[bad]乱码'
    assert parse_lrc(raw) == [(2.3, '有词')]


def test_clean_title_pulls_out_song_and_artist():
    import pet_music_lyrics as L
    assert L.clean_title('《晴天》- 周杰伦') == ('晴天', '周杰伦')
    # 中文旁边的英文字母没有 \b 边界，早先版本这里去不掉 MV/2160P
    assert L.clean_title('【4K修复】周杰伦 - 晴天MV 2160P修复版') == ('周杰伦', '晴天')
    assert L.strip_noise('晴天MV 2160P修复版') == '晴天'


def test_name_candidates_try_both_orders():
    """插件标题可能是「歌名 - 歌手」也可能是「歌手 - 歌名」，两种都得试"""
    import pet_music_lyrics as L
    cands = L._name_candidates('【4K修复】周杰伦 - 晴天MV 2160P修复版', '')
    assert ('晴天', '周杰伦') in cands, '正序要试'
    assert ('周杰伦', '晴天') in cands, '反序也要试'


# ---------------------------------------------------------------- 播放内核
def test_track_headers_become_mpv_fields_and_get_cleared():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    from pet_music import MusicPlayer, Track

    class FakeIpc(object):
        def __init__(self):
            self.calls = []

        def set(self, key, value):
            self.calls.append((key, value))

    player = MusicPlayer()
    ipc = FakeIpc()
    tr = Track('https://x/a.m4s', headers={'referer': 'https://www.bilibili.com/video/BV1',
                                           'user-agent': 'UA/1.0'})
    player._apply_headers(ipc, tr)
    assert ipc.calls[-1][0] == 'http-header-fields'
    assert ipc.calls[-1][1] == ['referer: https://www.bilibili.com/video/BV1', 'user-agent: UA/1.0']

    # 换到本地文件这一首时，头必须清空，否则 Referer 会串过去
    player._apply_headers(ipc, Track('/tmp/b.mp3'))
    assert ipc.calls[-1] == ('http-header-fields', [])


def test_auto_next_callback_takes_over():
    """队列由控制器自己排（MusicFree 直链要按需去取），内核得把「放完了」交出去"""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    from pet_music import MusicPlayer, Track

    player = MusicPlayer()
    hit = []
    player.auto_next_callback = lambda: hit.append(1)
    player.tracks = [Track('/tmp/a.mp3'), Track('/tmp/b.mp3')]
    player.index = 0
    player._auto_next()
    assert hit == [1], '有回调就走回调，不要自己往下播'


# ---------------------------------------------------------------- 插件桥
def test_plugin_host_available_is_boolean():
    import pet_music_free as mf
    assert isinstance(mf.PluginHost.available(), bool)


def test_plugin_host_lists_plugins_offline():
    import pet_music_free as mf
    if not mf.PluginHost.available():
        pytest.skip('这台机器没有 Node.js 或没放插件')
    host = mf.PluginHost(timeout=30)
    try:
        plugins = host.list_plugins()
    finally:
        host.close()
    assert plugins, '至少能列出插件'
    assert any(not p.get('error') for p in plugins), '至少有一个插件加载成功'
    for p in plugins:
        assert 'platform' in p and 'methods' in p


# ---------------------------------------------------------------- 插件订阅（v6.83）
def test_parse_subscription_accepts_common_shapes():
    import pet_music_free as mf
    a = mf.parse_subscription('{"desc":"0.1.0","plugins":[{"name":"QQ音乐","url":"http://x/qq.js","version":"1.0"}]}')
    assert a == [{'name': 'QQ音乐', 'url': 'http://x/qq.js', 'version': '1.0'}]
    b = mf.parse_subscription('[{"name":"A","srcUrl":"http://x/a.js"}]')
    assert b and b[0]['url'] == 'http://x/a.js'
    c = mf.parse_subscription('{"pluginList":[{"name":"B","downloadUrl":"http://x/b.js"}]}')
    assert c and c[0]['url'] == 'http://x/b.js'
    # 没有地址的条目要跳过；重复地址去重
    d = mf.parse_subscription('{"plugins":[{"name":"无地址"},{"name":"C","url":"http://x/c.js"},{"name":"C2","url":"http://x/c.js"}]}')
    assert [x['name'] for x in d] == ['C']
    with pytest.raises(mf.PluginHostError):
        mf.parse_subscription('not json at all')
    with pytest.raises(mf.PluginHostError):
        mf.parse_subscription('{"desc":"空"}')


def test_plugin_slug_strips_version_to_stable_name():
    """文件名必须稳定：不然每次更新都留一份旧文件，插件列表里一堆重名"""
    import pet_music_free as mf
    assert mf.plugin_slug('https://a/b/qq-source.plugin.v1.9.23.js') == 'qq-source.js'
    assert mf.plugin_slug('https://a/b/apple-music-v1.0.2.js') == 'apple-music.js'
    assert mf.plugin_slug('https://a/b/x.js?v=3') == 'x.js'
    assert mf.plugin_slug('https://a/b/netease-source.plugin.v1.9.22.js') == 'netease-source.js'
    # 抠不出名字时用哈希兜底，不能出现空名
    slug = mf.plugin_slug('https://a/b/.js', '中文插件名')
    assert slug.endswith('.js') and len(slug) > 4


def test_install_subscription_writes_files_and_skips_uptodate(tmp_path, monkeypatch):
    import json
    import pet_music_free as mf

    plug = tmp_path / 'plugins'
    monkeypatch.setattr(mf, 'PLUGIN_DIR', str(plug))
    monkeypatch.setattr(mf, 'META_FILE', str(plug / '_plugins.json'))

    sub = json.dumps({'plugins': [
        {'name': 'QQ音乐', 'url': 'http://x/qq-source.plugin.v1.0.1.js', 'version': '1.0.1'},
        {'name': '坏插件', 'url': 'http://x/broken.js', 'version': '1.0'},
    ]})
    js_blob = b'/* ' + b'x' * 400 + b' */ module.exports = { search: () => ({}) }'

    def fake_get(url, timeout=30):
        if url.endswith('plugins.json'):
            return sub.encode('utf-8')
        if url.endswith('broken.js'):
            raise RuntimeError('网络断了')
        return js_blob

    monkeypatch.setattr(mf, '_http_get', fake_get)

    seen = []
    rep = mf.install_subscription('http://x/plugins.json', progress=lambda d, t, s: seen.append((d, t)))
    assert rep['added'] == 1 and rep['total'] == 2
    assert rep['failed'] and rep['failed'][0][0] == '坏插件', '单个插件失败不能拖垮整次导入'
    assert (plug / 'qq-source.js').read_bytes() == js_blob
    assert seen and seen[-1][1] == 2, '进度要报 total'
    meta = json.loads((plug / '_plugins.json').read_text(encoding='utf-8'))
    assert meta['qq-source.js']['name'] == 'QQ音乐'

    # 版本没变 → 跳过（不重复下载）
    rep2 = mf.install_subscription('http://x/plugins.json')
    assert rep2['skipped'] == 1 and rep2['added'] == 0
    # force → 覆盖
    rep3 = mf.install_subscription('http://x/plugins.json', force=True)
    assert rep3['updated'] == 1


def test_plugin_vars_roundtrip(tmp_path, monkeypatch):
    """Jamendo 这类插件靠用户变量（Client ID）才能搜，得能存能读"""
    import json
    import pet_music_free as mf

    monkeypatch.setattr(mf, 'VARS_FILE', str(tmp_path / 'vars.json'))
    assert mf.read_vars() == {}
    assert mf.set_plugin_vars('jamendo', {'clientId': 'abc'}) is True
    assert mf.read_vars()['jamendo']['clientId'] == 'abc'
    mf.set_plugin_vars('jamendo', {'secret': 'x'})
    data = json.loads((tmp_path / 'vars.json').read_text(encoding='utf-8'))
    assert data['jamendo'] == {'clientId': 'abc', 'secret': 'x'}, '同一个插件的变量要合并，不能覆盖掉旧的'


# ---------------------------------------------------------------- 界面
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


def test_context_menu_and_header_have_music_entry(tmp_path):
    from PySide6.QtWidgets import QApplication
    p = _pet(tmp_path)
    menu, acts = p._build_context_menu()
    assert 'music' in acts, '右键菜单要有音乐入口'
    assert '音乐' in acts['music'].text()
    assert hasattr(p, 'chat_music_btn'), '标题栏要有 🎵 按钮'
    assert p.chat_music_btn.text() == '🎵'


def test_music_panel_opens_with_plugins(tmp_path, monkeypatch):
    import pet_music_free as mf
    if not mf.PluginHost.available():
        pytest.skip('这台机器没有 Node.js 或没放插件')
    import pet_music_ui
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: True)   # 别去改真配置
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    panel = p.open_music_panel()
    assert panel is not None
    assert panel.plugin_box.count() >= 1, '插件下拉里要有能搜歌的插件'
    assert panel.lyric_btn.isChecked() == p._music.lyric_on()
    # 再点一次不该重建（面板/播放器都是懒加载一份）
    again = p.open_music_panel()
    assert again is panel
    p._music.shutdown()
    QApplication.instance().processEvents()


def test_lyric_bubble_yields_to_speech_bubble(tmp_path, monkeypatch):
    """歌词和说话气泡抢同一块地方：说话时歌词藏起来，说完自动回来（实测需求）"""
    import pet_music_ui
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: True)
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    ui = pet_music_ui.MusicController(p)
    try:
        ui._lyric_on = True
        ui._lines = [(0.0, '第一句'), (5.0, '第二句')]

        ui._refresh_lyric_line(6.0)
        assert ui.lyric.isVisible(), '播放中应该显示当前那句'
        assert ui.lyric.text() == '第二句'

        p.bubble.setText('我说话呢')
        p.bubble.show()
        QApplication.instance().processEvents()
        ui._refresh_lyric_line(6.5)
        assert not ui.lyric.isVisible(), '说话时歌词要让位'
        assert ui._suspended is True

        p.bubble.hide()
        QApplication.instance().processEvents()
        ui._refresh_lyric_line(7.0)
        assert ui.lyric.isVisible(), '说完话歌词要自己回来'
        assert ui.lyric.text() == '第二句'

        # 关掉歌词开关 → 立刻收起
        ui.set_lyric_on(False)
        assert not ui.lyric.isVisible()
    finally:
        ui.shutdown()
        QApplication.instance().processEvents()


def test_lyric_bubble_position_follows_pet_size(tmp_path, monkeypatch):
    """歌词条得跟着窗口宽度走（扒边/改大小时不能被裁掉）"""
    import pet_music_ui
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: True)
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    ui = pet_music_ui.MusicController(p)
    try:
        ui.set_lyric_on(True)
        ui._lines = [(0.0, '故事的小黄花')]
        ui._refresh_lyric_line(1.0)
        QApplication.instance().processEvents()
        geo = ui.lyric.geometry()
        assert geo.width() <= p.width(), '不能比窗口还宽'
        assert geo.x() >= 0 and geo.x() + geo.width() <= p.width(), '得在窗口里水平居中'
    finally:
        ui.shutdown()
        QApplication.instance().processEvents()


def test_panel_subscription_buttons_and_progress(tmp_path, monkeypatch):
    """订阅导入的界面反馈：得有入口、有进度、失败要说明白（不能静默）"""
    import pet_music_ui
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: True)
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    ui = pet_music_ui.MusicController(p)
    try:
        panel = ui.panel()
        assert '订阅' in panel.sub_btn.text()
        assert '更新' in panel.update_btn.text()

        panel.on_import_progress(12, 57, 'QQ音乐')
        assert '12/57' in panel.status.text() and 'QQ音乐' in panel.status.text()

        panel.sub_btn.setEnabled(False)
        panel.on_import_done({'added': 5, 'updated': 1, 'skipped': 51, 'failed': []}, '')
        assert '装好 5 个' in panel.status.text() and '跳过 51' in panel.status.text()
        assert panel.sub_btn.isEnabled(), '导入结束要把按钮放开'

        panel.on_import_done(None, '网络断了')
        assert '失败' in panel.status.text() and '网络断了' in panel.status.text()
    finally:
        ui.shutdown()
        QApplication.instance().processEvents()


def test_subscription_url_remembered(tmp_path, monkeypatch):
    """填过的订阅地址要记住（「更新插件」靠它，不用每次重输）"""
    import pet_music_ui
    saved = {}
    monkeypatch.setattr(pet_music_ui, '_cfg_read', lambda: dict(saved))
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: saved.__setitem__(k, v) or True)
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import desktop_pet as dp
    p = dp.PetWidget()

    ui = pet_music_ui.MusicController(p)
    try:
        assert ui.subscriptions() == []
        ui._remember_subscription('http://a/plugins.json')
        ui._remember_subscription('http://b/plugins.json')
        ui._remember_subscription('http://a/plugins.json')
        assert ui.subscriptions() == ['http://a/plugins.json', 'http://b/plugins.json'], '最近用的排前面且不重复'
        assert ui.last_subscription() == 'http://a/plugins.json'
        ui.set_last_plugin('qq')
        assert ui.last_plugin() == 'qq'
    finally:
        ui.shutdown()


# ------------------------------------------------- 只给试听/片段时的处理（v6.84）
def test_pick_same_song_filters_clips_and_covers():
    """换源挑歌：片段不要、别人的翻唱不要、时长对得上才要"""
    from pet_music_ui import pick_same_song

    items = [
        {'title': '晴天（片段）', 'artist': '周杰伦', 'duration': 42},
        {'title': '晴天', 'artist': '某翻唱歌手', 'duration': 269},
        {'title': '晴天 (Live)', 'artist': '周杰伦', 'duration': 271},
        {'title': '晴天', 'artist': '周杰伦', 'duration': 269},
    ]
    best = pick_same_song(items, '晴天', '周杰伦', 269)
    assert best is not None and best['duration'] == 269, '该挑原版完整版，不是片段/翻唱'

    # 只有片段时宁可返回 None（说明这家也不行），不要拿 42 秒的糊弄
    assert pick_same_song([items[0]], '晴天', '周杰伦', 269) is None
    # 没有任何一条对得上歌名的
    assert pick_same_song([{'title': '稻香', 'artist': '周杰伦', 'duration': 223}], '晴天', '周杰伦', 269) is None


def test_truncated_source_is_reported(tmp_path, monkeypatch):
    """音源只给 1 分钟时要当场说清楚（这就是「为什么有些歌只能听一分钟」）"""
    import pet_music_ui
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: True)
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    c = pet_music_ui.MusicController(p)
    try:
        seen = []
        c.notice.connect(lambda s: seen.append(s))
        c._meta_dur, c._truncated = 269.0, 0.0
        c._check_truncated(60.0)
        assert c._truncated == 60.0
        assert any('只给到' in s for s in seen), '得提示「只给到 1:00」'
        assert any('换源' in s for s in seen), '还得告诉用户有换源这个出路'

        # 正常长度不许乱喊
        c._meta_dur, c._truncated = 313.0, 0.0
        seen.clear()
        c._check_truncated(313.4)
        assert c._truncated == 0.0 and not seen
        # 元数据本身很短（铃声/片段）就别当成问题
        c._meta_dur, c._truncated = 40.0, 0.0
        c._check_truncated(40.0)
        assert c._truncated == 0.0
    finally:
        c.shutdown()
        QApplication.instance().processEvents()


def test_panel_swap_button_hint(tmp_path, monkeypatch):
    """"🔀 换源"按钮得在，且音源不行时要亮出来"""
    import pet_music_ui
    monkeypatch.setattr(pet_music_ui, '_cfg_write', lambda k, v: True)
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    ui = pet_music_ui.MusicController(p)
    try:
        panel = ui.panel()
        assert '换源' in panel.swap_btn.text()
        panel.set_swap_hint(True)
        assert '⚠' in panel.swap_btn.text()
        panel.set_swap_hint(False)
        assert '⚠' not in panel.swap_btn.text()
    finally:
        ui.shutdown()
        QApplication.instance().processEvents()


def test_plugin_slug_keeps_chinese_names():
    """中文文件名的插件不能变成哈希名，不然一堆 plugin_xxxxxxxx.js 根本没法认"""
    import pet_music_free as mf
    assert mf.plugin_slug('https://x/plugins/%E5%85%83%E5%8A%9BQQ.js') == '元力QQ.js'
    assert mf.plugin_slug('https://x/plugins/元力QQ.js') == '元力QQ.js'
    assert mf.plugin_slug('https://x/酷狗JHMS.js') == '酷狗JHMS.js'


def test_clean_url_encodes_chinese_paths():
    """订阅里带中文/空格的地址，urllib 不编码会直接抛 ascii 错误"""
    import pet_music_free as mf
    u = mf._clean_url('https://a.com/p/元力QQ.js?v=1 2')
    assert ' ' not in u
    assert u.startswith('https://a.com/p/')
    assert '%E5%85%83' in u, '中文要编码'
    assert 'v=1' in u
