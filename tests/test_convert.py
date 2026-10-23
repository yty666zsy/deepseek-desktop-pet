# -*- coding: utf-8 -*-
"""文件转换（v6.86）：Filee CLI + 内置引擎兜底

这里锁的是“纯逻辑 + 界面行为”，**不打网络、不依赖 Filee 装没装**：
1. 文件归类（图片/音视频/PDF/Office/表格/压缩包/文本）与复合后缀（.tar.gz）
2. 目标格式列表按引擎能力现算；一批文件求交集（混类型就只留交得上的）
3. 引擎路由：Filee 优先 → Pillow / ffmpeg / pypdf / 标准库兜底；没引擎要把原因说人话
4. 输出命名：默认跟原文件同目录、同名不覆盖自动加序号
5. `filee formats` 的输出解析、`filee convert` 的 --json 结果解析（用假进程喂固定输出）
6. 任务状态机：跑完 done、取消 canceled、Filee 整批挂了退回内置引擎
7. 面板：拖进来的文件能上榜、默认「跟原文件放一起」、转完结果列表有 ✔
8. 桌宠：标题栏 ⇄ + 右键「⇄ 文件转换…」都在

运行：python -m pytest tests/test_convert.py -q
"""
import os
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """配置与格式缓存都指到临时目录，别把真 config.json 写脏

    并且默认「假装没装 Filee」——本机 tools/Filee 装了也不影响单测结果；
    需要 Filee 的用例自己再 patch 回来。
    """
    import pet_convert as cv
    monkeypatch.setattr(cv, 'CONFIG_PATH', str(tmp_path / 'config.json'), raising=False)
    monkeypatch.setattr(cv, 'TARGETS_CACHE', str(tmp_path / 'convert_targets.json'), raising=False)
    monkeypatch.setattr(cv, '_filee_cache', {}, raising=False)
    monkeypatch.setattr(cv, '_filee_cache_loaded', True, raising=False)
    monkeypatch.setattr(cv, '_preset_cache', None, raising=False)
    monkeypatch.setattr(cv, 'find_filee', lambda: '', raising=False)
    yield


# ---------------------------------------------------------------- 归类
def test_ext_and_kind():
    import pet_convert as cv
    assert cv.ext_of('C:/a/B.JPG') == 'jpg'
    assert cv.ext_of('备份.tar.gz') == 'tgz', '复合后缀不能被切成一截'
    assert cv.ext_of('x.tar.bz2') == 'tbz2'
    assert cv.ext_of('没有后缀') == ''
    cases = {'a.jpg': 'image', 'a.mp4': 'video', 'a.mp3': 'audio', 'a.pdf': 'pdf',
             'a.docx': 'office', 'a.xlsx': 'office', 'a.csv': 'table', 'a.zip': 'archive',
             'a.txt': 'text', 'a.zzz': ''}
    for name, kind in cases.items():
        assert cv.kind_of(name) == kind, '%s 应该归到 %s' % (name, kind)


def test_builtin_targets_per_kind():
    import pet_convert as cv
    imgs = cv.builtin_targets('jpg')
    assert 'png' in imgs and 'webp' in imgs and 'pdf' in imgs
    assert 'jpg' not in imgs, '图片别给自己列自己'
    assert 'png' not in cv.builtin_targets('png')

    audio = cv.builtin_targets('mp3')
    assert 'wav' in audio and 'flac' in audio and 'mp3' not in audio

    video = cv.builtin_targets('mp4')
    assert 'mkv' in video and 'gif' in video
    assert 'mp3' in video, '视频要能直接抽音频'

    if cv._has_module('pypdf'):
        pdf_ops = cv.builtin_targets('pdf')
        assert pdf_ops == ['txt', cv.MERGE_PDF, cv.SPLIT_PDF, cv.PAGES_PDF, cv.COMPRESS_PDF], pdf_ops
    else:
        assert cv.builtin_targets('pdf') == []
    assert cv.builtin_targets('docx') == ['txt']
    assert set(cv.builtin_targets('xlsx')) == {'csv', 'tsv', 'json'}
    assert set(cv.builtin_targets('pptx')) == {'txt'}
    assert 'xlsx' in cv.builtin_targets('csv')
    assert '7z' in cv.builtin_targets('zip') or not cv._has_module('py7zr')
    assert cv.builtin_targets('a.zzz') == []


def test_targets_for_merges_filee_and_builtin(monkeypatch):
    import pet_convert as cv
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['heic', 'jxl'])
    got = cv.targets_for('png')
    assert got[:2] == ['heic', 'jxl'], 'Filee 的能力排在前面（更全）'
    assert 'jpg' in got, '内置引擎的也要并进来'


def test_common_targets_intersection():
    import pet_convert as cv
    both = cv.common_targets(['a.jpg', 'b.png'], include_filee=False)
    assert 'webp' in both and 'bmp' in both
    # 图片和视频只在一处交得上（都写得出 gif），别的格式不能硬凑
    mixed = cv.common_targets(['a.jpg', 'b.mp4'], include_filee=False)
    assert mixed == ['gif'], mixed
    assert cv.common_targets(['a.jpg', 'b.docx'], include_filee=False) == []
    assert cv.common_targets([], include_filee=False) == []


# ---------------------------------------------------------------- 路由
def test_route_picks_engine(monkeypatch):
    import pet_convert as cv
    monkeypatch.setattr(cv, 'find_filee', lambda: '')
    if cv._has_module('PIL'):
        assert cv.route('a.jpg', 'png') == 'pillow'
    if cv.find_ffmpeg():
        assert cv.route('a.mp4', 'mp3') == 'ffmpeg'
        assert cv.route('a.mp4', 'webm') == 'ffmpeg'
    assert cv.route('a.xlsx', 'csv') == 'stdlib'
    assert cv.route('a.csv', 'json') == 'stdlib'
    assert cv.route('a.docx', 'txt') == 'stdlib'
    assert cv.route('a.zip', 'tar') == 'stdlib'
    if cv._has_module('pypdf'):
        assert cv.route('a.pdf', 'txt') == 'pypdf'
        assert cv.route('a.pdf', cv.MERGE_PDF) == 'pypdf'
    assert cv.route('a.zzz', 'png') == '', '认不出来的格式交给 Filee，内置引擎不硬扛'
    if not cv._pillow_readable('heic'):
        assert cv.route('a.heic', 'jpg') == '', 'Pillow 读不进来的源格式不许硬接（HEIC 等）'


def test_route_prefers_filee_when_it_can(monkeypatch):
    import pet_convert as cv
    monkeypatch.setattr(cv, 'find_filee', lambda: 'C:/tools/Filee/filee-cli.exe')
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['png', 'heic'])
    assert cv.route('a.heic', 'png') == 'filee'
    assert cv.route('a.heic', 'jpg') == '', 'Filee 说转不了的内置也不认 → 谁都不接'
    # 合并 PDF 是内置的事，别推给 Filee
    if cv._has_module('pypdf'):
        assert cv.route('a.pdf', cv.MERGE_PDF) == 'pypdf'


def test_no_engine_hint_is_actionable():
    import pet_convert as cv
    assert 'ffmpeg' in cv.no_engine_hint('a.mp4', 'mkv')
    assert 'Pillow' in cv.no_engine_hint('a.jpg', 'png')
    assert 'pypdf' in cv.no_engine_hint('a.pdf', cv.MERGE_PDF)
    assert 'Filee' in cv.no_engine_hint('a.zzz', 'png')
    assert cv.no_engine_hint('a.docx', 'pdf')


def test_explain_error():
    import pet_convert as cv
    assert '编码器' in cv.explain_error('Unknown encoder libvpx-vp9')
    assert '损坏' in cv.explain_error('Invalid data found when processing input')
    assert cv.explain_error('') == ''
    assert cv.explain_error('完全没见过的报错') == '', '不认识就返回空，让调用方自己截断原文'


# ---------------------------------------------------------------- 输出命名
def test_output_path_same_dir_and_unique(tmp_path):
    import pet_convert as cv
    src = tmp_path / 'trip.jpg'
    src.write_bytes(b'x')
    out = cv.output_path(str(src), 'png')
    assert out == str(tmp_path / 'trip.png'), '默认跟原文件放一起'
    (tmp_path / 'trip.png').write_bytes(b'y')
    out2 = cv.output_path(str(src), 'png')
    assert out2 == str(tmp_path / 'trip (1).png'), '同名不许覆盖，加序号'
    assert cv.output_path(str(src), 'png', overwrite=True) == str(tmp_path / 'trip.png')
    assert cv.output_path(str(src), 'png', outdir=str(tmp_path / 'sub')).endswith('sub\\trip.png') \
        or cv.output_path(str(src), 'png', outdir=str(tmp_path / 'sub')).endswith('sub/trip.png')
    assert cv.output_path(str(src), cv.MERGE_PDF).endswith('trip.pdf')


def test_target_label():
    import pet_convert as cv
    assert 'PNG' in cv.target_label('png')
    assert cv.target_label(cv.MERGE_PDF).startswith('PDF')
    assert cv.target_label('zzz') == '.zzz'


# ---------------------------------------------------------------- Filee CLI 适配
_FORMATS_OUT = """PNG image (.png) converts to:
  avif, bmp, gif, heic, icns, jpg, jxl, pdf, webp
  (with a download: dps, exr)
"""


def test_parse_formats_output():
    import pet_convert as cv
    got = cv.parse_formats_output(_FORMATS_OUT)
    assert 'jpg' in got and 'webp' in got
    assert 'exr' not in got, '要装引擎才行的格式不算「现在能用」'
    assert cv.parse_formats_output('PNG image (.png) converts to:\n  (nothing with the engines installed now)') == []
    assert cv.parse_formats_output('') == []
    assert cv.parse_formats_output('随便一段没用的输出') == []


def test_build_filee_args():
    import pet_convert as cv
    args = cv.build_filee_args('filee-cli.exe', ['C:/a/b.jpg'], 'png', outdir='C:/out',
                               overwrite=True, quality=80)
    join = ' '.join(args)
    assert args[0].endswith('filee-cli.exe') and args[1] == 'convert'
    assert '--to' in args and 'png' in args
    assert '--output' in args and 'C:/out' in args
    assert '--overwrite' in args and '--quality' in args and '80' in join
    assert '--json' in args, '要机器可读的结果'
    assert args[-1] == 'C:/a/b.jpg', '文件放最后'


_FAKE_FILEE_JSON = """import json, os, sys
files = sys.argv[1:]
out = {'preset': 'p', 'target': 'png', 'converted': len(files), 'skipped': 0, 'failed': 0,
       'files': [{'source': f, 'state': 'done',
                  'outputs': [os.path.splitext(f)[0] + '.png']} for f in files]}
print(json.dumps(out))
"""


def test_run_filee_parses_json(monkeypatch):
    import pet_convert as cv
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'build_filee_args',
                        lambda exe, sources, target, outdir='', overwrite=False, quality=0,
                        keep_dates=False:
                        [sys.executable, '-c', _FAKE_FILEE_JSON] + list(sources))
    results, err = cv.run_filee(['C:/a/b.jpg'], 'png')
    assert err == ''
    assert results[0]['ok'] and results[0]['outputs'] == ['C:/a/b.png']


def test_run_filee_reports_bad_exit(monkeypatch):
    import pet_convert as cv
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    script = "import sys\nsys.stderr.write(\"filee: unknown format 'zzz'.\")\nsys.exit(2)\n"
    monkeypatch.setattr(cv, 'build_filee_args',
                        lambda *a, **k: [sys.executable, '-c', script])
    results, err = cv.run_filee(['C:/a/b.jpg'], 'zzz')
    assert not results and '参数' in err


def test_convert_file_falls_back_to_builtin(monkeypatch, tmp_path):
    """Filee 转挂了 → 内置引擎接着干（能兜就兜，别让用户白点一次）"""
    import pet_convert as cv
    from PIL import Image
    src = tmp_path / 'a.jpg'
    Image.new('RGB', (40, 30), (10, 20, 200)).save(str(src), 'JPEG')
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['png'])
    monkeypatch.setattr(cv, '_filee_one', lambda *a, **k: {'ok': False, 'out': '', 'error': 'Filee 挂了'})
    res = cv.convert_file(str(src), 'png')
    assert res['ok'] and res['engine'] == 'pillow'
    assert os.path.isfile(res['out'])


def test_convert_file_missing_and_unsupported(tmp_path):
    import pet_convert as cv
    res = cv.convert_file(str(tmp_path / 'nope.jpg'), 'png')
    assert not res['ok'] and '不存在' in res['error']
    src = tmp_path / 'a.zzz'
    src.write_bytes(b'x')
    res = cv.convert_file(str(src), 'png')
    assert not res['ok'] and res['error']


# ---------------------------------------------------------------- 真转（用内置引擎，不打网络）
def _wait(job, states, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.state in states:
            return True
        time.sleep(0.05)
    return False


def test_batch_can_unify_to_same_format_and_skips_self(tmp_path):
    """一堆 jpg/png 混着来也能统一转成 png：已经是 png 的那个跳过，不重编码"""
    import pet_convert as cv
    from PIL import Image
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    jpg = tmp_path / 'a.jpg'
    png = tmp_path / 'b.png'
    Image.new('RGB', (20, 20), (1, 2, 3)).save(str(jpg), 'JPEG')
    Image.new('RGB', (20, 20), (4, 5, 6)).save(str(png), 'PNG')
    assert 'png' in cv.common_targets([str(jpg), str(png)], include_filee=False), \
        '批量时自身格式也得能选（不然没法统一转 png）'
    job = cv.ConvertJob([str(jpg), str(png)], 'png')
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done' and job.done_count == 2
    by_src = {os.path.basename(r['source']): r for r in job.results}
    assert by_src['a.jpg']['engine'] == 'pillow'
    assert by_src['b.png'].get('skipped') and by_src['b.png']['out'] == str(png)


def test_job_converts_images(tmp_path):
    import pet_convert as cv
    from PIL import Image
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    srcs = []
    for name in ('a.jpg', 'b.jpg'):
        p = tmp_path / name
        Image.new('RGB', (32, 24), (200, 40, 40)).save(str(p), 'JPEG')
        srcs.append(str(p))
    events = []
    job = cv.ConvertJob(srcs, 'png', on_event=lambda j, kind, data: events.append((kind, data)))
    job.start()
    assert _wait(job, ('done', 'error', 'canceled')), '任务没在 60 秒内结束'
    assert job.state == 'done' and job.done_count == 2 and job.fail_count == 0
    outs = [r['out'] for r in job.results]
    assert all(os.path.isfile(o) and o.endswith('.png') for o in outs)
    assert all(os.path.dirname(o) == str(tmp_path) for o in outs), '默认跟原文件同目录'
    percents = [d['percent'] for k, d in events if k == 'progress']
    assert percents == sorted(percents), '进度不许往回跳'
    assert job.summary().startswith('✔')


def test_job_merge_pdf(tmp_path):
    import pet_convert as cv
    if not (cv._has_module('pypdf') and cv._has_module('PIL')):
        pytest.skip('缺 pypdf/Pillow')
    from PIL import Image
    srcs = []
    for name in ('p1.pdf', 'p2.pdf'):
        p = tmp_path / name
        Image.new('RGB', (60, 60), (255, 255, 255)).save(str(p), 'PDF')
        srcs.append(str(p))
    job = cv.ConvertJob(srcs, cv.MERGE_PDF)
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done', job.error
    out = job.results[0]['out']
    assert out.endswith('.pdf') and os.path.isfile(out)
    from pypdf import PdfReader
    assert len(PdfReader(out).pages) == 2, '两个 PDF 要合并成两页'


def test_job_merge_pdf_needs_two_files(tmp_path):
    import pet_convert as cv
    p = tmp_path / 'only.pdf'
    p.write_bytes(b'%PDF-1.4\n')
    job = cv.ConvertJob([str(p)], cv.MERGE_PDF)
    job.start()
    assert _wait(job, ('error', 'done', 'canceled'))
    assert job.state == 'error' and '两个' in job.error


def test_job_cancel(tmp_path, monkeypatch):
    import pet_convert as cv
    src = tmp_path / 'a.png'
    src.write_bytes(b'x')

    def slow(*a, **k):
        time.sleep(3)
        return {'ok': True, 'out': 'x', 'engine': 'pillow', 'error': ''}

    monkeypatch.setattr(cv, 'find_filee', lambda: '')
    monkeypatch.setattr(cv, 'convert_file', slow)
    job = cv.ConvertJob([str(src), str(src)], 'jpg')
    job.start()
    assert _wait(job, ('running',), 10)
    job.cancel()
    assert _wait(job, ('canceled',), 15), '取消后要尽快停下来'


def test_job_falls_back_when_filee_batch_fails(tmp_path, monkeypatch):
    import pet_convert as cv
    from PIL import Image
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    src = tmp_path / 'a.jpg'
    Image.new('RGB', (20, 20), (1, 2, 3)).save(str(src), 'JPEG')
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['png'])
    monkeypatch.setattr(cv, 'run_filee', lambda *a, **k: ([], 'Filee 跑不起来'))
    events = []
    job = cv.ConvertJob([str(src)], 'png', on_event=lambda j, kind, data: events.append(kind))
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done', 'Filee 挂了要退回内置引擎'
    assert 'notice' in events, '得告诉用户「改用内置引擎」'
    assert os.path.isfile(job.results[0]['out'])


def test_job_filee_batch_success(tmp_path, monkeypatch):
    """Filee 能整批干时只起一次进程（不是每个文件一个进程）"""
    import pet_convert as cv
    a = tmp_path / 'a.heic'
    b = tmp_path / 'b.heic'
    a.write_bytes(b'x')
    b.write_bytes(b'y')
    calls = []

    def fake_run(sources, target, outdir='', overwrite=False, quality=0, timeout=0, **kw):
        calls.append(list(sources))
        return ([{'source': s, 'ok': True, 'outputs': [s + '.png'], 'error': ''} for s in sources], '')

    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['png'])
    monkeypatch.setattr(cv, 'run_filee', fake_run)
    job = cv.ConvertJob([str(a), str(b)], 'png')
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done' and job.done_count == 2
    assert len(calls) == 1 and len(calls[0]) == 2, '同一目录的一批文件应该一次调用搞定'


def test_job_filee_batch_respects_unified_outdir(tmp_path, monkeypatch):
    """勾了「统一输出到」时，批量的目录要传给 Filee —— 别把输出目录当文件列表（曾经踩过）"""
    import pet_convert as cv
    a = tmp_path / 'a.heic'
    b = tmp_path / 'b.heic'
    a.write_bytes(b'x')
    b.write_bytes(b'y')
    out = tmp_path / 'out'
    out.mkdir()
    calls = []

    def fake_run(sources, target, outdir='', overwrite=False, quality=0, timeout=0, **kw):
        calls.append((outdir, list(sources)))
        return ([{'source': s, 'ok': True, 'outputs': [s + '.png'], 'error': ''} for s in sources], '')

    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['png'])
    monkeypatch.setattr(cv, 'run_filee', fake_run)
    job = cv.ConvertJob([str(a), str(b)], 'png', outdir=str(out))
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done' and len(job.results) == 2, job.results
    assert [r['source'] for r in job.results] == [str(a), str(b)], '源文件清单不能被"拆成字符"'
    assert len(calls) == 1 and calls[0][0] == str(out), '一次调用，且带上统一输出目录'
    assert len(calls[0][1]) == 2


def test_job_groups_by_directory_for_filee(tmp_path, monkeypatch):
    import pet_convert as cv
    d1 = tmp_path / 'd1'
    d2 = tmp_path / 'd2'
    d1.mkdir()
    d2.mkdir()
    files = []
    for d in (d1, d2):
        p = d / 'x.heic'
        p.write_bytes(b'x')
        files.append(str(p))
    calls = []

    def fake_run(sources, target, outdir='', overwrite=False, quality=0, timeout=0, **kw):
        calls.append((outdir, list(sources)))
        return ([{'source': s, 'ok': True, 'outputs': [s + '.png'], 'error': ''} for s in sources], '')

    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_targets', lambda ext, **kw: ['png'])
    monkeypatch.setattr(cv, 'run_filee', fake_run)
    job = cv.ConvertJob(files, 'png')        # 不统一输出目录 → 跟各自原文件放一起
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert len(calls) == 2, '两个目录要分两次调用（各自输出到自己目录）'
    assert {c[0] for c in calls} == {str(d1), str(d2)}


# ---------------------------------------------------------------- 实际转换走一遍（内置引擎）
def test_real_image_and_table_conversions(tmp_path):
    import pet_convert as cv
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    from PIL import Image
    jpg = tmp_path / 'p.jpg'
    Image.new('RGB', (50, 40), (0, 128, 255)).save(str(jpg), 'JPEG')
    res = cv.convert_file(str(jpg), 'webp')
    assert res['ok'] and res['out'].endswith('.webp') and os.path.getsize(res['out']) > 0
    res = cv.convert_file(str(jpg), 'pdf')
    assert res['ok'] and res['out'].endswith('.pdf')

    csvp = tmp_path / 't.csv'
    csvp.write_text('名字,分数\n小明,90\n', encoding='utf-8')
    res = cv.convert_file(str(csvp), 'xlsx')
    assert res['ok']
    assert cv.xlsx_rows(res['out'])[0] == ['名字', '分数'], '写出来的 xlsx 要能被自己读回来'
    res = cv.convert_file(str(csvp), 'json')
    assert res['ok'] and '小明' in open(res['out'], encoding='utf-8').read()


def test_real_media_conversion(tmp_path):
    import pet_convert as cv
    ff = cv.find_ffmpeg()
    if not ff:
        pytest.skip('没装 ffmpeg')
    import subprocess
    mp4 = tmp_path / 'c.mp4'
    try:
        subprocess.run([ff, '-y', '-loglevel', 'error', '-f', 'lavfi',
                        '-i', 'testsrc=size=96x64:rate=8:duration=1',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
                        str(mp4)],
                       check=True, capture_output=True, timeout=60)
    except Exception:
        pytest.skip('这台机器的 ffmpeg 生成不了测试视频')
    res = cv.convert_file(str(mp4), 'mp3')
    assert res['ok'] and res['out'].endswith('.mp3') and os.path.getsize(res['out']) > 0
    assert cv.media_duration(str(mp4)) > 0


def test_real_office_read(tmp_path):
    """docx/xlsx/pptx 的读取只依赖标准库：手搓一个最小 docx 验一下"""
    import pet_convert as cv
    import zipfile
    docx = tmp_path / 'a.docx'
    doc_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               '<w:body><w:p><w:r><w:t>你好</w:t></w:r><w:r><w:t>世界</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>第二段</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(docx, 'w') as z:
        z.writestr('word/document.xml', doc_xml)
    text = cv.docx_text(str(docx))
    assert '你好世界' in text and text.count('\n') == 1
    res = cv.convert_file(str(docx), 'txt')
    assert res['ok'] and '第二段' in open(res['out'], encoding='utf-8').read()


# ---------------------------------------------------------------- v6.87：递归 / 质量 / 时间戳 / PDF 操作
def _make_pdf(path, pages=3):
    """造一个多页 PDF（Pillow 就能干：一页一张图）"""
    from PIL import Image
    colors = ((255, 0, 0), (0, 200, 0), (0, 0, 255), (220, 200, 0))
    imgs = [Image.new('RGB', (120, 120), colors[i % len(colors)]) for i in range(pages)]
    imgs[0].save(str(path), 'PDF', save_all=True, append_images=imgs[1:])
    return str(path)


def test_parse_presets_output():
    """`filee presets` 那张表：ID、名字、目标格式（列之间两个以上空格）"""
    import pet_convert as cv
    text = ('ID                       NAME                         TARGET\n'
            'to-png                   PNG                          png\n'
            'merge-pdf                Merge PDF                    pdf\n'
            'compress-pdf             Compress PDF                 pdf\n')
    got = cv.parse_presets_output(text)
    assert got['merge-pdf'] == 'Merge PDF' and got['compress-pdf'] == 'Compress PDF'
    assert 'ID' not in got, '表头不算预设'
    assert cv.parse_presets_output('') == {}


def test_pdf_ops_use_filee_preset_when_no_pages(tmp_path, monkeypatch):
    """没填页码 + Filee 有 pdf-split/compress-pdf 预设 → 走 Filee 预设（它的压缩更狠）"""
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    src = _make_pdf(tmp_path / '三页.pdf', 3)
    out = tmp_path / 'out'
    out.mkdir()

    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_presets',
                        lambda exe='', fetch=True: {cid: cid.upper()
                                                    for cid in cv.PDF_PRESETS.values()})
    calls = []

    def fake_preset(sources, preset, outdir='', overwrite=False, timeout=0, keep_dates=False):
        calls.append(preset)
        if preset == 'pdf-split':
            outs = [os.path.join(str(out), '三页_p%d.pdf' % (i + 1)) for i in range(3)]
            for p in outs:
                open(p, 'wb').write(b'%PDF-1.4\n')
            return [{'source': sources[0], 'ok': True, 'outputs': outs, 'error': ''}], ''
        target = os.path.join(str(out), '三页_compressed.pdf')
        open(target, 'wb').write(b'%PDF-1.4 tiny')
        return [{'source': sources[0], 'ok': True, 'outputs': [target], 'error': ''}], ''

    monkeypatch.setattr(cv, 'run_filee_preset', fake_preset)
    # pypdf 那几条路要是被走了就报错，证明真的没用它
    monkeypatch.setattr(cv, 'split_pdf', lambda *a, **k: pytest.fail('不该走 pypdf 拆分'))
    monkeypatch.setattr(cv, 'compress_pdf', lambda *a, **k: pytest.fail('不该走 pypdf 压缩'))

    res = cv.convert_file(src, cv.SPLIT_PDF, str(out))
    assert res['ok'] and res['engine'] == 'filee' and len(res['outs']) == 3
    res = cv.convert_file(src, cv.COMPRESS_PDF, str(out))
    assert res['ok'] and res['engine'] == 'filee' and res['out'].endswith('_compressed.pdf')
    assert res['note'], '压缩要报体积变化'
    assert calls == ['pdf-split', 'compress-pdf']


def test_pdf_ops_use_pypdf_when_pages_given(tmp_path, monkeypatch):
    """填了页码就轮到 pypdf —— Filee 的预设不吃页码参数"""
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    src = _make_pdf(tmp_path / '三页.pdf', 3)
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_presets',
                        lambda exe='', fetch=True: {'compress-pdf': 'Compress PDF',
                                                    'pdf-split': 'Split PDF'})
    monkeypatch.setattr(cv, 'run_filee_preset',
                        lambda *a, **k: pytest.fail('填了页码就不该用 Filee 预设'))
    from pypdf import PdfReader
    res = cv.convert_file(src, cv.COMPRESS_PDF, str(tmp_path / 'o1'), pages='1,3')
    assert res['ok'] and res['engine'] == 'pypdf'
    assert len(PdfReader(res['out']).pages) == 2
    res = cv.convert_file(src, cv.SPLIT_PDF, str(tmp_path / 'o2'), pages='2')
    assert res['ok'] and res['engine'] == 'pypdf' and len(res['outs']) == 1


def test_pdf_preset_missing_falls_back_to_pypdf(tmp_path, monkeypatch):
    """Filee 这一版没这几个预设（或压根没装）→ 照样能转（pypdf 兜底）"""
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    src = _make_pdf(tmp_path / '三页.pdf', 3)
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_presets', lambda exe='', fetch=True: {'to-png': 'PNG'})
    res = cv.convert_file(src, cv.COMPRESS_PDF, str(tmp_path / 'o'))
    assert res['ok'] and res['engine'] == 'pypdf', res


def test_merge_uses_filee_preset_when_available(tmp_path, monkeypatch):
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    a = _make_pdf(tmp_path / 'a.pdf', 2)
    b = _make_pdf(tmp_path / 'b.pdf', 2)
    merged = str(tmp_path / 'a_merged.pdf')

    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_presets', lambda exe='', fetch=True: {'merge-pdf': 'Merge PDF'})

    def fake_preset(sources, preset, outdir='', overwrite=False, timeout=0, keep_dates=False):
        assert preset == 'merge-pdf' and len(sources) == 2
        open(merged, 'wb').write(b'%PDF-1.4 merged')
        return [{'source': sources[0], 'ok': True, 'outputs': [merged], 'error': ''}], ''

    monkeypatch.setattr(cv, 'run_filee_preset', fake_preset)
    monkeypatch.setattr(cv, 'merge_pdfs', lambda *a, **k: pytest.fail('不该用 pypdf 合并'))
    job = cv.ConvertJob([a, b], cv.MERGE_PDF, outdir=str(tmp_path))
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done'
    assert job.results[0]['engine'] == 'filee' and job.results[0]['out'] == merged


def test_job_split_uses_filee_preset_when_no_pages(tmp_path, monkeypatch):
    """批量任务里也一样：没页码 → Filee 预设；有页码 → pypdf（走的是同一条 convert_file）"""
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    src = _make_pdf(tmp_path / '两页.pdf', 2)
    out = tmp_path / 'o'
    out.mkdir()
    monkeypatch.setattr(cv, 'find_filee', lambda: 'fake-filee')
    monkeypatch.setattr(cv, 'filee_presets', lambda exe='', fetch=True: {'pdf-split': 'Split PDF'})
    used = []

    def fake_preset(sources, preset, outdir='', overwrite=False, timeout=0, keep_dates=False):
        used.append(preset)
        outs = [os.path.join(str(out), '两页_p%d.pdf' % (i + 1)) for i in range(2)]
        for p in outs:
            open(p, 'wb').write(b'%PDF-1.4\n')
        return [{'source': sources[0], 'ok': True, 'outputs': outs, 'error': ''}], ''

    monkeypatch.setattr(cv, 'run_filee_preset', fake_preset)
    job = cv.ConvertJob([src], cv.SPLIT_PDF, outdir=str(out))
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done'
    assert used == ['pdf-split'], '任务里也该先试 Filee 预设'
    assert job.results[0]['engine'] == 'filee' and len(job.results[0]['outs']) == 2

    # 填了页码就轮不到预设
    job2 = cv.ConvertJob([src], cv.SPLIT_PDF, outdir=str(tmp_path / 'o2'), pages='2')
    job2.start()
    assert _wait(job2, ('done', 'error', 'canceled'))
    assert job2.state == 'done' and job2.results[0]['engine'] == 'pypdf'
    assert used == ['pdf-split'], '填了页码就不该再调预设'


def test_parse_pages_variants():
    import pet_convert as cv
    assert cv.parse_pages('1-3,5', 10) == [0, 1, 2, 4]
    assert cv.parse_pages('', 4) == [0, 1, 2, 3], '留空 = 全部页'
    assert cv.parse_pages('3,1', 5) == [0, 2], '顺序归一、去重'
    assert cv.parse_pages('2,2,2', 5) == [1], '重复的页只算一次'
    assert cv.parse_pages('9', 3) == [0, 1, 2], '超出范围就当没填（别给人一个空 PDF）'
    assert cv.parse_pages('乱七八糟', 2) == [0, 1]
    assert cv.parse_pages('2-', 4) == [1, 2, 3], '2- 到末页'
    assert cv.parse_pages('', 0) == []


def test_pdf_pages_suffix():
    import pet_convert as cv
    assert cv.pdf_pages_suffix('1-3,5') == '_第1-3、5页'
    assert cv.pdf_pages_suffix('') == '_取页'


def test_pdf_ops_listed_for_pdf_only():
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    ts = cv.targets_for('pdf')
    for op in (cv.MERGE_PDF, cv.SPLIT_PDF, cv.PAGES_PDF, cv.COMPRESS_PDF):
        assert op in ts, 'PDF 的四个操作要在下拉里：%s' % op
    assert cv.PAGES_PDF not in cv.targets_for('jpg'), '图片不该出现 PDF 专用操作'
    assert cv.target_label(cv.SPLIT_PDF) == 'PDF（每页拆成一个）'


def test_pdf_subset_split_compress_real(tmp_path):
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    from pypdf import PdfReader
    src = _make_pdf(tmp_path / '三页.pdf', 3)
    out = tmp_path / 'out'
    out.mkdir()

    res = cv.convert_file(src, cv.PAGES_PDF, str(out), pages='1,3')
    assert res['ok'] and res['engine'] == 'pypdf', res
    assert len(PdfReader(res['out']).pages) == 2, '取第 1、3 页'
    assert os.path.basename(res['out']) == '三页_第1、3页.pdf'

    res = cv.convert_file(src, cv.SPLIT_PDF, str(out))
    assert res['ok'] and len(res.get('outs') or []) == 3, '每页一个文件'
    assert all(os.path.isfile(p) for p in res['outs'])
    assert all(len(PdfReader(p).pages) == 1 for p in res['outs'])

    res = cv.convert_file(src, cv.COMPRESS_PDF, str(out))
    assert res['ok'] and os.path.isfile(res['out'])
    assert len(PdfReader(res['out']).pages) == 3, '压缩不该丢页'
    assert res['out'].endswith('_压缩.pdf')
    assert res.get('note'), '压缩要告诉用户小了没有'

    res = cv.convert_file(src, 'txt', str(out), pages='2')
    assert res['ok']


def test_merge_pdf_honors_pages(tmp_path):
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    from pypdf import PdfReader
    a = _make_pdf(tmp_path / 'a.pdf', 2)
    b = _make_pdf(tmp_path / 'b.pdf', 2)
    job = cv.ConvertJob([a, b], cv.MERGE_PDF, outdir=str(tmp_path), pages='2')
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done', job.error
    out = job.results[0]['out']
    assert len(PdfReader(out).pages) == 2, '两个 PDF 各取第 2 页 → 2 页'


def test_keep_dates_builtin_and_filee_args(tmp_path, monkeypatch):
    import pet_convert as cv
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    from PIL import Image

    monkeypatch.setattr(cv, 'find_filee', lambda: '')
    src = tmp_path / 'a.jpg'
    Image.new('RGB', (40, 30), (5, 90, 200)).save(str(src), 'JPEG')
    old = time.time() - 86400 * 40
    os.utime(str(src), (old, old))
    res = cv.convert_file(str(src), 'png', keep_dates=True)
    assert res['ok'] and res['engine'] == 'pillow'
    assert abs(os.stat(res['out']).st_mtime - old) < 2, '产物要沿用原文件的修改时间'

    res2 = cv.convert_file(str(src), 'webp', keep_dates=False)
    assert os.stat(res2['out']).st_mtime > old + 86400, '不勾就该是现在'

    args = cv.build_filee_args('f.exe', ['a.heic'], 'jpg', 'outdir', True, 55, True)
    assert '--quality' in args and '55' in args
    assert '--keep-dates' in args and '--overwrite' in args
    assert '--json' in args


def test_quality_changes_image_size(tmp_path, monkeypatch):
    import pet_convert as cv
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    from PIL import Image
    monkeypatch.setattr(cv, 'find_filee', lambda: '')
    src = tmp_path / 'noise.png'
    im = Image.new('RGB', (400, 300))
    px = im.load()
    for y in range(300):                     # 造点噪声，质量差异才看得出来
        for x in range(400):
            px[x, y] = ((x * 7 + y * 13) % 256, (x * 11 + y * 3) % 256, (x * 5 + y * 17) % 256)
    im.save(str(src))
    low = cv.convert_file(str(src), 'jpg', str(tmp_path / 'lo'), quality=20)
    high = cv.convert_file(str(src), 'jpg', str(tmp_path / 'hi'), quality=95)
    assert low['ok'] and high['ok']
    assert os.path.getsize(low['out']) < os.path.getsize(high['out']), '低质量得比高质量小'


def test_expand_paths_recursive(tmp_path):
    import pet_convert as cv
    root = tmp_path / '相册'
    sub = root / '子目录'
    sub.mkdir(parents=True)
    (root / 'a.jpg').write_bytes(b'x')
    (root / '.hidden.jpg').write_bytes(b'x')
    (sub / 'b.png').write_bytes(b'y')
    (sub / '.git').mkdir()
    (sub / '.git' / 'c.bin').write_bytes(b'z')

    deep, notes = cv.expand_paths([str(root)], recursive=True)
    names = sorted(os.path.basename(p) for p in deep)
    assert names == ['a.jpg', 'b.png'], names
    assert any('隐藏' in n for n in notes), '跳过的隐藏文件要说一声'

    shallow, _ = cv.expand_paths([str(root)], recursive=False)
    assert [os.path.basename(p) for p in shallow] == ['a.jpg']

    # 单个文件照旧
    one, _ = cv.expand_paths([str(root / 'a.jpg')])
    assert one == [str(root / 'a.jpg')]
    # 不存在的路径、空目录
    assert cv.expand_paths([str(tmp_path / 'nope')])[0] == []
    empty = tmp_path / '空的'
    empty.mkdir()
    got, notes = cv.expand_paths([str(empty)])
    assert got == [] and notes


def test_job_split_reports_multiple_outputs(tmp_path):
    import pet_convert as cv
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')
    src = _make_pdf(tmp_path / '两页.pdf', 2)
    events = []
    job = cv.ConvertJob([src], cv.SPLIT_PDF, outdir=str(tmp_path),
                        on_event=lambda j, k, d: events.append((k, d)))
    job.start()
    assert _wait(job, ('done', 'error', 'canceled'))
    assert job.state == 'done'
    assert len(job.results[0].get('outs') or []) == 2
    done = [d for k, d in events if k == 'done']
    assert done and len(done[0]['outputs']) >= 2
    assert '拆分 PDF' in job.summary()


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


def test_context_menu_and_header_have_convert_entry(tmp_path):
    p = _pet(tmp_path)
    menu, acts = p._build_context_menu()
    assert 'convert' in acts, '右键菜单要有文件转换入口'
    assert '转换' in acts['convert'].text()
    assert hasattr(p, 'chat_cv_btn'), '标题栏要有 ⇄ 按钮'
    assert p.chat_cv_btn.text() == '⇄'
    assert hasattr(p, 'open_convert_panel')


def test_drop_on_pet_opens_convert_panel(tmp_path):
    """拖文件到桌宠身上 = 打开转换面板；按住 Ctrl 拖仍是老行为（路径进聊天框）"""
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDropEvent
    from PySide6.QtWidgets import QApplication

    p = _pet(tmp_path)
    src = tmp_path / '丢进来.jpg'
    src.write_bytes(b'x')

    # 注意：mime 必须活到事件处理完（PySide 不替我们保活，GC 掉就是野指针 → 崩）
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(src))])

    def drop(mods):
        return QDropEvent(QPointF(12, 12), Qt.CopyAction, mime, Qt.LeftButton, mods)

    p.chat_input.setPlainText('')
    p.dropEvent(drop(Qt.ControlModifier))          # Ctrl = 老行为
    QApplication.instance().processEvents()
    # QUrl.toLocalFile() 回来的是正斜杠，比之前先归一化
    assert os.path.normpath(p.chat_input.toPlainText().strip()) == os.path.normpath(str(src)), 'Ctrl+拖 应该还是把路径插进聊天框'
    assert getattr(p, '_convert', None) is None, 'Ctrl+拖 不该弹转换面板'

    p.dropEvent(drop(Qt.NoModifier))               # 直接拖 = 转换
    QApplication.instance().processEvents()
    panel = p._convert.panel() if getattr(p, '_convert', None) else None
    try:
        assert panel is not None, '拖文件到桌宠身上要能打开转换面板'
        assert panel.files.count() == 1
        assert os.path.normpath(panel.files.item(0).data(Qt.UserRole)) == os.path.normpath(str(src))
    finally:
        if panel is not None:
            p._convert.shutdown()
            QApplication.instance().processEvents()


def test_panel_defaults_and_add_files(tmp_path):
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu

    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        assert panel.windowTitle().startswith('⇄')
        assert panel.outdir() == '', '默认「跟原文件放一起」，不是固定目录'
        assert not panel.go_btn.isEnabled(), '还没文件就不该点亮转换按钮'
        assert panel.engine_lbl.text(), '面板顶部要显示引擎状态'

        from PIL import Image
        for name in ('a.jpg', 'b.png'):
            Image.new('RGB', (20, 20), (9, 9, 9)).save(str(tmp_path / name))
        ctl.add_files([str(tmp_path / 'a.jpg'), str(tmp_path / 'b.png')])
        QApplication.instance().processEvents()
        assert panel.files.count() == 2
        targets = [panel.target_box.itemData(i) for i in range(panel.target_box.count())]
        assert 'webp' in targets, '两个图片共同能转的格式要在下拉里'
        assert targets.index('webp') >= 0 and 'mp3' not in targets, '不该混进视频/音频格式'
        assert panel.go_btn.isEnabled()
        # 同一路径加第二次不重复
        ctl.add_files([str(tmp_path / 'a.jpg')])
        assert panel.files.count() == 2
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_converts_and_lists_result(tmp_path):
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu
    if not cv._has_module('PIL'):
        pytest.skip('没装 Pillow')
    from PIL import Image

    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        src = tmp_path / 'x.jpg'
        Image.new('RGB', (24, 18), (120, 0, 0)).save(str(src), 'JPEG')
        panel.add_files([str(src)])
        QApplication.instance().processEvents()
        idx = panel.target_box.findData('png')
        assert idx >= 0
        panel.target_box.setCurrentIndex(idx)
        panel.on_convert()
        deadline = time.time() + 30
        while time.time() < deadline and '转好' not in panel.status.text():
            QApplication.instance().processEvents()
            time.sleep(0.05)
        assert '转好' in panel.status.text(), panel.status.text()
        assert panel.results.count() == 1 and panel.results.item(0).text().startswith('✔')
        assert os.path.isfile(str(tmp_path / 'x.png'))
        assert panel.bar.value() == 100
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_reports_mixed_types_without_targets(tmp_path, monkeypatch):
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu
    monkeypatch.setattr(cv, 'find_filee', lambda: '')
    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        a = tmp_path / 'a.jpg'
        b = tmp_path / 'b.docx'
        a.write_bytes(b'x')
        b.write_bytes(b'y')
        panel.add_files([str(a), str(b)])
        QApplication.instance().processEvents()
        assert panel.target_box.currentData() == '', '没有共同目标格式时要明确空着'
        assert not panel.go_btn.isEnabled()
        panel.on_convert()
        assert '共同' in panel.status.text() or '没有' in panel.status.text()
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_new_controls_persist(tmp_path):
    """质量滑杆 / 保留时间戳 / 页码范围 / 递归 都要记住（下次开面板还在）"""
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu

    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        panel.quality_slider.setValue(55)
        panel.keepdates_chk.setChecked(True)
        panel.pages_edit.setText('1-3,5')
        panel.recurse_chk.setChecked(False)
        QApplication.instance().processEvents()
        cfg = cv.cfg_read()
        assert cfg.get('cv_quality') == 55 and panel.quality_lbl.text() == '55'
        assert cfg.get('cv_keepdates') is True
        assert cfg.get('cv_pages') == '1-3,5'
        assert cfg.get('cv_recurse') is False

        fresh = cu.ConvertPanel(ctl, p)          # 新开一个面板：设置要读回来
        try:
            assert fresh.quality_slider.value() == 55
            assert fresh.keepdates_chk.isChecked()
            assert fresh.pages_edit.text() == '1-3,5'
            assert not fresh.recurse_chk.isChecked()
        finally:
            fresh.deleteLater()
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_missing_settings_fall_back_to_defaults(tmp_path):
    """配置里没有这些键（或被人写成 null）时得回到默认值：质量 90、递归开着"""
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu

    cv.cfg_write('cv_quality', None)
    cv.cfg_write('cv_recurse', None)
    cv.cfg_write('cv_pages', None)
    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        assert panel.quality_slider.value() == 90, '质量默认 90'
        assert panel.recurse_chk.isChecked(), '「包含子文件夹」默认是开着的'
        assert panel.pages_edit.text() == ''
        # 删键也要能用（验证脚本收尾就靠它，别在用户配置里留 null）
        assert cv.cfg_delete('cv_quality')
        assert 'cv_quality' not in cv.cfg_read()
        assert cv.cfg_delete('cv_quality'), '删一个不存在的键不算错'
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_pages_field_only_for_pdf(tmp_path):
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu
    if not cv._has_module('pypdf'):
        pytest.skip('没装 pypdf')

    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        jpg = tmp_path / 'a.jpg'
        jpg.write_bytes(b'x')
        panel.add_files([str(jpg)])
        QApplication.instance().processEvents()
        assert not panel.pages_edit.isEnabled(), '没有 PDF 时页码框应该是灰的'

        pdf = _make_pdf(tmp_path / 'b.pdf', 2)
        panel.on_clear()                        # 只留 PDF，才能出现「PDF 取页」这类操作
        panel.add_files([pdf])
        QApplication.instance().processEvents()
        assert panel.pages_edit.isEnabled(), '有 PDF 了才能填页码'

        # 选了「PDF 取页」却没填页码 → 拦住，不建任务
        idx = panel.target_box.findData(cv.PAGES_PDF)
        assert idx >= 0
        panel.target_box.setCurrentIndex(idx)
        panel.pages_edit.setText('')
        before = len(ctl.jobs())
        panel.on_convert()
        assert '页码' in panel.status.text()
        assert len(ctl.jobs()) == before, '填漏了就别真起任务'
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_targets_have_no_duplicates(tmp_path, monkeypatch):
    """后台线程查完 Filee 再填一次下拉 —— 不清空就会重复（曾经重复了）"""
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu

    monkeypatch.setattr(cv, 'find_filee', lambda: '')
    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        src = tmp_path / 'a.png'
        src.write_bytes(b'x')
        panel.add_files([str(src)])
        QApplication.instance().processEvents()
        panel.on_targets_ready('')            # 手动模拟一次「后台查完了」
        QApplication.instance().processEvents()
        data = [panel.target_box.itemData(i) for i in range(panel.target_box.count())]
        assert len(data) == len(set(data)), '下拉里不能有重复项：%s' % data
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()


def test_panel_accepts_folder_and_recurses(tmp_path):
    """拖文件夹进来：勾了递归就连子目录一起收，不勾只收第一层"""
    import pet_convert as cv
    from PySide6.QtWidgets import QApplication
    import pet_convert_ui as cu

    p = _pet(tmp_path)
    ctl = cu.ConvertController(p)
    try:
        panel = ctl.panel()
        root = tmp_path / '素材'
        (root / '深一层').mkdir(parents=True)
        (root / 'a.jpg').write_bytes(b'x')
        (root / '深一层' / 'b.png').write_bytes(b'y')
        panel.recurse_chk.setChecked(False)
        panel.on_clear()
        panel.add_files([str(root)])
        QApplication.instance().processEvents()
        assert panel.files.count() == 1, '不勾递归只收第一层'

        panel.recurse_chk.setChecked(True)
        panel.on_clear()
        panel.add_files([str(root)])
        QApplication.instance().processEvents()
        assert panel.files.count() == 2, '勾了递归要把子目录里的也收进来'
    finally:
        ctl.shutdown()
        QApplication.instance().processEvents()
