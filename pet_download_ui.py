# -*- coding: utf-8 -*-
"""pet_download_ui.py — 「一键下载」面板（v6.85）

分工：
    pet_download.py   内核：找 yt-dlp/ffmpeg、解析链接、跑下载、解析进度（子进程）
    本模块              界面与调度：粘贴/监听剪贴板 → 解析 → 选画质 → 下载 → 进度/暂停/继续

设计要点（跟音乐面板一套规矩）：
- 颜色一律走 pet_theme 的 token（tests/test_theme_tokens.py 会扫硬编码颜色）
- 解析和下载都在后台线程，GUI 线程只更新控件
- 面板懒创建：点了才起线程、才找 yt-dlp，平时零开销
- 复制视频链接 → 桌宠自己把面板端出来并解析好，用户只需点「⬇ 下载」（这就是「一键」）
"""

import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout,
    QCheckBox, QWidget,
)

import pet_download as dl
import pet_window as pwin  # v6.90 每个窗口标题栏的「—」隐藏键


def _tokens(pet):
    """按当前主题取一组颜色（切主题后重新取一次即可跟着变）"""
    import pet_theme
    out = {}
    for key in ('dialog_bg', 'text', 'input_bg', 'input_text', 'list_bg', 'list_border',
                'list_sel_bg', 'list_sel_text', 'item_bg', 'ui_btn_bg', 'ui_btn_hover',
                'ui_accent', 'ui_text', 'ui_text_strong', 'ui_text_dim', 'ui_border_soft',
                'ui_btn_alt', 'accent'):
        try:
            out[key] = pet._tk(key)
        except Exception:
            try:
                out[key] = pet_theme.color(key)
            except Exception:
                out[key] = ''
    return out


class DownloadPanel(QDialog):
    """下载面板：贴链接 → 解析 → 选画质 → ⬇ 下载（进度/暂停/继续/取消）"""

    def __init__(self, controller, parent):
        super(DownloadPanel, self).__init__(parent)
        self.c = controller
        self.setWindowTitle('🎬 下载视频')
        # v6.90：去掉 Qt.Tool（工具窗系统不画最小化键），换成普通窗口 + 标题栏「—」
        pwin.add_hide_button(self)
        self.resize(620, 520)
        self.setStyleSheet(self._qss())
        self._info = {}
        self._last_file = ''

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # 链接行
        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText('粘贴视频链接（B站 / YouTube / 抖音 / 小红书 / 微博…）')
        self.paste_btn = QPushButton('📋 粘贴')
        self.probe_btn = QPushButton('🔍 解析')
        row.addWidget(self.url_edit, 1)
        row.addWidget(self.paste_btn)
        row.addWidget(self.probe_btn)
        lay.addLayout(row)

        # 解析出来的信息
        self.info = QLabel('把链接粘进来，先按「🔍 解析」看看能不能下')
        self.info.setWordWrap(True)
        self.info.setMinimumHeight(42)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.info)

        # 画质 + 保存位置
        qrow = QHBoxLayout()
        self.quality_box = QComboBox()
        for label, value in dl.QUALITY_CHOICES:
            self.quality_box.addItem(label, value)
        self.quality_box.setMinimumWidth(170)
        qrow.addWidget(QLabel('画质'))
        qrow.addWidget(self.quality_box)
        qrow.addSpacing(8)
        qrow.addWidget(QLabel('存到'))
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText('默认 downloads/')
        qrow.addWidget(self.dir_edit, 1)
        self.dir_btn = QPushButton('📂')
        self.dir_btn.setFixedWidth(38)
        qrow.addWidget(self.dir_btn)
        lay.addLayout(qrow)

        # cookie + 剪贴板
        crow = QHBoxLayout()
        self.cookie_box = QComboBox()
        for value, label in (('', '不用 cookie'),) + tuple(dl.installed_browsers()):
            self.cookie_box.addItem(('浏览器 cookie：' + label) if value else label, value)
        self.cookie_box.setToolTip('有些站要登录态才能下（B站 1080P 高码率、私密视频等）；\n'
                                  'Chrome/Edge 开着时读不到 cookie 库，关掉浏览器再试')
        self.cookie_box.setMinimumWidth(170)
        crow.addWidget(self.cookie_box)
        self.watch_chk = QCheckBox('复制视频链接就自动填进来')
        self.watch_chk.setToolTip('开着的话，复制 B站/YouTube/抖音… 的链接时面板会自动把链接解析好')
        crow.addWidget(self.watch_chk)
        crow.addStretch(1)
        lay.addLayout(crow)

        # 按钮行
        brow = QHBoxLayout()
        self.dl_btn = QPushButton('⬇ 下载')
        self.pause_btn = QPushButton('⏸ 暂停')
        self.resume_btn = QPushButton('▶ 继续')
        self.cancel_btn = QPushButton('✕ 取消')
        self.open_btn = QPushButton('📂 打开文件夹')
        for b in (self.dl_btn, self.pause_btn, self.resume_btn, self.cancel_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        brow.addWidget(self.open_btn)
        lay.addLayout(brow)

        # 进度
        pbar_row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        pbar_row.addWidget(self.bar, 1)
        lay.addLayout(pbar_row)
        self.status = self._dim(QLabel(''))
        lay.addWidget(self.status)

        # 任务列表
        lay.addWidget(self._dim(QLabel('下载记录（双击打开文件）')))
        self.tasks = QListWidget()
        lay.addWidget(self.tasks, 1)

        self.hint = self._dim(QLabel(''))
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        # 信号
        self.paste_btn.clicked.connect(self.on_paste)
        self.probe_btn.clicked.connect(self.on_probe)
        self.url_edit.returnPressed.connect(self.on_probe)
        self.dl_btn.clicked.connect(self.on_download)
        self.pause_btn.clicked.connect(self.c.pause)
        self.resume_btn.clicked.connect(self.c.resume)
        self.cancel_btn.clicked.connect(self.c.cancel)
        self.open_btn.clicked.connect(self.on_open_folder)
        self.dir_btn.clicked.connect(self.on_pick_dir)
        self.tasks.itemDoubleClicked.connect(self.on_task_open)
        self.quality_box.currentIndexChanged.connect(self._remember_quality)
        self.cookie_box.currentIndexChanged.connect(self._remember_browser)
        self.watch_chk.toggled.connect(self.c.set_watch_clipboard)

        self._load_settings()
        ok, hint = dl.status()
        self.hint.setText(hint if not ok else
                          '支持 B站 / YouTube / 抖音 / 小红书 / 快手 / 微博 / X …（yt-dlp 的站点列表）\n'
                          '下不了的：爱奇艺/腾讯视频 VIP 加密、Netflix 这类 DRM 内容、需要验证码的页面')
        self.dl_btn.setEnabled(False)
        self._refresh_buttons()

    # ---------- 样式 ----------
    def _dim(self, w):
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
            'QCheckBox{color:%(ui_text)s;font-size:12px;}'
            'QLineEdit,QComboBox{background:%(input_bg)s;color:%(input_text)s;'
            'border:1px solid %(list_border)s;border-radius:6px;padding:5px 8px;font-size:12px;}'
            'QPushButton{background:%(ui_btn_bg)s;color:%(ui_text)s;border:none;border-radius:6px;'
            'padding:6px 10px;font-size:12px;}'
            'QPushButton:hover{background:%(ui_btn_hover)s;}'
            'QPushButton:disabled{color:%(ui_text_dim)s;}'
            'QListWidget{background:%(list_bg)s;color:%(text)s;border:1px solid %(list_border)s;'
            'border-radius:6px;font-size:12px;}'
            'QListWidget::item:selected{background:%(list_sel_bg)s;color:%(list_sel_text)s;}'
            'QProgressBar{background:%(list_bg)s;border:1px solid %(list_border)s;border-radius:6px;'
            'height:16px;text-align:center;color:%(ui_text)s;font-size:11px;}'
            'QProgressBar::chunk{background:%(ui_accent)s;border-radius:5px;}'
            % t
        )

    # ---------- 设置 ----------
    def _load_settings(self):
        cfg = dl.cfg_read()
        self.dir_edit.setText(str(cfg.get('dl_dir') or dl.default_outdir()))
        want = str(cfg.get('dl_quality') or 'best')
        idx = self.quality_box.findData(want)
        if idx >= 0:
            self.quality_box.setCurrentIndex(idx)
        browser = str(cfg.get('dl_browser') or '')
        bidx = self.cookie_box.findData(browser)
        if bidx >= 0:
            self.cookie_box.setCurrentIndex(bidx)
        self.watch_chk.blockSignals(True)
        self.watch_chk.setChecked(bool(cfg.get('dl_watch', True)))
        self.watch_chk.blockSignals(False)

    def _remember_quality(self, *_):
        dl.cfg_write('dl_quality', self.quality_box.currentData() or 'best')

    def _remember_browser(self, *_):
        dl.cfg_write('dl_browser', self.cookie_box.currentData() or '')

    def outdir(self):
        text = self.dir_edit.text().strip()
        return text or dl.default_outdir()

    # ---------- 交互 ----------
    def on_paste(self):
        try:
            text = QApplication.clipboard().text() or ''
        except Exception:
            text = ''
        url = dl.looks_like_url(text)
        if not url:
            self.set_status('剪贴板里没有链接')
            return
        self.url_edit.setText(url)
        self.on_probe()

    def on_pick_dir(self):
        start = self.outdir()
        path = QFileDialog.getExistingDirectory(self, '选择保存文件夹', start)
        if path:
            self.dir_edit.setText(path)
            dl.cfg_write('dl_dir', path)

    def on_probe(self):
        url = self.url_edit.text().strip()
        if not url:
            self.set_status('先贴个链接')
            return
        self.set_status('正在解析（第一次可能会慢几秒）…')
        self.probe_btn.setEnabled(False)
        self.c.probe(url, self.cookie_box.currentData() or '')

    def fill_url(self, url):
        """外面（剪贴板监听）捡到链接时调用：填进来并顺手解析"""
        if not url:
            return
        self.url_edit.setText(url)
        self.show()
        self.raise_()
        self.on_probe()

    def on_probed(self, info):
        self.probe_btn.setEnabled(True)
        info = info or {}
        if not info.get('ok'):
            self._info = {}
            self.info.setText('❌ %s' % (info.get('error') or '解析失败'))
            self.dl_btn.setEnabled(False)
            self.set_status('解析失败')
            return
        self._info = info
        parts = ['<b>%s</b>' % (info.get('title') or '（无标题）')]
        meta = []
        if info.get('site'):
            meta.append(str(info['site']))
        if info.get('uploader'):
            meta.append(str(info['uploader']))
        dur = dl.fmt_duration(info.get('duration'))
        if dur:
            meta.append(dur)
        if info.get('is_live'):
            meta.append('直播流（可能下不全）')
        if meta:
            parts.append('<span>%s</span>' % ' · '.join(meta))
        heights = info.get('heights') or []
        if heights:
            parts.append('可选清晰度：%s（共 %d 个格式）'
                         % (' / '.join('%dP' % h for h in heights), info.get('format_count') or 0))
        self.info.setText('<br>'.join(parts))
        self.dl_btn.setEnabled(True)
        self.set_status('解析好了，点「⬇ 下载」开下')
        if info.get('notice'):
            self.set_status(info['notice'])

    def on_download(self):
        url = self.url_edit.text().strip()
        if not url:
            self.set_status('先贴个链接')
            return
        quality = self.quality_box.currentData() or 'best'
        browser = self.cookie_box.currentData() or ''
        outdir = self.outdir()
        title = (self._info or {}).get('title') or url
        job = self.c.new_job(url, outdir=outdir, quality=quality, browser=browser, title=title)
        self.set_status('已开始下载：%s' % title)
        self.bar.setValue(0)
        self._refresh_buttons()

    def on_open_folder(self):
        target = self._last_file or self.outdir()
        dl.open_folder(target)

    def on_task_open(self, item):
        path = item.data(Qt.UserRole) if item is not None else ''
        if path:
            dl.open_folder(path)

    # ---------- 任务事件 ----------
    def on_job_event(self, job, kind, data):
        data = data or {}
        if kind == 'progress':
            self.bar.setValue(int(data.get('percent') or 0))
            bits = ['%d%%' % int(data.get('percent') or 0)]
            if data.get('total'):
                bits.append('%s / %s' % (dl.fmt_size(data.get('downloaded')), dl.fmt_size(data.get('total'))))
            spd = dl.fmt_speed(data.get('speed'))
            if spd:
                bits.append(spd)
            eta = dl.fmt_eta(data.get('eta'))
            if eta:
                bits.append('剩 ' + eta)
            self.set_status('⬇ ' + ' · '.join(bits))
            self._refresh_buttons()
        elif kind == 'state':
            state = data.get('state')
            if state == 'merging':
                self.bar.setValue(max(self.bar.value(), 99))
                self.set_status('🔧 音视频合并中（ffmpeg）…')
            elif state == 'paused':
                self.set_status('⏸ 已暂停（再点「▶ 继续」会接着下，不用从头来）')
            elif state == 'canceled':
                self.set_status('✕ 已取消')
            self._refresh_buttons()
        elif kind == 'done':
            path = data.get('file') or ''
            self._last_file = path
            self.bar.setValue(100)
            self.set_status('✅ 下好了：%s' % os.path.basename(path or ''))
            self.c.notice.emit('🎬 视频下好了：%s' % os.path.basename(path or ''))
            self._refresh_buttons()
        elif kind == 'error':
            self.set_status('❌ %s' % (data.get('error') or '下载失败'))
            self.c.notice.emit('🎬 下载失败：%s' % (data.get('error') or ''))
            self._refresh_buttons()
        elif kind == 'notice':
            self.set_status(data.get('text') or '')
        if kind != 'progress':
            self.refresh_tasks()

    def refresh_tasks(self):
        self.tasks.clear()
        for job in self.c.jobs():
            item = QListWidgetItem(job.summary())
            item.setData(Qt.UserRole, job.file or '')
            self.tasks.addItem(item)

    def _refresh_buttons(self):
        job = self.c.current_job()
        active = bool(job and job.active())
        state = job.state if job else ''
        self.pause_btn.setEnabled(state in ('running', 'merging'))
        self.resume_btn.setEnabled(state == 'paused')
        self.cancel_btn.setEnabled(active)

    def set_status(self, text):
        self.status.setText(text or '')


class DownloadController(QObject):
    """面板 + 任务的生命周期管理（桌宠只认这个）"""

    notice = Signal(str)                  # 给桌宠冒一句（通知 + 气泡）
    probed = Signal(object)               # 解析结果
    job_event = Signal(object, str, object)   # job, kind, data
    clipboard_hit = Signal(str)           # 剪贴板里捡到视频链接

    def __init__(self, pet):
        super(DownloadController, self).__init__(pet)
        self.pet = pet
        self._panel = None
        self._jobs = []
        self._last_clip = ''
        self._clip_on = False
        self._wired_clip = False

    # ---------- 面板 ----------
    def panel(self):
        if self._panel is None:
            ok, hint = dl.status()
            if not ok:
                self.notice.emit('🎬 ' + hint)
            self._panel = DownloadPanel(self, self.pet)
            self.job_event.connect(self._panel.on_job_event)
            self.probed.connect(self._panel.on_probed)
            self.start_watching_if_needed()
            self._panel.show()
        pwin.present(self._panel)       # v6.90：隐藏着的面板要能一键叫回来
        return self._panel

    def _ui(self):
        return self._panel if self._panel is not None else None

    # ---------- 剪贴板监听 ----------
    def set_watch_clipboard(self, on):
        """开/关剪贴板监听：复制视频链接 → 面板自动端出来并解析好"""
        on = bool(on)
        if bool(dl.cfg_read().get('dl_watch', True)) != on:
            dl.cfg_write('dl_watch', on)
        self._clip_on = on
        if on and not self._wired_clip:
            try:
                QApplication.clipboard().dataChanged.connect(self._on_clip_changed)
                self._wired_clip = True
            except Exception:
                pass
        if not on and self._wired_clip:
            try:
                QApplication.clipboard().dataChanged.disconnect(self._on_clip_changed)
                self._wired_clip = False
            except Exception:
                pass

    def start_watching_if_needed(self):
        """开面板时按配置把监听接上（默认开）"""
        if self._wired_clip:
            return
        self._clip_on = bool(dl.cfg_read().get('dl_watch', True))
        if self._clip_on:
            self.set_watch_clipboard(True)

    def _on_clip_changed(self):
        if not self._clip_on:
            return
        try:
            text = QApplication.clipboard().text() or ''
        except Exception:
            return
        url = dl.looks_like_video_url(text)
        if not url or url == self._last_clip:
            return
        self._last_clip = url
        self.clipboard_hit.emit(url)
        p = self._ui()
        if p is not None:
            p.fill_url(url)
        else:
            self.notice.emit('🎬 复制到视频链接了：按标题栏的 ⬇ 就能下')

    # ---------- 解析 ----------
    def probe(self, url, browser=''):
        def work():
            info = dl.probe(url, browser=browser, timeout=90)
            self.probed.emit(info or {'ok': False, 'error': '解析失败'})

        threading.Thread(target=work, name='pet-dl-probe', daemon=True).start()

    # ---------- 任务 ----------
    def jobs(self):
        return list(self._jobs)

    def current_job(self):
        for job in reversed(self._jobs):
            if job.active():
                return job
        return self._jobs[-1] if self._jobs else None

    def new_job(self, url, outdir=None, quality='best', browser='', cookies_file='', title=''):
        def on_event(job, kind, data):
            self.job_event.emit(job, kind, data)

        job = dl.DownloadJob(url, outdir=outdir or dl.default_outdir(), quality=quality,
                             browser=browser, cookies_file=cookies_file, title=title,
                             on_event=on_event)
        self._jobs.append(job)
        job.start()
        return job

    def pause(self):
        job = self.current_job()
        if job:
            job.pause()

    def resume(self):
        job = self.current_job()
        if job:
            job.resume()

    def cancel(self):
        job = self.current_job()
        if job:
            job.cancel()

    # ---------- 收尾 ----------
    def shutdown(self):
        for job in self._jobs:
            if job.active():
                try:
                    job.cancel()
                except Exception:
                    pass
