# -*- coding: utf-8 -*-
"""pet_voice_ui.py — 「本地声线库」面板（v6.92）

一句话：**把 GitHub/HuggingFace 上免费开源的预训练中文声线，装进朗读声线列表。**

分工：
    pet_voicelib.py    内核：模型清单 MODELS、说话人枚举、sherpa-onnx 合成（不懂界面）
    tools/fetch_voices.py 下载器：按清单只取推理必需文件，可续传（可单独命令行跑）
    本模块              界面：挑模型 → 挑音色（搜索/试听/应用）→ 缺的模型一键下载

为什么单独开一个面板，而不是把 2000 多个声线塞进设置页下拉：
- 8 个模型合计 2000+ 说话人（theresa/eula 各 804 个），全塞下拉框那不叫列表，叫折磨；
- 所以设置页下拉只放「每个模型 5 个代表音色」，想要别的就来这里翻、搜、试听。

颜色一律走 pet_theme 的 token（tests/test_theme_tokens.py 会扫硬编码颜色）。
下载与合成都在后台线程，GUI 线程只更新控件（这规矩跟下载面板/音乐面板一致）。
"""

import os
import subprocess
import sys
import tempfile
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

import pet_voicelib as vlib
import pet_window as pwin

SAMPLE_TEXT = '你好呀，我是你的桌宠，这是这个音色的声音。'

# 试听音频放临时目录，别污染项目
PREVIEW_DIR = os.path.join(tempfile.gettempdir(), 'pet_voice_lib')


def _tokens(pet):
    """按当前主题取一组颜色（切主题后重新取一次即可跟着变）"""
    import pet_theme
    out = {}
    for key in ('dialog_bg', 'text', 'input_bg', 'input_text', 'list_bg', 'list_border',
                'list_sel_bg', 'list_sel_text', 'ui_btn_bg', 'ui_btn_hover',
                'ui_accent', 'ui_text', 'ui_text_strong', 'ui_text_dim'):
        try:
            out[key] = pet._tk(key)
        except Exception:
            try:
                out[key] = pet_theme.color(key)
            except Exception:
                out[key] = ''
    return out


def _play(path):
    """试听：winsound 异步播（跟 voice_io 的播放方式一致，不额外拉依赖）"""
    try:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True
    except Exception:
        return False


class VoiceLibraryDialog(QDialog):
    """本地声线库：左挑模型，右挑音色（可搜索/试听/应用），缺的模型可一键下载"""

    def __init__(self, host, parent=None):
        super(VoiceLibraryDialog, self).__init__(parent)
        self.host = host
        self.setWindowTitle('🎚 本地声线库（免费预训练声线）')
        pwin.add_hide_button(self)          # v6.90：普通窗口 + 标题栏「—」隐藏键
        self.resize(780, 520)
        self.setStyleSheet(self._qss())
        self._key = ''
        self._speakers = []
        self._downloading = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        self.info = QLabel('')
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.info)

        row = QHBoxLayout()
        row.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(4)
        left.addWidget(QLabel('模型（共 %d 个）' % len(vlib.MODELS)))
        self.lst_models = QListWidget()
        self.lst_models.setMinimumWidth(300)
        self.lst_models.currentItemChanged.connect(lambda *_: self._on_model())
        left.addWidget(self.lst_models, 1)
        row.addLayout(left, 1)

        right = QVBoxLayout()
        right.setSpacing(4)
        self.lbl_spk = QLabel('音色')
        right.addWidget(self.lbl_spk)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText('搜索编号，如 12 / 187（回车即筛）')
        self.ed_search.textChanged.connect(lambda *_: self._fill_speakers())
        right.addWidget(self.ed_search)
        self.lst_speakers = QListWidget()
        self.lst_speakers.setMinimumWidth(300)
        self.lst_speakers.itemDoubleClicked.connect(lambda *_: self._apply())
        right.addWidget(self.lst_speakers, 1)
        row.addLayout(right, 1)
        lay.addLayout(row, 1)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.btn_preview = QPushButton('🔊 试听')
        self.btn_preview.clicked.connect(self._preview)
        self.btn_apply = QPushButton('✔ 用这个音色朗读')
        self.btn_apply.clicked.connect(self._apply)
        self.btn_dl = QPushButton('⬇ 下载选中模型')
        self.btn_dl.clicked.connect(lambda: self._download(self._key))
        self.btn_dl_all = QPushButton('⬇ 下载全部缺失')
        self.btn_dl_all.clicked.connect(lambda: self._download(''))
        self.btn_refresh = QPushButton('🔄 刷新')
        self.btn_refresh.clicked.connect(self._reload_models)
        self.btn_close = QPushButton('关闭')
        self.btn_close.clicked.connect(self.reject)
        for b in (self.btn_preview, self.btn_apply, self.btn_dl, self.btn_dl_all,
                  self.btn_refresh):
            btns.addWidget(b)
        btns.addStretch(1)
        btns.addWidget(self.btn_close)
        lay.addLayout(btns)

        self.status = QLabel('')
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self._reload_models()

    # ---------- 外观 ----------
    def _qss(self):
        t = _tokens(self.host)
        return (
            'QDialog{background:%(dialog_bg)s;}'
            'QLabel{color:%(text)s;font-size:12px;}'
            'QLineEdit{background:%(input_bg)s;color:%(input_text)s;'
            'border:1px solid %(list_border)s;border-radius:6px;padding:5px 8px;font-size:12px;}'
            'QPushButton{background:%(ui_btn_bg)s;color:%(ui_text)s;border:none;border-radius:6px;'
            'padding:6px 10px;font-size:12px;}'
            'QPushButton:hover{background:%(ui_btn_hover)s;}'
            'QPushButton:disabled{color:%(ui_text_dim)s;}'
            'QListWidget{background:%(list_bg)s;color:%(text)s;border:1px solid %(list_border)s;'
            'border-radius:6px;font-size:12px;}'
            'QListWidget::item:selected{background:%(list_sel_bg)s;color:%(list_sel_text)s;}'
            % t
        )

    # ---------- 数据 ----------
    def _reload_models(self):
        keep = self._key
        self.lst_models.blockSignals(True)
        self.lst_models.clear()
        for m in vlib.MODELS:
            key = m['key']
            if vlib.is_installed(key):
                text = '✔ %s（%d 音色）' % (m['title'], vlib.speakers(key))
            else:
                text = '· %s（未下载 · %d MB）' % (m['title'], int(m.get('mb') or 0))
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, key)
            it.setToolTip(m.get('note') or '')
            self.lst_models.addItem(it)
            if key == keep:
                self.lst_models.setCurrentItem(it)
        self.lst_models.blockSignals(False)
        if self.lst_models.currentItem() is None and self.lst_models.count():
            self.lst_models.setCurrentRow(0)
        self._on_model()
        self.info.setText(
            '已装模型：%s ｜ 目录 %s ｜ 全部 8 个模型约 1.2 GB（2000+ 音色，免费开源）'
            % (vlib.summary(), vlib.ROOT))
        if not self._sherpa_ready():
            self.status.setText('⚠ 缺少推理引擎：%s' % self._sherpa_hint())

    def _sherpa_ready(self):
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except Exception:
            return False

    def _sherpa_hint(self):
        return '在项目目录执行  .venv\\Scripts\\python.exe -m pip install sherpa-onnx  （约 17 MB，装完即可用）'

    def _current_key(self):
        it = self.lst_models.currentItem()
        return (it.data(Qt.UserRole) if it else '') or ''

    def _on_model(self):
        self._key = self._current_key()
        m = vlib.BY_KEY.get(self._key) or {}
        installed = vlib.is_installed(self._key)
        self.lbl_spk.setText('音色（%d 个%s）' % (vlib.speakers(self._key),
                                                '，双击即应用' if installed else '，未下载'))
        self.btn_preview.setEnabled(installed)
        self.btn_apply.setEnabled(installed)
        self.btn_dl.setEnabled(bool(self._key) and not installed and not self._downloading)
        self._fill_speakers()
        if not installed and m:
            self.status.setText('这个模型还没下载（%s，约 %d MB）—— 点「⬇ 下载选中模型」'
                                % (m.get('title'), int(m.get('mb') or 0)))

    def _fill_speakers(self):
        self.lst_speakers.clear()
        self._speakers = []
        if not vlib.is_installed(self._key):
            return
        q = self.ed_search.text().strip()
        for spk in range(vlib.speakers(self._key)):
            if q and q not in ('#%d' % spk) and q != str(spk):
                continue
            it = QListWidgetItem('#%d' % spk)
            it.setData(Qt.UserRole, spk)
            self.lst_speakers.addItem(it)
            self._speakers.append(spk)
        self.lbl_spk.setText('音色（%d 个%s）' % (vlib.speakers(self._key),
                                                '，双击即应用' if self._speakers else ''))
        if self.lst_speakers.count():
            self.lst_speakers.setCurrentRow(0)

    # ---------- 动作 ----------
    def _current_speaker(self):
        it = self.lst_speakers.currentItem()
        return int(it.data(Qt.UserRole)) if it else 0

    def _preview(self):
        if not vlib.is_installed(self._key):
            self.status.setText('先下载这个模型再试听')
            return
        key, spk = self._key, self._current_speaker()
        if not self._sherpa_ready():
            self.status.setText('⚠ 缺少推理引擎：%s' % self._sherpa_hint())
            return
        self.status.setText('正在合成试听（%s #%d）…' % (vlib.label(key, spk), spk))
        speed = 1.0
        try:
            from voice_io import rate_to_multiplier
            speed = rate_to_multiplier(getattr(getattr(self.host, 'voice', None), 'rate', ''))
        except Exception:
            speed = 1.0

        def work():
            os.makedirs(PREVIEW_DIR, exist_ok=True)
            path = os.path.join(PREVIEW_DIR, 'preview_%s_%d.wav' % (key, spk))
            ok, err = vlib.synth(key, spk, SAMPLE_TEXT, path, speed=speed)
            if ok and _play(path):
                msg = '🔊 正在试听 %s #%d' % (vlib.BY_KEY[key]['title'], spk)
            else:
                msg = '试听失败：%s' % (err or '播放不出来')
            self.host._run_on_ui(lambda: self.status.setText(msg))
        threading.Thread(target=work, daemon=True).start()

    def _apply(self):
        if not vlib.is_installed(self._key):
            self.status.setText('先下载这个模型再用它朗读')
            return
        spk = self._current_speaker()
        spec = vlib.spec(self._key, spk)
        try:
            self.host._set_voice_name(spec)
            self.status.setText('✅ 已换成 %s —— 试试「🔊 试听」，不满意再挑一个'
                                % vlib.label(self._key, spk))
        except Exception as e:                                   # noqa: BLE001
            self.status.setText('应用失败：%s' % str(e)[:80])

    def _download(self, key):
        """用 tools/fetch_voices.py 在子进程里下（跟命令行完全同一条路径，行为一致）"""
        if self._downloading:
            return
        keys = [key] if key else [k for k in vlib.missing()]
        if not keys:
            self.status.setText('没有缺的模型，全部已装好')
            return
        base = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(base, 'tools', 'fetch_voices.py')
        if not os.path.isfile(script):
            self.status.setText('找不到下载器：%s' % script)
            return
        self._downloading = True
        for b in (self.btn_dl, self.btn_dl_all):
            b.setEnabled(False)
        total_mb = sum(int(vlib.BY_KEY[k].get('mb') or 0) for k in keys)
        self.status.setText('开始下载 %d 个模型（约 %d MB，可后台进行，别关桌宠）…'
                            % (len(keys), total_mb))
        cmd = [sys.executable, script, '--only', ','.join(keys)]
        env = dict(os.environ, PYTHONIOENCODING='utf-8')

        def work():
            err = ''
            try:
                flags = 0
                if os.name == 'nt':
                    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                r = subprocess.run(cmd, capture_output=True, timeout=7200, env=env,
                                   cwd=base, creationflags=flags)
                tail = (r.stdout or b'').decode('utf-8', 'ignore').strip().splitlines()
                msg = tail[-1] if tail else ''
                if r.returncode != 0:
                    err = msg or '退出码 %d' % r.returncode
                else:
                    msg = msg or '下载完成'
            except Exception as e:                               # noqa: BLE001
                err = str(e)[:120]

            def done():
                self._downloading = False
                self._reload_models()
                self.status.setText(('❌ 下载失败：%s（重试会续传）' % err) if err
                                    else ('✅ %s —— 现在可以在左边挑音色了' % (msg or '下载完成')))
            self.host._run_on_ui(done)
        threading.Thread(target=work, daemon=True).start()

    def closeEvent(self, ev):
        if self._downloading:
            self.host._notify('下载还在后台跑，关掉面板不影响（重开可看进度）')
        super(VoiceLibraryDialog, self).closeEvent(ev)


def open_library(host, parent=None):
    """设置页「🎚 本地声线库…」的入口；返回对话框（执行完 exec）"""
    dlg = VoiceLibraryDialog(host, parent)
    dlg.exec()
    return dlg
