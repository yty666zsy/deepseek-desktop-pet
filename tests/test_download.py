# -*- coding: utf-8 -*-
"""一键下载（v6.85）：yt-dlp 内核 + PySide6 面板

这里锁的是“纯逻辑 + 界面行为”，**不打网络**（真下载要靠外网，不适合进单测）：
1. 画质预设 → -f 选择器 / 音频模式参数（照 videdown 的 electron/main.ts）
2. 进度行能解析（自己打的 `PETDL_PROG|` 前缀，不去猜给人看的百分比文本）
3. yt-dlp 的英文报错能翻成能照做的中文；cookie 读不出来能认出来并降级重试
4. 剪贴板只认视频站链接，普通网页链接不许打扰
5. 任务状态机：跑完 → done、取消 → canceled、cookie 失败 → 去掉 cookie 再跑一次
6. 标题栏和右键菜单都有下载入口，面板能开、下拉有货

运行：python -m pytest tests/test_download.py -q
"""
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest


# ---------------------------------------------------------------- 纯函数
def test_quality_choices_cover_main_branches():
    import pet_download as dl
    values = [v for _label, v in dl.QUALITY_CHOICES]
    assert values == ['best', '1080', '720', '480', 'audio'], '画质预设的五个分支不能少'
    for v in values[:-1]:
        assert v in dl.FORMAT_BY_QUALITY, '%s 得有自己的 -f 选择器' % v
    assert 'height<=1080' in dl.FORMAT_BY_QUALITY['1080']


def test_build_args_video_mode():
    import pet_download as dl
    args = dl.build_args('https://www.bilibili.com/video/BV1xx', outdir='C:/dl', quality='720')
    join = ' '.join(args)
    assert '-f' in args and 'bv*[height<=720]+ba' in join, '按画质拼选择器'
    assert '--merge-output-format' in args and 'mp4' in args, '音视频分流要合并成 mp4'
    assert '--no-playlist' in args, '贴播放列表链接别把整张列表拖下来'
    assert '--newline' in args and '--no-simulate' in args and '--progress' in args
    assert dl.PROGRESS_PREFIX in join, '进度模板要带上自己的前缀'
    assert 'after_move:' in join, '要能拿到合并后的真实路径'
    assert 'C:/dl' in args, '输出目录要落到 -P 上'
    assert args[-1].startswith('https://'), '链接放最后'
    assert '--windows-filenames' in args, '标题里有 :/? 也不会炸'


def test_build_args_audio_mode():
    """只下音频：抽 mp3 + 写 ID3（videdown 那套 FFmpegMetadata 参数）"""
    import pet_download as dl
    args = dl.build_args('https://youtu.be/xxx', quality='audio')
    join = ' '.join(args)
    assert '-x' in args and '--audio-format' in args and 'mp3' in join
    assert '--audio-quality' in args
    assert 'FFmpegMetadata' in join
    assert '--merge-output-format' not in args, '音频模式不该要求合并 mp4'


def test_build_args_cookies_and_js_runtime():
    import pet_download as dl
    args = dl.build_args('https://www.bilibili.com/video/BV1xx', browser='edge')
    assert '--cookies-from-browser' in args and 'edge' in args

    args2 = dl.build_args('https://x/y', cookies_file='C:/cookies.txt')
    assert '--cookies' in args2 and 'C:/cookies.txt' in args2
    assert '--cookies-from-browser' not in args2, '两个都给时别混着上'

    # YouTube 才需要 --js-runtimes（机器上没装 node 就跳过这条）
    if dl.find_node():
        args3 = dl.build_args('https://www.youtube.com/watch?v=xx')
        assert '--js-runtimes' in args3 and 'node:' in ' '.join(args3)
    args4 = dl.build_args('https://www.bilibili.com/video/BV1xx')
    assert '--js-runtimes' not in args4, 'B站用不着 node'


def test_parse_progress_line():
    import pet_download as dl
    got = dl.parse_progress('PETDL_PROG|1024|2048|NA|512.5|12|downloading')
    assert got == {'downloaded': 1024.0, 'total': 2048.0, 'speed': 512.5,
                   'eta': 12.0, 'status': 'downloading'}
    # 总大小还没探到时用估算值顶
    assert dl.parse_progress('PETDL_PROG|1|NA|999|NA|NA|downloading')['total'] == 999.0
    # NaN/None 之类的脏值不许抛异常
    assert dl.parse_progress('PETDL_PROG|nan|None|NA|nan|NaN|downloading')['downloaded'] == 0.0
    assert dl.parse_progress('PETDL_FILE|C:/dl/a.mp4') == {'file': 'C:/dl/a.mp4'}
    assert dl.parse_progress('[download] 12.3% of 50MiB') is None
    assert dl.parse_progress('') is None


def test_explain_error_translates_common_cases():
    import pet_download as dl
    assert '登录' in dl.explain_error('ERROR: Sign in to confirm you’re not a bot')
    assert '会员' in dl.explain_error('ERROR: This video requires payment')
    assert '412' in dl.explain_error('ERROR: HTTP Error 412: Precondition Failed')
    # 认不出来的原样给（截断，不要把整段 stderr 塞进状态条）
    short = dl.explain_error('ERROR: something weird happened')
    assert 'something weird' in short and len(short) < 220


def test_is_cookie_error():
    import pet_download as dl
    assert dl.is_cookie_error('ERROR: Could not copy Chrome cookie database')
    assert dl.is_cookie_error('Failed to decrypt with DPAPI')
    assert not dl.is_cookie_error('ERROR: Unsupported URL: https://a/b')
    assert not dl.is_cookie_error('')


def test_looks_like_video_url_only_for_video_sites():
    import pet_download as dl
    assert dl.looks_like_video_url('看这个 https://www.bilibili.com/video/BV1xx411c7mD 哈哈')
    assert dl.looks_like_video_url('https://youtu.be/dQw4w9WgXcQ')
    assert not dl.looks_like_video_url('https://example.com/some/article'), '普通链接别让桌宠冒出来'
    assert not dl.looks_like_video_url('今天天气不错'), '不是链接'
    assert not dl.looks_like_video_url('')


def test_default_outdir_is_project_downloads(tmp_path, monkeypatch):
    import pet_download as dl
    monkeypatch.setattr(dl, 'CONFIG_PATH', str(tmp_path / 'config.json'))
    assert dl.default_outdir() == dl.DOWNLOAD_DIR
    dl.cfg_write('dl_dir', str(tmp_path / 'movies'))
    assert dl.default_outdir() == str(tmp_path / 'movies'), 'config 里的 dl_dir 优先'


def test_fmt_helpers():
    import pet_download as dl
    assert dl.fmt_size(0) == '—'
    assert dl.fmt_size(1536).endswith('KB')
    assert dl.fmt_speed(0) == ''
    assert 'MB/s' in dl.fmt_speed(2 * 1024 * 1024)
    assert dl.fmt_eta(90) == '1 分 30 秒'
    assert dl.fmt_duration(269) == '4:29'
    assert dl.fmt_duration(3700) == '1:01:40'


# ---------------------------------------------------------------- 任务状态机（假 yt-dlp）
_FAKE_OK = (
    "for i in range(0, 101, 10):\n"
    "    print('PETDL_PROG|%d|100|NA|1000000|3|downloading' % i, flush=True)\n"
    "print('PETDL_FILE|C:/dl/fake.mp4', flush=True)\n"
)


def _run_until(job, states, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def test_job_reports_progress_then_done(tmp_path, monkeypatch):
    """把 yt-dlp 换成一段假脚本：进度要单调往上，跑完要落到 done 且拿到文件路径"""
    import pet_download as dl
    monkeypatch.setattr(dl, 'build_args', lambda *a, **k: [sys.executable, '-c', _FAKE_OK])
    events = []
    job = dl.DownloadJob('https://example.com/v', outdir=str(tmp_path),
                         on_event=lambda j, kind, data: events.append((kind, data)))
    job.start()
    assert _run_until(job, ('done', 'error', 'canceled')), '任务没在 30 秒内结束'
    assert job.state == 'done', '假脚本正常退出应判为 done'
    assert job.percent == 100.0
    assert job.file == 'C:/dl/fake.mp4', '--print after_move 出来的路径要收到'
    assert job.error == ''
    kinds = [k for k, _ in events]
    assert 'progress' in kinds and 'done' in kinds
    percents = [d['percent'] for k, d in events if k == 'progress']
    assert percents == sorted(percents), '进度不许往回跳（多流下载时尤其重要）'
    assert job.summary().startswith('✔')


def test_job_cancel(tmp_path, monkeypatch):
    import pet_download as dl
    slow = ("import time\n"
            "for _ in range(400):\n"
            "    print('PETDL_PROG|1|100|NA|0|0|downloading', flush=True)\n"
            "    time.sleep(0.05)\n")
    monkeypatch.setattr(dl, 'build_args', lambda *a, **k: [sys.executable, '-c', slow])
    job = dl.DownloadJob('https://example.com/v', outdir=str(tmp_path))
    job.start()
    assert _run_until(job, ('running',), 10)
    job.cancel()
    assert _run_until(job, ('canceled',), 15), '取消后要尽快停下来'
    assert job.state == 'canceled'


def test_job_retries_without_cookies(tmp_path, monkeypatch):
    """Chrome/Edge 开着时 cookie 库被锁 → 不能整件事失败，去掉 cookie 再跑一次"""
    import pet_download as dl
    fail = ("import sys\n"
            "print('ERROR: Could not copy Chrome cookie database. See issue 7271', flush=True)\n"
            "sys.exit(1)\n")
    calls = []

    def fake_args(url, outdir=None, quality='best', browser='', cookies_file='', fmt_id='',
                  out_template='%(title).80s'):
        calls.append({'browser': browser, 'cookies_file': cookies_file})
        script = fail if (browser or cookies_file) else _FAKE_OK
        return [sys.executable, '-c', script]

    monkeypatch.setattr(dl, 'build_args', fake_args)
    kinds = []
    job = dl.DownloadJob('https://example.com/v', outdir=str(tmp_path), browser='edge',
                         on_event=lambda j, kind, data: kinds.append(kind))
    job.start()
    assert _run_until(job, ('done', 'error', 'canceled'), 40)
    assert job.state == 'done', 'cookie 读不出来不该让下载彻底失败'
    assert len(calls) == 2 and calls[0]['browser'] == 'edge' and calls[1]['browser'] == ''
    assert 'notice' in kinds, '得告诉用户「这次不带 cookie 下载」'


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


def test_context_menu_and_header_have_download_entry(tmp_path):
    p = _pet(tmp_path)
    menu, acts = p._build_context_menu()
    assert 'download' in acts, '右键菜单要有下载入口'
    assert '下载' in acts['download'].text()
    assert hasattr(p, 'chat_dl_btn'), '标题栏要有 ⬇ 按钮'
    assert p.chat_dl_btn.text() == '⬇'


def test_panel_builds_with_defaults(tmp_path, monkeypatch):
    """面板能开：画质/Cookie 下拉有货，默认存到 downloads/，没解析前不让点下载"""
    import pet_download as dl
    monkeypatch.setattr(dl, 'CONFIG_PATH', str(tmp_path / 'config.json'))
    from PySide6.QtWidgets import QApplication
    import pet_download_ui as du

    p = _pet(tmp_path)
    ctrl = du.DownloadController(p)
    try:
        panel = ctrl.panel()
        assert panel.windowTitle().startswith('🎬')
        qualities = [panel.quality_box.itemText(i) for i in range(panel.quality_box.count())]
        assert any('最高清' in q for q in qualities) and any('mp3' in q for q in qualities)
        assert panel.cookie_box.count() >= 1, '至少要有「不用 cookie」这一项'
        assert panel.outdir() == dl.DOWNLOAD_DIR, '默认存项目里的 downloads/'
        assert not panel.dl_btn.isEnabled(), '还没解析就把下载按钮点亮会误导人'
        assert panel.watch_chk.isChecked(), '剪贴板监听默认开着（这就是「一键」）'
    finally:
        ctrl.shutdown()
        QApplication.instance().processEvents()


def test_panel_shows_probe_failure(tmp_path, monkeypatch):
    import pet_download as dl
    monkeypatch.setattr(dl, 'CONFIG_PATH', str(tmp_path / 'config.json'))
    from PySide6.QtWidgets import QApplication
    import pet_download_ui as du

    p = _pet(tmp_path)
    ctrl = du.DownloadController(p)
    try:
        panel = ctrl.panel()
        panel.on_probed({'ok': False, 'error': '视频已删除或地区受限'})
        assert '视频已删除' in panel.info.text()
        assert not panel.dl_btn.isEnabled()
        panel.on_probed({'ok': True, 'title': '某视频', 'site': 'BiliBili', 'duration': 212,
                         'uploader': '某UP', 'heights': [1080, 720], 'format_count': 15})
        assert '某视频' in panel.info.text() and '1080P' in panel.info.text()
        assert panel.dl_btn.isEnabled()
    finally:
        ctrl.shutdown()
        QApplication.instance().processEvents()


def test_controller_clipboard_watch_opens_panel(tmp_path, monkeypatch):
    """复制视频链接 → 面板端出来并自动解析（这就是「一键下载」的入口）"""
    import pet_download as dl
    monkeypatch.setattr(dl, 'CONFIG_PATH', str(tmp_path / 'config.json'))
    from PySide6.QtWidgets import QApplication
    import pet_download_ui as du

    p = _pet(tmp_path)
    ctrl = du.DownloadController(p)
    probed = []
    monkeypatch.setattr(dl, 'probe', lambda url, browser='', cookies_file='', timeout=60:
                        probed.append(url) or {'ok': True, 'title': 't'})
    try:
        panel = ctrl.panel()
        ctrl.set_watch_clipboard(True)
        QApplication.clipboard().setText('看这个 https://www.bilibili.com/video/BV1xx411c7mD')
        for _ in range(20):
            QApplication.instance().processEvents()
            time.sleep(0.02)
        assert panel.url_edit.text().startswith('https://www.bilibili.com/video/')
        assert probed and probed[0].startswith('https://www.bilibili.com/video/')

        # 普通网页链接不许触发
        probed.clear()
        QApplication.clipboard().setText('https://example.com/article/42')
        for _ in range(10):
            QApplication.instance().processEvents()
            time.sleep(0.02)
        assert not probed, '普通链接不该让桌宠冒出来'
    finally:
        ctrl.set_watch_clipboard(False)
        ctrl.shutdown()
        QApplication.instance().processEvents()
