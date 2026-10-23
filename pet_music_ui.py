# -*- coding: utf-8 -*-
"""pet_music_ui.py — 音乐面板 + 头顶歌词气泡（v6.82）

分工：
    pet_music.py        mpv 播放内核（本地文件 / 网络直链）
    pet_music_free.py   MusicFree 插件桥（搜歌 / 取音源）
    pet_music_lyrics.py 歌词抓取（LRCLIB 优先，插件兜底）
    本模块               界面与调度：音乐面板、播放队列、歌词气泡

设计要点（都是踩过的坑）：
- 搜歌和取音源都要走网络，一律后台线程；GUI 线程只更新控件
- 播放队列按「条目」排队，播到哪首才去取哪首的音源（bilibili 直链有时效，不能提前全取）
- 歌词气泡和说话气泡抢同一块地方 → 只让一个显示：说话时歌词暂时让位，说完自动回来
"""
import json
import os
import re
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSlider, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QHeaderView, QFileDialog, QAbstractItemView, QInputDialog,
)

from pet_music import MusicPlayer, Track, read_track, is_audio, AUDIO_EXTS
import pet_music_free as free_bridge
import pet_music_lyrics as lyrics_mod
import pet_theme
import pet_window as pwin  # v6.90 每个窗口标题栏的「—」隐藏键

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

# 实测可用的插件订阅（57 个音源：QQ/网易云/酷狗/酷我/咪咕/汽水/B站/喜马拉雅…）
DEFAULT_SUBSCRIPTION = 'https://hebijunge.github.io/musicfree-plugins/plugins.json'

# 更多订阅（同样实测可装；qwerwhr 是 76 个插件的合集，meerl 那个每天自动检测可用性）
EXTRA_SUBSCRIPTIONS = (
    'https://qwerwhr.github.io/musicfree-plugins/plugins.json',
    'https://musicfreepluginshub.2020818.xyz/plugins.json',
)

# 下拉框排序用：这几个主流源排最前面，其余按名字排
MAIN_PLATFORMS = ('qq', 'netease', 'kugou', 'kuwo', 'migu', 'qishui', 'bilibili', 'ximalaya')


def _cfg_read():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _cfg_write(key, value):
    """只改一个键（读-改-写，保留别人的字段）"""
    try:
        cfg = _cfg_read()
        cfg[key] = value
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def fmt_time(sec):
    try:
        sec = int(max(0, float(sec or 0)))
    except Exception:
        sec = 0
    return '%d:%02d' % (sec // 60, sec % 60)


def _norm_text(s):
    """比对歌名/歌手用：去掉空格标点括号，中文全角半角一视同仁（粗糙但够用）"""
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', str(s or '').lower())


def pick_same_song(items, title, artist='', want=0.0):
    """在别的音源的搜索结果里挑「同一首歌的完整版」

    为什么要有这套：同一个音源常常只给会员试听（1 分钟就没了），而别的音源
    可能正好有完整版。挑的时候三条都要过：
      - 歌名规范化后相互包含（「晴天 (Live)」也算同一首，「日不落」不算）
      - 歌手对得上（两边都有歌手时才比，避免把别人的翻唱当原曲）
      - 时长接近原曲（差太多的是片段/剪辑版，直接排掉——这正是"只能听一分钟"的元凶）
    """
    t = _norm_text(title)
    a = _norm_text(artist)
    if not t:
        return None
    best, best_score = None, None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        ti = _norm_text(it.get('title'))
        if not ti or not (t in ti or ti in t):
            continue
        ai = _norm_text(it.get('artist'))
        if a and ai and a not in ai and ai not in a:
            continue
        d = float(it.get('duration') or 0)
        if want >= 90 and d > 0 and abs(d - want) > max(10.0, want * 0.08):
            continue
        score = (0 if (a and ai) else 1, abs((d or want) - want) if want else 0)
        if best_score is None or score < best_score:
            best, best_score = it, score
    return best


# 面板要用的主题 token：全部走 pet_theme，不在本模块写任何颜色字面量
# （v6.57 的护栏 tests/test_theme_tokens.py 会扫产品代码里的硬编码颜色）
_UI_TOKENS = (
    'dialog_bg', 'text', 'input_bg', 'input_text', 'list_bg', 'list_border',
    'list_sel_bg', 'list_sel_text', 'item_bg', 'ui_btn_bg', 'ui_btn_hover',
    'ui_btn_alt', 'ui_accent', 'ui_text', 'ui_text_strong', 'ui_text_dim',
    'ui_border_soft', 'ui_popup_bg', 'accent', 'hint_text',
)


def _tokens(pet):
    """按当前主题取一组颜色（切主题后重新调一次即可跟着变）"""
    out = {}
    for key in _UI_TOKENS:
        try:
            out[key] = pet._tk(key)
        except Exception:
            out[key] = pet_theme.color(key)
    return out


class LyricBubble(QLabel):
    """宠物头顶的歌词条（跟着当前句滚动；说话气泡出现时自动让位）"""

    def __init__(self, parent, pet):
        super(LyricBubble, self).__init__(parent)
        self._pet = pet
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setMaximumWidth(400)
        self.hide()

    def restyle(self):
        try:
            pet = self._pet
            tk = _tokens(pet)
            bg = pet._solid(tk['ui_popup_bg']) if hasattr(pet, '_solid') else tk['ui_popup_bg']
            self.setStyleSheet(
                'QLabel{background:%s;color:%s;border-radius:12px;'
                'border:1px solid %s;'
                'padding:5px 12px;font-size:13px;font-weight:600;}' % (bg, tk['accent'], tk['list_border']))
        except Exception:
            pass

    def place(self):
        """贴在宠物头顶（跟说话气泡同一块区域，居中）"""
        try:
            avail = max(60, self._pet.width() - 8)
            self.ensurePolished()
            hint = self.sizeHint()
            bw = min(max(hint.width(), 60), 360, avail)
            bh = max(26, min(hint.height(), 96))
            bx = max(0, (self._pet.width() - bw) // 2)
            self.setGeometry(bx, 6, bw, bh)
            self.raise_()
        except Exception:
            pass

    def set_line(self, text):
        text = (text or '').strip()
        if not text:
            self.hide()
            return
        if self.text() != text:
            self.setText(text)
            self.place()
        self.show()
        self.raise_()


class MusicPanel(QDialog):
    """音乐面板：搜歌 → 双击播放；播放列表、进度、音量、歌词开关"""

    def __init__(self, controller, parent):
        super(MusicPanel, self).__init__(parent)
        self.c = controller
        self.setWindowTitle('🎵 音乐')
        # v6.90：原来是 Qt.Tool（不进任务栏），但工具窗系统不给最小化键 →
        # 换成普通窗口并配上「—」（代价：进任务栏，正好跟浏览器一样，隐藏后点得回来）
        pwin.add_hide_button(self)
        self.resize(560, 520)
        self.setStyleSheet(self._qss())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # 搜索行
        row = QHBoxLayout()
        self.plugin_box = QComboBox()
        self.plugin_box.setMinimumWidth(120)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('搜歌名 / 歌手（Enter 搜索）')
        self.search_btn = QPushButton('🔍 搜索')
        self.sub_btn = QPushButton('➕ 订阅')
        self.sub_btn.setToolTip('粘贴 MusicFree 插件订阅地址（plugins.json），一键装/更新全部音源')
        row.addWidget(self.plugin_box)
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.search_btn)
        row.addWidget(self.sub_btn)
        lay.addLayout(row)
        self.status = self._dim(QLabel(''))
        lay.addWidget(self.status)

        # 结果表
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['歌名', '歌手', '时长'])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 60)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table, 3)

        # 播放列表
        tip = self._dim(QLabel('播放列表（双击切歌）'))
        lay.addWidget(tip)
        self.playlist = QListWidget()
        lay.addWidget(self.playlist, 2)

        # 进度
        prow = QHBoxLayout()
        self.time_lbl = self._dim(QLabel('0:00 / 0:00'))
        self.progress = QSlider(Qt.Horizontal)
        self.progress.setRange(0, 1000)
        prow.addWidget(self.time_lbl)
        prow.addWidget(self.progress, 1)
        lay.addLayout(prow)

        # 控制行
        crow = QHBoxLayout()
        self.prev_btn = QPushButton('⏮')
        self.play_btn = QPushButton('▶')
        self.next_btn = QPushButton('⏭')
        for b in (self.prev_btn, self.play_btn, self.next_btn):
            b.setFixedWidth(44)
            crow.addWidget(b)
        crow.addWidget(QLabel('音量'))
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(110)
        crow.addWidget(self.vol)
        self.lyric_btn = QPushButton('☁ 歌词上头顶')
        self.lyric_btn.setCheckable(True)
        crow.addWidget(self.lyric_btn)
        self.local_btn = QPushButton('📂 本地文件')
        crow.addWidget(self.local_btn)
        self.swap_btn = QPushButton('🔀 换源')
        self.swap_btn.setToolTip('这首只有试听/放不完时，用同一首歌去别的音源找一遍完整版')
        crow.addWidget(self.swap_btn)
        self.update_btn = QPushButton('🔄 更新插件')
        self.update_btn.setToolTip('重新拉一遍已保存的订阅地址（已是最新的会跳过）')
        crow.addWidget(self.update_btn)
        crow.addStretch(1)
        lay.addLayout(crow)

        self.now = QLabel('未播放')
        self.now.setStyleSheet('color:%s;font-size:12px;font-weight:600;' % _tokens(self.c.pet)['ui_text'])
        lay.addWidget(self.now)

        # 信号
        self.search_btn.clicked.connect(self.do_search)
        self.search_edit.returnPressed.connect(self.do_search)
        self.table.cellDoubleClicked.connect(self.on_pick)
        self.playlist.itemDoubleClicked.connect(self.on_playlist_pick)
        self.play_btn.clicked.connect(self.c.toggle)
        self.prev_btn.clicked.connect(self.c.prev)
        self.next_btn.clicked.connect(self.c.next)
        self.vol.valueChanged.connect(self.c.set_volume)
        self.lyric_btn.toggled.connect(self.c.set_lyric_on)
        self.local_btn.clicked.connect(self.on_local_files)
        self.swap_btn.clicked.connect(self.c.retry_other_source)
        self.sub_btn.clicked.connect(self.on_subscribe)
        self.update_btn.clicked.connect(self.on_update_plugins)
        self.progress.sliderReleased.connect(self.on_seek)
        self.plugin_box.currentIndexChanged.connect(self.on_plugin_changed)
        self.c.import_progress.connect(self.on_import_progress)
        self.c.import_done.connect(self.on_import_done)

        self.refresh_plugins()
        self.vol.setValue(self.c.volume())
        self.lyric_btn.setChecked(self.c.lyric_on())
        self.refresh_playlist()

    def _dim(self, w):
        """次要文字（时间/状态/提示）：一律用主题里的弱化色"""
        try:
            w.setStyleSheet('color:%s;font-size:11px;' % _tokens(self.c.pet)['ui_text_dim'])
        except Exception:
            pass
        return w

    def _qss(self):
        t = _tokens(self.c.pet)
        return (
            'QDialog{background:%(dialog_bg)s;}'
            'QLabel{color:%(text)s;font-size:12px;}'
            'QLineEdit,QComboBox{background:%(input_bg)s;color:%(input_text)s;'
            'border:1px solid %(list_border)s;border-radius:6px;padding:5px 8px;font-size:12px;}'
            'QPushButton{background:%(ui_btn_bg)s;color:%(ui_text)s;border:none;border-radius:6px;'
            'padding:6px 10px;font-size:12px;}'
            'QPushButton:hover{background:%(ui_btn_hover)s;}'
            'QPushButton:checked{background:%(ui_accent)s;color:%(ui_text_strong)s;}'
            'QTableWidget,QListWidget{background:%(list_bg)s;color:%(text)s;'
            'border:1px solid %(list_border)s;border-radius:6px;'
            'alternate-background-color:%(item_bg)s;font-size:12px;}'
            'QTableWidget::item:selected,QListWidget::item:selected{'
            'background:%(list_sel_bg)s;color:%(list_sel_text)s;}'
            'QHeaderView::section{background:%(ui_btn_alt)s;color:%(ui_text_dim)s;'
            'border:none;padding:5px;}'
            'QSlider::groove:horizontal{height:6px;border-radius:3px;background:%(ui_border_soft)s;}'
            'QSlider::handle:horizontal{width:13px;margin:-4px 0;border-radius:6px;'
            'background:%(ui_accent)s;}'
            % t
        )

    # ---------- 列表 ----------
    def refresh_plugins(self, keep=None):
        """重填插件下拉：显示名用订阅清单里的中文名，值仍是插件 platform"""
        want = keep or self.plugin_box.currentData() or self.c.last_plugin()
        self.plugin_box.blockSignals(True)
        self.plugin_box.clear()
        for title, platform in self.c.music_plugins():
            self.plugin_box.addItem(title, platform)
        if self.plugin_box.count() == 0:
            self.plugin_box.addItem('（还没有插件，点「➕ 订阅」装一批）', '')
        else:
            idx = self.plugin_box.findData(want) if want else -1
            if idx >= 0:
                self.plugin_box.setCurrentIndex(idx)
        self.plugin_box.blockSignals(False)

    def on_plugin_changed(self, *_):
        self.c.set_last_plugin(self.plugin_box.currentData() or '')

    def refresh_playlist(self):
        self.playlist.clear()
        for i, (plugin, item) in enumerate(self.c.queue()):
            title = str(item.get('title') or '未知曲目')
            artist = str(item.get('artist') or '')
            it = QListWidgetItem(('▶ ' if i == self.c.queue_index() else '   ') +
                                 (title + (' - ' + artist if artist else '')))
            self.playlist.addItem(it)

    def set_status(self, text):
        self.status.setText(text or '')

    def set_now(self, text):
        self.now.setText(text or '')

    def set_progress(self, pos, dur):
        self.time_lbl.setText('%s / %s' % (fmt_time(pos), fmt_time(dur)))
        if not self.progress.isSliderDown() and dur > 0:
            self.progress.setValue(int(max(0, min(1.0, pos / dur)) * 1000))

    def set_playing_icon(self, playing):
        self.play_btn.setText('⏸' if playing else '▶')

    def set_swap_hint(self, on):
        """当前这首音源不行（只给试听 / 播一半断了）→ 换源按钮亮起来"""
        try:
            self.swap_btn.setText('🔀 换源 ⚠' if on else '🔀 换源')
        except Exception:
            pass

    # ---------- 交互 ----------
    def do_search(self):
        plugin = self.plugin_box.currentData()
        kw = self.search_edit.text().strip()
        if not plugin or not kw:
            self.set_status('先选插件并输入关键词')
            return
        self.set_status('正在搜索…')
        self.c.search(plugin, kw)

    def on_pick(self, row, _col):
        try:
            item = self.c.search_result(row)
        except Exception:
            return
        if item:
            self.c.play_search_result(row)

    def on_playlist_pick(self, _item):
        row = self.playlist.currentRow()
        if row >= 0:
            self.c.play_queue_at(row)

    def on_seek(self):
        dur = self.c.duration()
        if dur > 0:
            self.c.seek(dur * self.progress.value() / 1000.0)

    def on_local_files(self):
        exts = ' '.join('*' + e for e in AUDIO_EXTS)
        files, _ = QFileDialog.getOpenFileNames(self, '选择音乐文件', '', '音频 (%s)' % exts)
        if files:
            self.c.add_local(files)

    # ---------- 插件订阅 ----------
    def on_subscribe(self):
        """粘贴订阅地址 → 后台装插件（默认填实测可用的那个 57 源订阅）"""
        default = self.c.last_subscription() or DEFAULT_SUBSCRIPTION
        url, ok = QInputDialog.getText(
            self, '插件订阅',
            'MusicFree 插件订阅地址（plugins.json）：\n'
            '例：%s' % DEFAULT_SUBSCRIPTION,
            text=default)
        if ok and url.strip():
            self.sub_btn.setEnabled(False)
            self.update_btn.setEnabled(False)
            self.set_status('正在读取订阅…')
            self.c.import_subscription(url.strip())

    def on_update_plugins(self):
        urls = self.c.subscriptions() or [DEFAULT_SUBSCRIPTION]
        self.sub_btn.setEnabled(False)
        self.update_btn.setEnabled(False)
        self.set_status('正在检查更新…')
        self.c.import_subscription(urls, update_all=True)

    def on_import_progress(self, done, total, text):
        self.set_status('插件 %d/%d · %s' % (done, total, text))

    def on_import_done(self, report, err):
        self.sub_btn.setEnabled(True)
        self.update_btn.setEnabled(True)
        if err:
            self.set_status('订阅导入失败：%s' % err)
            return
        report = report or {}
        msg = ('装好 %d 个、更新 %d 个、跳过 %d 个（已最新）'
               % (report.get('added', 0), report.get('updated', 0), report.get('skipped', 0)))
        if report.get('failed'):
            msg += ' · 失败 %d 个' % len(report['failed'])
        self.set_status(msg + '，插件列表已刷新')
        self.refresh_plugins()
        if report.get('failed'):
            detail = '；'.join('%s（%s）' % (n, r) for n, r in report['failed'][:3])
            self.c.notice.emit('🎵 部分插件没装上：%s' % detail)


class MusicController(QObject):
    """把播放内核、插件、歌词、界面串起来（桌宠只跟它打交道）"""

    search_done = Signal(list, str)      # 结果项, 错误
    play_ready = Signal(object, str)     # Track, 错误
    notice = Signal(str)
    status_msg = Signal(str)             # 后台线程改状态条（不能直接碰控件）
    import_progress = Signal(int, int, str)   # 已完成, 总数, 当前插件名
    import_done = Signal(object, str)         # 报告, 错误

    def __init__(self, pet):
        super(MusicController, self).__init__(pet)
        self.pet = pet
        self.host = free_bridge.PluginHost(timeout=25.0)
        self.player = MusicPlayer(self)
        self.player.auto_next_callback = self._on_track_end   # 队列自己管，不用内核的线性播放
        self.lyric = LyricBubble(pet, pet)
        self.lyric.restyle()
        self._suspended = False

        cfg = _cfg_read()
        self._lyric_on = bool(cfg.get('music_lyric_on', True))
        self._volume = int(cfg.get('music_volume', 70) or 70)
        self.player.set_volume(self._volume)

        self._queue = []            # [(plugin, item)]
        self._qi = -1
        self._results = []          # 搜索结果
        self._lines = []            # 当前歌词 [(秒, 词)]
        self._line_now = ''
        self._cur_title = ''
        self._panel = None
        self._fetching_title = ''
        self._meta_dur = 0.0        # 曲目「应该有多长」（来自搜索结果元数据）
        self._truncated = 0.0       # 实际只给到的长度（音源只给试听时才有值）

        self.player.positionChanged.connect(self._on_pos)
        self.player.playingChanged.connect(self._on_playing)
        self.player.failed.connect(self._on_failed)
        self.player.interrupted.connect(self._on_interrupted)
        self.search_done.connect(self._on_search_done)
        self.play_ready.connect(self._on_play_ready)
        self.status_msg.connect(self._status)

        self._fetcher = lyrics_mod.LyricFetcher(self.host, self)
        self._fetcher.done.connect(self._on_lyric_fetched)

    # ---------- 配置 ----------
    def volume(self):
        return self._volume

    def lyric_on(self):
        return self._lyric_on

    def set_lyric_on(self, on):
        self._lyric_on = bool(on)
        _cfg_write('music_lyric_on', self._lyric_on)
        if not self._lyric_on:
            self.lyric.hide()
        else:
            self._refresh_lyric_line(force=True)

    def set_volume(self, v):
        self._volume = int(max(0, min(100, v)))
        self.player.set_volume(self._volume)
        _cfg_write('music_volume', self._volume)

    def last_plugin(self):
        return str(_cfg_read().get('music_plugin', '') or '')

    def set_last_plugin(self, platform):
        if platform and platform != self.last_plugin():
            _cfg_write('music_plugin', platform)

    def subscriptions(self):
        """用过的订阅地址（更新插件时逐个重拉）"""
        subs = _cfg_read().get('music_subscriptions') or []
        return [s for s in subs if isinstance(s, str) and s.strip()]

    def last_subscription(self):
        subs = self.subscriptions()
        return subs[0] if subs else ''

    def _remember_subscription(self, url):
        subs = [s for s in self.subscriptions() if s != url]
        subs.insert(0, url)
        _cfg_write('music_subscriptions', subs[:5])

    # ---------- 面板 ----------
    def panel(self):
        if self._panel is None:
            self._panel = MusicPanel(self, self.pet)
        pwin.present(self._panel)       # v6.90：隐藏着的面板要能一键叫回来
        return self._panel

    def _ui(self):
        return self._panel if self._panel is not None else None

    def _status(self, text):
        p = self._ui()
        if p:
            p.set_status(text)

    # ---------- 插件 ----------
    def music_plugins(self):
        """能搜歌的插件 → [(显示名, 插件文件名)]

        值用**文件名**而不是 platform：同一家（QQ/网易云…）可能同时装了好几套变体，
        platform 全叫 qq/netease，只有文件名能区分，否则选了也没用（会被解析到第一个）。
        显示名优先用订阅清单里的中文名（元力QQ / 网易(念心音源)…）。
        排序：主流几大家的排前面（订阅装多了有一百多个源，不排根本找不着）。
        """
        out = []
        try:
            titles = free_bridge.plugin_titles()
            for p in self.host.list_plugins():
                if p.get('error'):
                    continue
                methods = p.get('methods') or []
                stats = p.get('supportedSearchType') or []
                if 'search' not in methods or (stats and 'music' not in stats):
                    continue
                key = p.get('file') or p.get('platform') or ''
                if not key:
                    continue
                platform = p.get('platform') or ''
                out.append((titles.get(key) or platform or key, key, platform))
        except Exception as exc:
            self.notice.emit('插件读取失败：%s' % exc)

        def rank(item):
            low = (item[2] or '').lower()
            for i, name in enumerate(MAIN_PLATFORMS):
                if low == name:
                    return (i, 0, item[0])
                if name in low:
                    return (i, 1, item[0])
            return (len(MAIN_PLATFORMS), 0, item[0])

        out.sort(key=rank)
        # 同名插件（三份 5sing、两份 bilibili…）加一句文件名区分，不然下拉框里一模一样
        counts = {}
        for title, _k, _p in out:
            counts[title] = counts.get(title, 0) + 1
        fixed = []
        for title, key, _p in out:
            if counts[title] > 1:
                title = '%s · %s' % (title, os.path.splitext(key)[0])
            fixed.append((title, key))
        return fixed

    # ---------- 插件订阅（v6.83） ----------
    def import_subscription(self, url_or_urls, update_all=False):
        """后台装/更新插件；progress 与结果都走信号回 GUI 线程"""
        urls = [url_or_urls] if isinstance(url_or_urls, str) else list(url_or_urls or [])
        urls = [u for u in urls if str(u).strip()]

        def work():
            total_report = {'added': 0, 'updated': 0, 'skipped': 0, 'failed': [],
                            'total': 0, 'names': []}
            try:
                for url in urls:
                    def prog(done, total, text, _u=url):
                        self.import_progress.emit(done, total, text)
                    rep = free_bridge.install_subscription(url, progress=prog)
                    for key in ('added', 'updated', 'skipped', 'total'):
                        total_report[key] += int(rep.get(key) or 0)
                    total_report['failed'].extend(rep.get('failed') or [])
                    total_report['names'].extend(rep.get('names') or [])
            except Exception as exc:
                self.import_done.emit(None, str(exc))
                return
            # 文件换了内容必须让 Node 丢掉 require 缓存，否则还是旧插件
            try:
                self.host.reload()
            except Exception:
                pass
            for url in urls:
                self._remember_subscription(url)
            self.import_done.emit(total_report, '')

        threading.Thread(target=work, daemon=True).start()

    # ---------- 搜索 ----------
    def search(self, plugin, keyword):
        def work():
            try:
                items = self.host.search(plugin, keyword, limit=40)
                self.search_done.emit(items, '')
            except Exception as exc:
                self.search_done.emit([], str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_search_done(self, items, err):
        self._results = list(items or [])
        p = self._ui()
        if p is not None:
            p.table.setRowCount(0)
            for it in self._results:
                r = p.table.rowCount()
                p.table.insertRow(r)
                p.table.setItem(r, 0, QTableWidgetItem(str(it.get('title') or '')))
                p.table.setItem(r, 1, QTableWidgetItem(str(it.get('artist') or '')))
                dur = float(it.get('duration') or 0)
                cell = QTableWidgetItem(fmt_time(dur))
                if 0 < dur < 90:
                    # 搜索结果里本来就混着「42 秒片段」这类条目，先标出来，省得听一半才发现
                    cell.setToolTip('只有 %s：多半是片段/试听，播到这儿就完了' % fmt_time(dur))
                p.table.setItem(r, 2, cell)
            if err:
                p.set_status('搜索失败：%s' % err)
            elif not self._results:
                p.set_status('这个插件没搜到（换个音源试试：曲库各不相同）')
            else:
                p.set_status('共 %d 条（双击播放）' % len(self._results))
        if err:
            self.notice.emit('搜索失败：%s' % err)

    def search_result(self, row):
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    # ---------- 队列与播放 ----------
    def queue(self):
        return self._queue

    def queue_index(self):
        return self._qi

    def duration(self):
        tr = self.player.current
        try:
            if self.player._ipc and self.player._ipc.alive:
                d = self.player._ipc.get('duration')
                if d:
                    return float(d)
        except Exception:
            pass
        return float(getattr(tr, 'duration', 0) or 0)

    def seek(self, sec):
        self.player.seek(sec)

    def toggle(self):
        self.player.toggle()
        p = self._ui()
        if p:
            p.set_playing_icon(self.player.playing)

    def prev(self):
        if self._qi > 0:
            self.play_queue_at(self._qi - 1)
        else:
            self.player.seek(0)

    def next(self):
        if self._qi + 1 < len(self._queue):
            self.play_queue_at(self._qi + 1)
        else:
            self.player.stop()

    def _on_track_end(self):
        """一首放完（内核回调）→ 队列下一首"""
        if self._qi + 1 < len(self._queue):
            self.play_queue_at(self._qi + 1)
        else:
            self.player._set_playing(False)

    def play_search_result(self, row):
        item = self.search_result(row)
        if not item:
            return
        plugin = (self._ui().plugin_box.currentData() if self._ui() else '') or item.get('platform') or ''
        self._queue = [(plugin, it) for it in self._results]
        self.play_queue_at(row)

    # ---------- 换源（同一个音源只给试听/放不动时换一家） ----------
    def current_entry(self):
        if 0 <= self._qi < len(self._queue):
            return self._queue[self._qi]
        return None

    def retry_other_source(self):
        """拿当前这首的歌名去别的音源再找一遍，挑时长对得上的版本播

        为什么需要：各家音源对会员曲的待遇不一样（有的给 128k 完整版、有的只给 60 秒试听、
        有的干脆没有），所以「换一家」往往比「换一首」有效。
        """
        cur = self.current_entry()
        if not cur:
            self._status('先放一首歌，再点换源')
            return
        plugin, item = cur
        title = str(item.get('title') or '').strip()
        artist = str(item.get('artist') or '').strip()
        if not title:
            self._status('这首没有歌名，换源没法找')
            return
        want = float(self._meta_dur or item.get('duration') or 0)
        self._status('正在别的音源里找《%s》…' % title)

        def work():
            prefer = ('qq', 'netease', 'kugou', 'kuwo', 'migu', 'qishui', 'bilibili')
            try:
                info = {p['file']: (p.get('platform') or '')
                        for p in self.host.list_plugins()
                        if not p.get('error') and 'search' in (p.get('methods') or [])}
            except Exception:
                info = {}

            def fam_of(key):
                plat = (info.get(key) or key).lower()
                for name in prefer:
                    if name in plat:
                        return name
                return plat

            def rank(key):
                fam = fam_of(key)
                return prefer.index(fam) if fam in prefer else len(prefer)

            # 同一个平台最多试两家（订阅读多了会有十一份网易云，全试一遍得等到明年）
            per_fam, tried = {}, 0
            queue_keys = sorted([k for k in info if k != plugin], key=rank)
            for key in queue_keys:
                fam = fam_of(key)
                if per_fam.get(fam, 0) >= 2:
                    continue
                per_fam[fam] = per_fam.get(fam, 0) + 1
                tried += 1
                if tried > 12:
                    break
                try:
                    items = self.host.search(key, (title + ' ' + artist).strip(), limit=10)
                except Exception:
                    continue
                best = pick_same_song(items, title, artist, want)
                if not best:
                    continue
                try:
                    src = self.host.media(key, best)
                except Exception:
                    continue
                tr = Track(src['url'],
                           title=str(best.get('title') or title),
                           artist=str(best.get('artist') or artist),
                           album=str(best.get('album') or ''),
                           duration=float(best.get('duration') or 0),
                           headers=src.get('headers'), plugin=key, extra=best)
                self._queue = [(key, best)]
                self._qi = 0
                self.status_msg.emit('已换到「%s」播《%s》' % (key, title))
                self.play_ready.emit(tr, '')
                return
            self.status_msg.emit('别的音源也没有《%s》的完整版（这类会员/独家曲目确实拿不到）' % title)

        threading.Thread(target=work, daemon=True).start()

    def add_local(self, files):
        added = 0
        for f in files:
            if os.path.isfile(f) and is_audio(f):
                self._queue.append(('', {'title': os.path.basename(f), 'path': f, 'local': True}))
                added += 1
        p = self._ui()
        if p:
            p.refresh_playlist()
            p.set_status('已加 %d 首本地音乐' % added)
        if added and self._qi < 0:
            self.play_queue_at(0)

    def play_queue_at(self, i):
        if not (0 <= i < len(self._queue)):
            return
        self._qi = i
        plugin, item = self._queue[i]
        p = self._ui()
        if p:
            p.refresh_playlist()
            p.set_status('正在取音源…')

        # 本地文件：直接播
        path = item.get('path')
        if item.get('local') and path:
            tr = read_track(path)
            self._start_track(tr)
            return

        def work():
            try:
                src = self.host.media(plugin, item)
                tr = Track(
                    src['url'],
                    title=str(item.get('title') or ''),
                    artist=str(item.get('artist') or ''),
                    album=str(item.get('album') or ''),
                    duration=float(item.get('duration') or 0),
                    headers=src.get('headers'),
                    plugin=plugin,
                    extra=item,
                )
                self.play_ready.emit(tr, '')
            except Exception as exc:
                self.play_ready.emit(None, str(exc))

        threading.Thread(target=work, daemon=True).start()

    def _on_play_ready(self, tr, err):
        if err or tr is None:
            msg = '播放失败：%s' % (err or '未知原因')
            self._status(msg)
            self.notice.emit(msg)
            return
        self._start_track(tr)

    def _start_track(self, tr):
        self._cur_title = tr.title or ''
        self._lines = []
        self._line_now = ''
        self._meta_dur = float(getattr(tr, 'duration', 0) or 0)
        self._truncated = 0.0
        self.lyric.hide()
        self.player.set_tracks([tr])
        p = self._ui()
        if p:
            p.set_now('🎵 ' + tr.label())
            p.set_playing_icon(True)
            p.set_swap_hint(False)
            p.refresh_playlist()
            p.set_status('正在播放')
        if self._lyric_on:
            self._fetcher.start(tr.title, tr.artist, tr.duration, tr.album,
                                plugin=getattr(tr, 'plugin', ''), item=getattr(tr, 'extra', None))
            self._fetching_title = tr.title

    # ---------- 歌词 ----------
    def _on_lyric_fetched(self, title, raw):
        try:
            self._lines = lyrics_mod.parse_lrc(raw) if raw else []
        except Exception:
            self._lines = []
        if self._lines:
            self._refresh_lyric_line(force=True)
            self.notice.emit('🎼 已载入歌词：%d 句' % len(self._lines))
        else:
            self._status('这首歌没找到歌词（不影响播放）')

    def _on_pos(self, pos, dur):
        p = self._ui()
        if p:
            p.set_progress(pos, dur)
        self._check_truncated(dur)
        if self._lyric_on:
            self._refresh_lyric_line(pos)

    def _check_truncated(self, dur):
        """音源只给试听/片段时，播到 1 分钟就没了 —— 当场说清楚，别让人以为播放器坏了

        判据：mpv 报的真实总长 < 元数据时长的 80%（且元数据本身够长）。
        只有真播起来之后才知道实际多长，所以放在位置回调里判一次。
        """
        try:
            dur = float(dur or 0)
        except Exception:
            return
        if self._truncated or dur <= 0 or self._meta_dur < 90:
            return
        if dur >= self._meta_dur * 0.8:
            return
        self._truncated = dur
        msg = ('这首音源只给到 %s（标注 %s）—— 多半是会员试听/片段，点「🔀 换源」换个音源试试'
               % (fmt_time(dur), fmt_time(self._meta_dur)))
        self._status(msg)
        self.notice.emit('🎵 ' + msg)
        p = self._ui()
        if p:
            p.set_swap_hint(True)

    def _on_interrupted(self, reason):
        """播到一半被掐断（直链过期/防盗链限流/网络断）——记一笔，提示换源"""
        msg = '播放中断：%s' % (reason or '音源断开')
        self._status(msg)
        self.notice.emit('🎵 %s（点「🔀 换源」试试别的音源）' % msg)
        p = self._ui()
        if p:
            p.set_swap_hint(True)

    def _refresh_lyric_line(self, pos=None, force=False):
        if not self._lyric_on:
            self.lyric.hide()
            return
        # 说话气泡在显示时，歌词让位（两者抢同一块地方）；气泡收起后自动回来
        try:
            busy = self.pet.bubble.isVisible() or getattr(self.pet, '_speak_busy', False)
        except Exception:
            busy = False
        if busy:
            self._suspended = True
            self.lyric.hide()
            return
        if getattr(self, '_suspended', False):
            self._suspended = False
            force = True
        if pos is None:
            try:
                pos = float(self.player._ipc.get('time-pos') or 0) if self.player._ipc else 0.0
            except Exception:
                pos = 0.0
        line = ''
        for t, text in self._lines:
            if t <= pos:
                line = text
            else:
                break
        if not line and self._lines:
            line = ''       # 前奏没到第一句就不显示
        if line == self._line_now and not force:
            return
        self._line_now = line
        if line:
            self.lyric.restyle()
            self.lyric.place()
            self.lyric.set_line(line)
        else:
            self.lyric.hide()

    def _on_playing(self, playing):
        p = self._ui()
        if p:
            p.set_playing_icon(playing)
        if not playing:
            self.lyric.hide()

    def _on_failed(self, err):
        msg = '播放失败：%s' % err
        self._status(msg)
        self.notice.emit(msg)

    # ---------- 收尾 ----------
    def shutdown(self):
        try:
            self.player.shutdown()
        except Exception:
            pass
        try:
            self.host.close()
        except Exception:
            pass
