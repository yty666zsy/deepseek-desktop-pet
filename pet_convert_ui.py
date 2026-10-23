# -*- coding: utf-8 -*-
"""pet_convert_ui.py — 「文件转换」面板（v6.86）

分工：
    pet_convert.py   内核：找 Filee、算可用目标格式、跑转换（子进程 + 后台线程）
    本模块             界面与调度：拖文件进来 → 列出可用格式 → 选一个 → 转 → 结果/进度

设计要点（跟下载/音乐面板一套规矩）：
- 颜色一律走 pet_theme 的 token（tests/test_theme_tokens.py 会扫硬编码颜色）
- **整块面板都接拖放**：把文件丢进窗口任意位置就行，不用对准某个小方块
- 目标格式按「这批文件共同能转成什么」动态算，混了不同类型就只留交得上的那些
- 输出默认跟原文件放一起（Filee 的默认行为），想统一放别处才勾「统一输出到」
- 面板懒创建：点了才找 Filee、才起线程，平时零开销
"""

import os
import threading

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSlider, QVBoxLayout,
)

import pet_convert as cv
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


class ConvertPanel(QDialog):
    """转换面板：拖文件 → 选目标格式 → ⇄ 转换（批量、进度、结果）"""

    def __init__(self, controller, parent):
        super(ConvertPanel, self).__init__(parent)
        self.c = controller
        self.setWindowTitle('⇄ 文件转换')
        # v6.90：去掉 Qt.Tool（工具窗系统不画最小化键），换成普通窗口 + 标题栏「—」
        pwin.add_hide_button(self)
        self.resize(660, 560)
        self.setAcceptDrops(True)          # 整块面板都吃拖放
        self.setStyleSheet(self._qss())
        self._files = []                   # 绝对路径（去重、保序）
        self._last_out = ''
        self._targets = []
        self._asking = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        # 引擎状态
        self.engine_lbl = self._dim(QLabel(''))
        self.engine_lbl.setWordWrap(True)
        lay.addWidget(self.engine_lbl)

        # 拖放提示条
        self.drop_hint = QLabel('把文件或整个文件夹拖进这个窗口，或点右边「📂 添加文件 / 📁 添加文件夹」—— '
                                '图片 / 音视频 / PDF / Office / 表格 / 压缩包都行')
        self.drop_hint.setAlignment(Qt.AlignCenter)
        self.drop_hint.setWordWrap(True)
        self.drop_hint.setMinimumHeight(58)
        self.drop_hint.setStyleSheet(
            'QLabel{color:%s;background:%s;border:1px dashed %s;border-radius:8px;'
            'padding:10px;font-size:12px;}'
            % (_tokens(self.c.pet)['ui_text_dim'], _tokens(self.c.pet)['list_bg'],
               _tokens(self.c.pet)['list_border']))
        lay.addWidget(self.drop_hint)

        # 文件列表 + 右侧按钮
        mid = QHBoxLayout()
        col = QVBoxLayout()
        self.files = QListWidget()
        self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.setToolTip('Delete 键可以移除选中的文件')
        col.addWidget(self.files, 1)
        mid.addLayout(col, 1)
        side = QVBoxLayout()
        side.setSpacing(6)
        self.add_btn = QPushButton('📂 添加文件')
        self.adddir_btn = QPushButton('📁 添加文件夹')
        self.del_btn = QPushButton('🗑 移除选中')
        self.clear_btn = QPushButton('🧹 清空')
        for b in (self.add_btn, self.adddir_btn, self.del_btn, self.clear_btn):
            b.setMinimumWidth(110)
            side.addWidget(b)
        side.addStretch(1)
        mid.addLayout(side, 0)
        lay.addLayout(mid, 1)

        # 目标格式
        trow = QHBoxLayout()
        trow.addWidget(QLabel('转成'))
        self.target_box = QComboBox()
        self.target_box.setMinimumWidth(240)
        trow.addWidget(self.target_box, 1)
        self.overwrite_chk = QCheckBox('同名覆盖（不勾就自动加序号）')
        trow.addWidget(self.overwrite_chk)
        lay.addLayout(trow)

        # 质量 + 保留时间戳（v6.87：Filee 的 --quality / --keep-dates）
        qrow = QHBoxLayout()
        qrow.addWidget(QLabel('质量'))
        self.quality_slider = QSlider(Qt.Horizontal)
        self.quality_slider.setRange(1, 100)
        self.quality_slider.setValue(90)
        self.quality_slider.setFixedWidth(150)
        self.quality_slider.setToolTip('只影响有损图片（JPG / WebP / AVIF）—— 数值越低文件越小、越糊')
        qrow.addWidget(self.quality_slider)
        self.quality_lbl = self._dim(QLabel('90'))
        self.quality_lbl.setMinimumWidth(28)
        qrow.addWidget(self.quality_lbl)
        self.keepdates_chk = QCheckBox('保留原时间戳')
        self.keepdates_chk.setToolTip('产物沿用原文件的修改时间，不再变成「刚刚」')
        qrow.addWidget(self.keepdates_chk)
        qrow.addStretch(1)
        lay.addLayout(qrow)

        # PDF 页码范围 + 文件夹递归（v6.87）
        prow = QHBoxLayout()
        prow.addWidget(QLabel('页码范围'))
        self.pages_edit = QLineEdit()
        self.pages_edit.setPlaceholderText('留空 = 全部')
        self.pages_edit.setMaximumWidth(150)
        self.pages_edit.setToolTip('只对 PDF 生效，比如 1-3,5,8-（到末页）。\n'
                                   '配合「PDF 取页 / 拆分 / 压缩 / 合并 / 转文本」使用')
        prow.addWidget(self.pages_edit)
        self.pages_hint = self._dim(QLabel('如 1-3,5 —— 只对 PDF 生效'))
        prow.addWidget(self.pages_hint, 1)
        self.recurse_chk = QCheckBox('包含子文件夹')
        self.recurse_chk.setToolTip('拖进来的是文件夹时，连里面的子文件夹一起收（对应 Filee 的 -r）')
        prow.addWidget(self.recurse_chk)
        lay.addLayout(prow)

        # 输出位置
        orow = QHBoxLayout()
        self.unify_chk = QCheckBox('统一输出到')
        orow.addWidget(self.unify_chk)
        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText('不勾就跟着原文件放一起（Filee 的默认行为）')
        self.dir_edit.setEnabled(False)
        orow.addWidget(self.dir_edit, 1)
        self.dir_btn = QPushButton('📂')
        self.dir_btn.setFixedWidth(38)
        self.dir_btn.setEnabled(False)
        orow.addWidget(self.dir_btn)
        lay.addLayout(orow)

        # 按钮行
        brow = QHBoxLayout()
        self.go_btn = QPushButton('⇄ 开始转换')
        self.cancel_btn = QPushButton('✕ 取消')
        self.open_btn = QPushButton('📂 打开输出位置')
        brow.addWidget(self.go_btn)
        brow.addWidget(self.cancel_btn)
        brow.addStretch(1)
        brow.addWidget(self.open_btn)
        lay.addLayout(brow)

        # 进度
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        lay.addWidget(self.bar)
        self.status = self._dim(QLabel(''))
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        # 结果
        lay.addWidget(self._dim(QLabel('转换结果（双击用默认程序打开）')))
        self.results = QListWidget()
        lay.addWidget(self.results, 1)

        # 信号
        self.add_btn.clicked.connect(self.on_add)
        self.adddir_btn.clicked.connect(self.on_add_dir)
        self.del_btn.clicked.connect(self.on_remove)
        self.clear_btn.clicked.connect(self.on_clear)
        self.go_btn.clicked.connect(self.on_convert)
        self.cancel_btn.clicked.connect(self.c.on_cancel)
        self.open_btn.clicked.connect(self.on_open)
        self.dir_btn.clicked.connect(self.on_pick_dir)
        self.unify_chk.toggled.connect(self._on_unify_toggled)
        self.target_box.currentIndexChanged.connect(lambda *_: self._refresh_buttons())
        self.results.itemDoubleClicked.connect(self.on_result_open)
        self.quality_slider.valueChanged.connect(self._on_quality)
        self.keepdates_chk.toggled.connect(lambda *_: self._remember())
        self.recurse_chk.toggled.connect(lambda *_: self._remember())
        self.pages_edit.editingFinished.connect(self._remember)
        self.files.installEventFilter(self)

        self._load_settings()
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
            'QSlider::groove:horizontal{height:4px;background:%(list_bg)s;'
            'border:1px solid %(list_border)s;border-radius:2px;}'
            'QSlider::sub-page:horizontal{background:%(ui_accent)s;border-radius:2px;}'
            'QSlider::handle:horizontal{background:%(ui_accent)s;width:12px;margin:-5px 0;'
            'border-radius:6px;}'
            % t
        )

    # ---------- 设置 ----------
    def _load_settings(self):
        cfg = cv.cfg_read()
        unify = bool(cfg.get('cv_unify', False))
        self.unify_chk.blockSignals(True)
        self.overwrite_chk.setChecked(bool(cfg.get('cv_overwrite', False)))
        self.unify_chk.setChecked(unify)
        self.unify_chk.blockSignals(False)
        self.dir_edit.setText(str(cfg.get('cv_dir') or ''))
        self.dir_edit.setEnabled(unify)
        self.dir_btn.setEnabled(unify)
        # v6.87：质量 / 保留时间戳 / 页码范围 / 递归
        try:
            q = int(cfg.get('cv_quality', 90) or 90)
        except Exception:
            q = 90
        q = min(100, max(1, q))
        self.quality_slider.blockSignals(True)
        self.quality_slider.setValue(q)
        self.quality_slider.blockSignals(False)
        self.quality_lbl.setText(str(q))
        self.keepdates_chk.setChecked(bool(cfg.get('cv_keepdates') or False))
        rec = cfg.get('cv_recurse')          # 没设过就是默认「收子文件夹」
        self.recurse_chk.setChecked(True if rec is None else bool(rec))
        self.pages_edit.setText(str(cfg.get('cv_pages') or ''))
        self.refresh_engine()

    def _remember(self):
        cv.cfg_write('cv_overwrite', bool(self.overwrite_chk.isChecked()))
        cv.cfg_write('cv_unify', bool(self.unify_chk.isChecked()))
        cv.cfg_write('cv_quality', int(self.quality_slider.value()))
        cv.cfg_write('cv_keepdates', bool(self.keepdates_chk.isChecked()))
        cv.cfg_write('cv_recurse', bool(self.recurse_chk.isChecked()))
        cv.cfg_write('cv_pages', self.pages_edit.text().strip())
        if self.dir_edit.text().strip():
            cv.cfg_write('cv_dir', self.dir_edit.text().strip())

    def _on_quality(self, value):
        self.quality_lbl.setText(str(int(value)))
        self._remember()

    def _on_unify_toggled(self, on):
        self.dir_edit.setEnabled(bool(on))
        self.dir_btn.setEnabled(bool(on))
        self._remember()

    # ---------- 拖放 ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super(ConvertPanel, self).dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super(ConvertPanel, self).dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            paths = []
            for u in e.mimeData().urls():
                p = u.toLocalFile() if u.isLocalFile() else u.toString()
                if p and (os.path.isfile(p) or os.path.isdir(p)):
                    paths.append(p)
            if paths:
                self.add_files(paths)
            e.accept()
        else:
            super(ConvertPanel, self).dropEvent(e)

    def eventFilter(self, obj, event):
        """文件列表里按 Delete 移除"""
        try:
            if obj is self.files and event.type() == QEvent.KeyPress:
                if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                    self.on_remove()
                    return True
        except Exception:
            pass
        return super(ConvertPanel, self).eventFilter(obj, event)

    # ---------- 文件列表 ----------
    def add_files(self, paths):
        if isinstance(paths, str):       # 防呆：传进来一个路径而不是列表时，别按字符拆
            paths = [paths]
        # 文件夹摊平（v6.87：对应 Filee 的 -r，勾了「包含子文件夹」就递归）
        files, notes = cv.expand_paths(paths, self.recurse_chk.isChecked())
        added = 0
        for p in files:
            p = os.path.abspath(str(p))
            if not os.path.isfile(p) or p in self._files:
                continue
            self._files.append(p)
            added += 1
        if not added:
            if notes:
                self.set_status('；'.join(notes))
                if self.isHidden():
                    self.show()
            return
        self.refresh_files()
        self.refresh_targets()
        if self.isHidden():
            self.show()
        self.raise_()
        msg = '加进来 %d 个文件，选个目标格式就能转' % len(self._files)
        if notes:
            msg += '（%s）' % '；'.join(notes)
        self.set_status(msg)

    def refresh_files(self):
        self.files.clear()
        for p in self._files:
            try:
                size = cv.fmt_size(os.path.getsize(p))
            except Exception:
                size = '—'
            kind = {'image': '图片', 'audio': '音频', 'video': '视频', 'pdf': 'PDF',
                    'office': 'Office', 'table': '表格', 'archive': '压缩包',
                    'text': '文本'}.get(cv.kind_of(p), '未知')
            item = QListWidgetItem('%s   ·   %s · %s' % (os.path.basename(p), kind, size))
            item.setData(Qt.UserRole, p)
            item.setToolTip(p)
            self.files.addItem(item)
        self._refresh_pages_state()
        self._refresh_buttons()

    def _refresh_pages_state(self):
        """页码范围只有 PDF 才用得上 —— 没 PDF 就灰着，别让人白填"""
        has_pdf = any(cv.ext_of(p) == 'pdf' for p in self._files)
        self.pages_edit.setEnabled(has_pdf)
        self.pages_hint.setText('如 1-3,5 —— 留空 = 全部页' if has_pdf else '只有 PDF 才用得上页码')

    def files_paths(self):
        return list(self._files)

    def on_add(self):
        start = os.path.dirname(self._files[-1]) if self._files else (self.outdir() or '')
        paths, _flt = QFileDialog.getOpenFileNames(
            self, '选择要转换的文件', start,
            '所有文件 (*.*);;图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif *.ico *.heic *.avif);;'
            '音视频 (*.mp4 *.mkv *.mov *.avi *.webm *.mp3 *.wav *.flac *.m4a *.aac *.ogg);;'
            'PDF (*.pdf);;Office (*.docx *.xlsx *.pptx);;压缩包 (*.zip *.7z *.tar *.gz)')
        if paths:
            self.add_files(paths)

    def on_add_dir(self):
        """加一整个文件夹（勾了「包含子文件夹」就连子目录一起收）"""
        start = (os.path.dirname(self._files[-1]) if self._files else '') or self.outdir() or ''
        path = QFileDialog.getExistingDirectory(self, '选择文件夹（里面的文件都会加进来）', start)
        if path:
            self.add_files([path])

    def on_remove(self):
        for item in self.files.selectedItems():
            path = item.data(Qt.UserRole)
            if path in self._files:
                self._files.remove(path)
        self.refresh_files()
        self.refresh_targets()

    def on_clear(self):
        self._files = []
        self.refresh_files()
        self.refresh_targets()
        self.results.clear()
        self.bar.setValue(0)
        self.set_status('')

    # ---------- 目标格式 ----------
    def refresh_targets(self, async_filee=True):
        """算「这批文件共同能转成什么」并填下拉框；Filee 的清单需要起进程，放后台查"""
        paths = self._files
        keep = self.target_box.currentData()
        self.target_box.clear()
        if not paths:
            self._targets = []
            self.drop_hint.setVisible(True)
            self._refresh_buttons()
            return
        direct = cv.common_targets(paths, include_filee=False)
        known = cv.common_targets(paths, include_filee=True, fetch_filee=False)
        self._fill_targets(known or direct)
        self.drop_hint.setVisible(False)
        # Filee 的格式清单要起一次进程问它（结果会缓存），所以第一次先给内置的，查完再刷一遍
        if async_filee and cv.find_filee():
            self._ask_targets_async(list(paths), keep)
        if keep:
            idx = self.target_box.findData(keep)
            if idx >= 0:
                self.target_box.setCurrentIndex(idx)
        self._refresh_buttons()

    def _fill_targets(self, targets):
        self._targets = list(targets or [])
        self.target_box.clear()              # 后台线程查完会再填一次，不清就重复了
        for t in self._targets:
            self.target_box.addItem(cv.target_label(t), t)
        if not self._targets:
            self.target_box.addItem('（这批文件没有共同的可转格式）', '')

    def _ask_targets_async(self, paths, keep=''):
        if self._asking:
            return
        self._asking = True
        exts = sorted({cv.ext_of(p) for p in paths})

        def work():
            for ext in exts:
                try:
                    cv.filee_targets(ext)
                except Exception:
                    pass
            self.c.targets_ready.emit(keep)

        threading.Thread(target=work, name='pet-cv-targets', daemon=True).start()

    def on_targets_ready(self, keep=''):
        self._asking = False
        if not self._files:
            return
        known = cv.common_targets(self._files, include_filee=True, fetch_filee=False)
        if known:
            self._fill_targets(known)
            if keep:
                idx = self.target_box.findData(keep)
                if idx >= 0:
                    self.target_box.setCurrentIndex(idx)
        self._refresh_buttons()

    # ---------- 转换 ----------
    def outdir(self):
        if self.unify_chk.isChecked():
            return self.dir_edit.text().strip()
        return ''

    def on_pick_dir(self):
        start = self.dir_edit.text().strip() or (os.path.dirname(self._files[0]) if self._files else '')
        path = QFileDialog.getExistingDirectory(self, '选择输出文件夹', start)
        if path:
            self.dir_edit.setText(path)
            self._remember()

    def on_convert(self):
        if not self._files:
            self.set_status('先把文件拖进来')
            return
        target = self.target_box.currentData() or ''
        if not target:
            self.set_status('这批文件没有共同的可转格式（不同类型混在一起了，分开转）')
            return
        if self.unify_chk.isChecked() and not self.dir_edit.text().strip():
            self.set_status('勾了「统一输出到」，但目录还空着 —— 点 📂 选一个')
            return
        pages = self.pages_edit.text().strip()
        if target == cv.PAGES_PDF and not pages:
            self.set_status('「PDF 取页」得先填个页码范围，比如 1-3,5')
            return
        self._remember()
        if target == cv.MERGE_PDF and len(self._files) < 2:
            self.set_status('合并 PDF 至少要两个文件')
            return
        self.bar.setValue(0)
        self.results.clear()
        self.c.new_job(self._files, target, outdir=self.outdir(),
                       overwrite=self.overwrite_chk.isChecked(),
                       quality=int(self.quality_slider.value()),
                       keep_dates=self.keepdates_chk.isChecked(), pages=pages)
        self._refresh_buttons()

    def _refresh_buttons(self):
        job = self.c.current_job()
        active = bool(job and job.active())
        has = bool(self._files) and bool(self.target_box.currentData())
        self.go_btn.setEnabled(has and not active)
        self.cancel_btn.setEnabled(active)
        self.add_btn.setEnabled(not active)
        self.clear_btn.setEnabled(not active)

    # ---------- 引擎 ----------
    def showEvent(self, event):
        """每次弹出来都刷一次引擎状态（用户可能刚装/刚解压 Filee）"""
        try:
            self.refresh_engine()
        except Exception:
            pass
        super(ConvertPanel, self).showEvent(event)

    def refresh_engine(self):
        try:
            ok, hint = cv.status()
            self.engine_lbl.setText(hint if ok else (hint + ' —— 什么都没有，先装 Pillow / ffmpeg 或 Filee'))
        except Exception:
            self.engine_lbl.setText('')

    # ---------- 任务事件 ----------
    def on_job_event(self, job, kind, data):
        data = data or {}
        if kind == 'progress':
            self.bar.setValue(int(data.get('percent') or 0))
        elif kind == 'state':
            state = data.get('state')
            if state == 'running':
                self.set_status('⇄ 转换中…')
            elif state == 'canceled':
                self.set_status('✕ 已取消（已经转好的文件不会删）')
            self._refresh_buttons()
        elif kind == 'file_done':
            src = data.get('source') or ''
            if data.get('ok') and data.get('skipped'):
                item = QListWidgetItem('✔ %s（已经是 .%s，没重编码）'
                                       % (os.path.basename(src), cv.ext_of(src)))
                item.setData(Qt.UserRole, src)
            elif data.get('ok'):
                out = data.get('out') or ''
                outs = data.get('outs') or []
                self._last_out = out or self._last_out
                if len(outs) > 1:                       # 拆分：一进多出
                    text = '✔ %s → %d 个 PDF（每页一个）' % (os.path.basename(src), len(outs))
                else:
                    text = '✔ %s → %s' % (os.path.basename(src), os.path.basename(out))
                if data.get('note'):
                    text += '（%s）' % data['note']
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, out)
                tip = '\n'.join(outs if outs else [out])
                item.setToolTip('%s\n引擎：%s%s' % (tip, data.get('engine') or '',
                                                   ('\n' + data['note']) if data.get('note') else ''))
            else:
                item = QListWidgetItem('✕ %s：%s' % (os.path.basename(src),
                                                     (data.get('error') or '转换失败')[:70]))
                item.setData(Qt.UserRole, '')
                item.setToolTip(data.get('error') or '')
            self.results.addItem(item)
            self.results.scrollToBottom()
            self.refresh_jobs()
        elif kind == 'done':
            conv, failed = data.get('converted') or 0, data.get('failed') or 0
            skipped = data.get('skipped') or 0
            outputs = data.get('outputs') or []
            if outputs:
                self._last_out = outputs[-1]
            self.bar.setValue(100)
            text = '✅ 转好了 %d 个' % conv if conv else '✕ 一个都没转成'
            if skipped:
                text += '（其中 %d 个本来就是目标格式，跳过了）' % skipped
            if failed:
                text += '，%d 个失败（原因见下面的结果列表）' % failed
            self.set_status(text)
            if conv:
                self.c.notice.emit('⇄ 文件转换：%s' % text)
            self._refresh_buttons()
            self.refresh_jobs()
        elif kind == 'error':
            self.set_status('❌ %s' % (data.get('error') or '转换失败'))
            self.c.notice.emit('⇄ 转换失败：%s' % (data.get('error') or ''))
            self._refresh_buttons()
            self.refresh_jobs()
        elif kind == 'notice':
            self.set_status(data.get('text') or '')

    def refresh_jobs(self):
        """转换按钮的可用状态跟着任务走"""
        self._refresh_buttons()

    def on_open(self):
        target = self._last_out or self.outdir() or (os.path.dirname(self._files[0]) if self._files else '')
        cv.open_folder(target)

    def on_result_open(self, item):
        path = item.data(Qt.UserRole) if item is not None else ''
        if path and os.path.isfile(path):
            cv.open_folder(path)

    def set_status(self, text):
        self.status.setText(text or '')


class ConvertController(QObject):
    """面板 + 任务的生命周期管理（桌宠只认这个）"""

    notice = Signal(str)                    # 给桌宠冒一句
    job_event = Signal(object, str, object)  # job, kind, data
    targets_ready = Signal(str)             # Filee 的格式清单查完了

    def __init__(self, pet):
        super(ConvertController, self).__init__(pet)
        self.pet = pet
        self._panel = None
        self._jobs = []

    # ---------- 面板 ----------
    def panel(self, files=None):
        if self._panel is None:
            ok, hint = cv.status()
            if not ok:
                self.notice.emit('⇄ ' + hint)
            self._panel = ConvertPanel(self, self.pet)
            self.job_event.connect(self._panel.on_job_event)
            self.targets_ready.connect(self._panel.on_targets_ready)
        pwin.present(self._panel)       # v6.90：隐藏着的面板要能一键叫回来
        if files:
            self._panel.add_files(files)
        return self._panel

    def _ui(self):
        return self._panel if self._panel is not None else None

    def add_files(self, files):
        """外部（拖到桌宠身上）丢文件进来：没面板就先建，再塞文件"""
        paths = [p for p in (files or []) if p and (os.path.isfile(p) or os.path.isdir(p))]
        if not paths:
            return None
        return self.panel(paths)

    # ---------- 任务 ----------
    def jobs(self):
        return list(self._jobs)

    def current_job(self):
        for job in reversed(self._jobs):
            if job.active():
                return job
        return self._jobs[-1] if self._jobs else None

    def new_job(self, sources, target, outdir=None, overwrite=False, quality=90,
                keep_dates=False, pages=''):
        def on_event(job, kind, data):
            self.job_event.emit(job, kind, data)

        job = cv.ConvertJob(sources, target, outdir=outdir or '', overwrite=overwrite,
                            quality=quality, on_event=on_event,
                            keep_dates=keep_dates, pages=pages)
        self._jobs.append(job)
        job.start()
        return job

    def on_cancel(self):
        job = self.current_job()
        if job and job.active():
            job.cancel()

    # ---------- 收尾 ----------
    def shutdown(self):
        for job in self._jobs:
            if job.active():
                try:
                    job.cancel()
                except Exception:
                    pass
