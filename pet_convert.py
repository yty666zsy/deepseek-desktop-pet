# -*- coding: utf-8 -*-
"""pet_convert.py — 「文件转换」内核（v6.87）

为什么长这样（跟 v6.85 的下载一个套路）：
    Filee（github.com/KnifeLemon/Filee，MIT，255 种格式离线互转）是 **C#/.NET 的桌面应用**
    （Avalonia），不可能当 Python 库 import 进来。但它自带命令行 `filee-cli.exe`：
        filee-cli convert -t <格式> [-o 输出目录] [-q 质量] [-r] [--keep-dates]
                          [--json] [--overwrite|--skip] 文件...
        filee-cli convert -p <预设> ...        ← PDF 的合并/拆分/压缩在预设里（merge-pdf / pdf-split / compress-pdf）
        退出码：0 全成 / 1 有失败 / 2 参数或输入有问题 / 3 没东西可转
    所以这里跟「videdown → yt-dlp」完全一个思路：**界面自己写，能力借它的 CLI**。

    另外还写了一套**内置引擎兜底**，没装 Filee（或它这一版转不了）时照样能干活：
        Pillow     图片互转 + 图片→PDF
        ffmpeg     音频/视频互转、视频抽音频、视频→GIF
        pypdf      PDF→文本、多个 PDF 合并、取页 / 拆分 / 压缩（页码只有它能做）
        标准库      docx→txt、xlsx→csv/tsv/json、pptx→txt、CSV/TSV/JSON/XLSX 互转
        py7zr      压缩包重打包（zip / 7z / tar / tar.gz）

纪律：
- 一律子进程 + 后台线程，GUI 只收信号（跟下载/音乐一个规矩）
- 转换失败要说清原因（缺引擎 / 缺编码器 / 文件坏了），别丢一串 stderr
- 输出默认**跟原文件放一起**（Filee 的默认行为），同名不覆盖、自动加序号
"""

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from xml.etree import ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
FILEE_DIR = os.path.join(BASE_DIR, 'tools', 'Filee')
CACHE_DIR = os.path.join(BASE_DIR, 'logs')
TARGETS_CACHE = os.path.join(CACHE_DIR, 'convert_targets.json')

# Windows 下 pythonw 起子进程会弹黑框，统一加这个标志
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# PDF 的这几个不是「转成什么格式」，是**操作** —— 用一个不会跟格式名撞车的记号表示
MERGE_PDF = 'pdf+merge'          # 多个合并成一个
SPLIT_PDF = 'pdf+split'          # 每页拆成一个 PDF
COMPRESS_PDF = 'pdf+compress'    # 压缩瘦身（去重对象 + 压缩内容流）
PAGES_PDF = 'pdf+pages'          # 只留下指定页码范围，还是一个 PDF
PDF_OPS = (MERGE_PDF, SPLIT_PDF, COMPRESS_PDF, PAGES_PDF)

# ---------------------------------------------------------------- 格式分类
# 注意：这些只是「我们认得」的分类；真正能转成什么由引擎现场说了算（见 targets_for）
IMAGE_EXTS = ('jpg', 'jpeg', 'jpe', 'jfif', 'png', 'webp', 'bmp', 'tif', 'tiff', 'gif',
              'ico', 'ppm', 'pgm', 'pbm', 'tga', 'avif', 'heic', 'heif', 'jxl', 'jp2',
              'psd', 'dds', 'pcx', 'exr', 'hdr')
AUDIO_EXTS = ('mp3', 'wav', 'flac', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'wma', 'aiff', 'aif',
              'amr', 'ape', 'mka')
VIDEO_EXTS = ('mp4', 'mkv', 'webm', 'avi', 'mov', 'wmv', 'flv', 'm4v', 'ts', 'mpg', 'mpeg',
              '3gp', 'rmvb', 'vob', 'mts')
PDF_EXTS = ('pdf',)
OFFICE_EXTS = ('docx', 'xlsx', 'pptx')
TABLE_EXTS = ('csv', 'tsv', 'json')
ARCHIVE_EXTS = ('zip', '7z', 'tar', 'tgz', 'tbz2', 'txz', 'gz', 'bz2', 'xz')
TEXT_EXTS = ('txt', 'log', 'md', 'markdown', 'ini', 'cfg', 'conf', 'srt', 'ass', 'vtt',
             'html', 'htm', 'xml', 'sql', 'py', 'js', 'css', 'yml', 'yaml', 'bat', 'sh')

# 界面上展示目标格式时的中文说明（查不到就直接显示大写扩展名）
TARGET_LABELS = {
    'png': 'PNG 图片', 'jpg': 'JPG 图片', 'webp': 'WebP 图片', 'bmp': 'BMP 图片',
    'tiff': 'TIFF 图片', 'gif': 'GIF 图片', 'ico': 'ICO 图标', 'ppm': 'PPM 图片',
    'tga': 'TGA 图片', 'avif': 'AVIF 图片', 'heic': 'HEIC 图片', 'jxl': 'JPEG XL',
    'jp2': 'JPEG 2000', 'psd': 'PSD 分层图', 'pdf': 'PDF 文档',
    'icns': 'ICNS 图标', 'qoi': 'QOI 图片', 'dds': 'DDS 贴图', 'pcx': 'PCX 图片',
    'mp3': 'MP3 音频', 'wav': 'WAV 音频', 'flac': 'FLAC 无损', 'm4a': 'M4A 音频',
    'aac': 'AAC 音频', 'ogg': 'OGG 音频', 'opus': 'Opus 音频', 'wma': 'WMA 音频',
    'aiff': 'AIFF 音频',
    'mp4': 'MP4 视频', 'mkv': 'MKV 视频', 'webm': 'WebM 视频', 'avi': 'AVI 视频',
    'mov': 'MOV 视频', 'wmv': 'WMV 视频', 'flv': 'FLV 视频', 'm4v': 'M4V 视频',
    'mpg': 'MPEG 视频', '3gp': '3GP 视频', 'ts': 'TS 视频',
    'docx': 'Word 文档', 'doc': 'Word 97-2003', 'rtf': 'RTF 文档', 'odt': 'ODT 文档',
    'xlsx': 'Excel 表格', 'xls': 'Excel 97-2003', 'ods': 'ODS 表格',
    'pptx': 'PowerPoint', 'odp': 'ODP 演示',
    'txt': '纯文本', 'md': 'Markdown', 'html': '网页 HTML', 'csv': 'CSV 表格',
    'tsv': 'TSV 表格', 'json': 'JSON 数据', 'xml': 'XML 数据',
    'epub': 'EPUB 电子书', 'mobi': 'Kindle MOBI', 'azw3': 'Kindle AZW3',
    'zip': 'ZIP 压缩包', '7z': '7Z 压缩包', 'tar': 'TAR 归档', 'tgz': 'tar.gz 归档',
    'svg': 'SVG 矢量图', 'dxf': 'DXF 图纸', 'stl': 'STL 模型',
    MERGE_PDF: 'PDF（多个合并成一个）',
    SPLIT_PDF: 'PDF（每页拆成一个）',
    COMPRESS_PDF: 'PDF（压缩瘦身）',
    PAGES_PDF: 'PDF（只留指定页码）',
}

# Pillow 的格式名 ↔ 扩展名（只列我们愿意在界面上露出来的）
_PIL_FORMATS = (
    ('png', 'PNG'), ('jpg', 'JPEG'), ('webp', 'WEBP'), ('pdf', 'PDF'),
    ('bmp', 'BMP'), ('tiff', 'TIFF'), ('gif', 'GIF'), ('ico', 'ICO'),
    ('ppm', 'PPM'), ('tga', 'TGA'), ('avif', 'AVIF'), ('jp2', 'JPEG2000'),
    ('icns', 'ICNS'), ('qoi', 'QOI'), ('pcx', 'PCX'), ('dds', 'DDS'),
)

# ffmpeg 编码参数：够用、跨机器稳（libx264 / aac 是 gyan.dev 与 wingen 两个 build 都带的）
_AUDIO_ARGS = {
    'mp3': ['-c:a', 'libmp3lame', '-q:a', '2'],
    'wav': ['-c:a', 'pcm_s16le'],
    'flac': ['-c:a', 'flac', '-compression_level', '5'],
    'm4a': ['-c:a', 'aac', '-b:a', '192k'],
    'aac': ['-c:a', 'aac', '-b:a', '192k'],
    'ogg': ['-c:a', 'libvorbis', '-q:a', '5'],
    'oga': ['-c:a', 'libvorbis', '-q:a', '5'],
    'opus': ['-c:a', 'libopus', '-b:a', '128k'],
    'wma': ['-c:a', 'wmav2', '-b:a', '192k'],
    'aiff': ['-c:a', 'pcm_s16be'],
    'mka': ['-c:a', 'libopus', '-b:a', '128k'],
}
_VIDEO_ARGS = {
    'mp4': ['-c:v', 'libx264', '-crf', '23', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart'],
    'm4v': ['-c:v', 'libx264', '-crf', '23', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k'],
    'mov': ['-c:v', 'libx264', '-crf', '23', '-preset', 'veryfast', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart'],
    'mkv': ['-c:v', 'libx264', '-crf', '23', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '192k'],
    'webm': ['-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0', '-c:a', 'libopus', '-b:a', '128k'],
    'avi': ['-c:v', 'mpeg4', '-qscale:v', '4', '-c:a', 'libmp3lame', '-q:a', '4'],
    'flv': ['-c:v', 'libx264', '-crf', '23', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '128k'],
    'wmv': ['-c:v', 'wmv2', '-qscale:v', '4', '-c:a', 'wmav2', '-b:a', '192k'],
    'ts': ['-c:v', 'libx264', '-crf', '23', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '192k'],
    'mpg': ['-c:v', 'mpeg2video', '-q:v', '5', '-c:a', 'mp2', '-b:a', '192k'],
    '3gp': ['-c:v', 'libx264', '-crf', '26', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '96k'],
}

_NS_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
_NS_S = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
_NS_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_NS_R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
_NS_PKG_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'


# ---------------------------------------------------------------- 配置
def cfg_read():
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def cfg_write(key, value):
    """只改一个键（读-改-写，别把别人的字段冲掉）"""
    try:
        cfg = cfg_read()
        cfg[key] = value
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def cfg_delete(key):
    """删掉一个键（验证脚本收尾用：本来没有的键别留个 null 在用户配置里）"""
    try:
        cfg = cfg_read()
        if key not in cfg:
            return True
        cfg.pop(key, None)
        tmp = CONFIG_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except Exception:
        return False


def _cfg_text(key):
    return str(cfg_read().get(key) or '').strip()


def _cfg_int(key, default=0):
    try:
        return int(cfg_read().get(key, default) or default)
    except Exception:
        return default


# ---------------------------------------------------------------- 找工具
_version_cache = {}


def find_filee():
    """Filee 的 CLI 在哪：配置 → 项目 tools/Filee → PATH → 安装版常见路径

    便携版解压出来是 `Filee.exe`（界面）旁边一个 `filee-cli.exe`（命令行），
    安装版会把 `cli\\filee.cmd` 加到 PATH，所以这几个名字都要试。
    """
    hit = _cfg_text('cv_filee_path')
    if hit and os.path.isfile(hit):
        return hit
    names = ('filee-cli.exe', 'filee-cli', 'filee.exe', 'filee')
    # 项目内便携版（含再套一层目录的解压结果）
    roots = [FILEE_DIR] + [os.path.join(FILEE_DIR, d) for d in _safe_listdir(FILEE_DIR)]
    roots.append(os.path.join(FILEE_DIR, 'cli'))
    for root in roots:
        for name in names:
            p = os.path.join(root, name)
            if os.path.isfile(p):
                return p
    for name in names:
        try:
            hit = shutil.which(name)
        except Exception:
            hit = None
        if hit:
            return hit
    for sub in ('Programs/Filee', 'Filee', 'Programs/filee'):
        for base in (os.environ.get('LOCALAPPDATA') or '', os.environ.get('ProgramFiles') or ''):
            if not base:
                continue
            for name in names:
                p = os.path.join(base, sub.replace('/', os.sep), name)
                if os.path.isfile(p):
                    return p
            p = os.path.join(base, sub.replace('/', os.sep), 'cli', 'filee-cli.exe')
            if os.path.isfile(p):
                return p
    return ''


def _safe_listdir(path):
    try:
        return sorted(os.listdir(path))
    except Exception:
        return []


def filee_version(exe=''):
    """跑一次 `filee version` 拿版本号（失败返回空串）

    记住结果：这个值会被目标格式缓存的键用到，每个文件都起一次进程没必要。
    """
    exe = exe or find_filee()
    if not exe:
        return ''
    try:
        stamp = os.path.getmtime(exe)
    except Exception:
        stamp = 0
    key = (exe, stamp)
    if key in _version_cache:
        return _version_cache[key]
    text = ''
    try:
        out = subprocess.run([exe, 'version'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW, timeout=20)
        raw = (out.stdout or b'').decode('utf-8', 'replace').strip()
        text = raw.splitlines()[0].strip() if raw else ''
    except Exception:
        text = ''
    _version_cache[key] = text
    return text


def find_ffmpeg():
    """ffmpeg：配置 → PATH → 复用下载模块那套找法（yt-dlp 也在用）"""
    hit = _cfg_text('cv_ffmpeg_path')
    if hit and os.path.isfile(hit):
        return hit
    for name in ('ffmpeg.exe', 'ffmpeg'):
        hit = shutil.which(name)
        if hit:
            return hit
    try:
        import pet_download
        return pet_download.find_ffmpeg() or ''
    except Exception:
        return ''


def find_ffprobe():
    hit = _cfg_text('cv_ffprobe_path')
    if hit and os.path.isfile(hit):
        return hit
    ff = find_ffmpeg()
    if ff:
        cand = os.path.join(os.path.dirname(ff), 'ffprobe.exe')
        if os.path.isfile(cand):
            return cand
    for name in ('ffprobe.exe', 'ffprobe'):
        hit = shutil.which(name)
        if hit:
            return hit
    try:
        import pet_download
        return pet_download.find_ffmpeg().replace('ffmpeg', 'ffprobe') or ''
    except Exception:
        return ''


def _has_module(name):
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def builtin_engines():
    """内置引擎在不在（探一次，界面直接拿来显示）"""
    return {
        'pillow': _has_module('PIL'),
        'ffmpeg': bool(find_ffmpeg()),
        'pypdf': _has_module('pypdf'),
        'py7zr': _has_module('py7zr'),
        'stdlib': True,          # docx/xlsx/pptx 解析、CSV/JSON、zip/tar 全靠标准库
    }


def status():
    """(能不能用, 一行说明) —— 面板顶部那行就显示这个"""
    eng = builtin_engines()
    fe = find_filee()
    bits = []
    if fe:
        ver = filee_version(fe)                 # "filee 1.12.0" → "1.12.0"（别显示成 Filee filee 1.12.0）
        if ver.lower().startswith('filee'):
            ver = ver[5:].strip()
        bits.append('Filee %s（255 种格式）' % (ver or '便携版'))
    else:
        bits.append('未装 Filee（只有内置引擎）')
    have = []
    if eng['pillow']:
        have.append('图片')
    if eng['ffmpeg']:
        have.append('音视频')
    if eng['pypdf']:
        have.append('PDF')
    if eng['py7zr']:
        have.append('7z')
    have.append('Office/表格/压缩包')
    bits.append('内置：' + '·'.join(have))
    ok = bool(fe) or eng['pillow'] or eng['ffmpeg'] or eng['pypdf']
    return ok, '引擎：' + ' ｜ '.join(bits)


# ---------------------------------------------------------------- 格式判断
def ext_of(path):
    """小写扩展名；`.tar.gz` 这种复合后缀保留成 `tgz`"""
    name = os.path.basename(str(path or '')).lower()
    if name.endswith('.tar.gz'):
        return 'tgz'
    if name.endswith('.tar.bz2'):
        return 'tbz2'
    if name.endswith('.tar.xz'):
        return 'txz'
    ext = os.path.splitext(name)[1]
    return ext[1:] if ext.startswith('.') else ext


def kind_of(path):
    """这个文件归哪一类（决定给它列哪些目标格式）"""
    ext = ext_of(path)
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in AUDIO_EXTS:
        return 'audio'
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in PDF_EXTS:
        return 'pdf'
    if ext in OFFICE_EXTS:
        return 'office'
    if ext in TABLE_EXTS:
        return 'table'
    if ext in ARCHIVE_EXTS:
        return 'archive'
    if ext in TEXT_EXTS:
        return 'text'
    return ''


# ---------------------------------------------------------------- 目标格式
_PIL_WRITE_CACHE = None


def _pillow_targets():
    """Pillow 这个 build 真正写得出来的格式（不猜，问它自己）

    注意：Pillow 13 的 `Image.SAVE` 是**懒加载**的，不先 init 就是空字典 —— 所以先 registered_extensions()
    """
    global _PIL_WRITE_CACHE
    if _PIL_WRITE_CACHE is not None:
        return list(_PIL_WRITE_CACHE)
    out = []
    try:
        from PIL import Image
        Image.init()
        try:
            Image.registered_extensions()
        except Exception:
            pass
        have = set(getattr(Image, 'SAVE', {}) or {})
    except Exception:
        _PIL_WRITE_CACHE = []
        return out
    for ext, fmt in _PIL_FORMATS:
        if fmt in have:
            out.append(ext)
    _PIL_WRITE_CACHE = list(out)
    return out


def _pillow_readable(ext):
    """这个扩展名 Pillow 读得进来吗（能写 ≠ 能读：HEIC 能写不代表能读）"""
    try:
        from PIL import Image
        Image.init()
        exts = Image.registered_extensions()
    except Exception:
        return False
    return ('.' + str(ext or '').lower().lstrip('.')) in exts


def kind_targets(ext):
    """这一类文件「统一转成 X」时，X 能是哪些（**含自身格式**，批量用）

    区别：`builtin_targets(ext)` 是单个文件（会排掉自己，png→png 没意义），
    这里给批量算交集用 —— 一堆 jpg/png 混着来，也得能统一转成 png（已经是 png 的就跳过）。
    """
    ext = (ext or '').lower()
    kind = kind_of('x.' + ext)
    if kind == 'image':
        return list(_pillow_targets()) if _pillow_readable(ext) else []
    if kind == 'audio':
        return _audio_targets()
    if kind == 'video':
        return _dedupe(list(_VIDEO_ARGS) + ['gif'] + _audio_targets())
    if kind == 'pdf':
        out = ['txt'] if _has_module('pypdf') else []
        if _has_module('pypdf'):
            out = out + [MERGE_PDF, SPLIT_PDF, PAGES_PDF, COMPRESS_PDF]
        return out
    if kind == 'office':
        if ext == 'docx':
            return ['txt']
        if ext == 'xlsx':
            return ['csv', 'tsv', 'json']
        if ext == 'pptx':
            return ['txt']
        return []
    if kind == 'table':
        return ['csv', 'tsv', 'json', 'xlsx']
    if kind == 'archive':
        out = ['zip', 'tar', 'tgz']
        if _has_module('py7zr'):
            out.append('7z')
        return out
    if kind == 'text':
        return ['txt']
    return []


def _all_targets(ext, include_filee=True, fetch_filee=True):
    """一个文件所有能转的目标格式（批量交集用，含自身格式）"""
    ext = (ext or '').lower()
    out = list(kind_targets(ext))
    if include_filee:
        fe = filee_targets(ext, fetch=fetch_filee)
        if fe:
            out = _dedupe(list(fe) + out)
    return out


def builtin_targets(ext):
    """内置引擎能把这个扩展名转成什么"""
    ext = (ext or '').lower()
    kind = kind_of('x.' + ext)
    if kind == 'image':
        if not _pillow_readable(ext):
            return []          # 读不进来的源格式别硬许诺（HEIC/JXL 这类交给 Filee）
        out = list(_pillow_targets())
        # 源格式本身别列出来（png→png 没意义），但 png→jpg 这种照旧
        return [t for t in out if t != ext and not (ext in ('jpg', 'jpeg') and t in ('jpg', 'jpeg'))]
    if kind == 'audio':
        return [t for t in _audio_targets() if t != ext]
    if kind == 'video':
        out = [t for t in _VIDEO_ARGS if t != ext]
        out += ['gif'] + [t for t in _audio_targets() if t != ext]     # 视频抽音频
        return _dedupe(out)
    if kind == 'pdf':
        if not _has_module('pypdf'):
            return []
        return ['txt', MERGE_PDF, SPLIT_PDF, PAGES_PDF, COMPRESS_PDF]
    if kind == 'office':
        if ext == 'docx':
            return ['txt']
        if ext == 'xlsx':
            return ['csv', 'tsv', 'json']
        if ext == 'pptx':
            return ['txt']
        return []
    if kind == 'table':
        out = [t for t in ('csv', 'tsv', 'json', 'xlsx') if t != ext]
        return out
    if kind == 'archive':
        out = [t for t in ('zip', '7z', 'tar', 'tgz') if t != ext]
        if not _has_module('py7zr'):
            out = [t for t in out if t != '7z']
        return out
    if kind == 'text':
        return ['txt'] if ext != 'txt' else []
    return []


def _audio_targets():
    return list(_AUDIO_ARGS.keys())


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---- Filee 的目标格式：问它自己（结果缓存到 logs/convert_targets.json） ----
_filee_cache = {}
_filee_cache_lock = threading.Lock()
_filee_cache_loaded = False


def _load_targets_cache(version=''):
    global _filee_cache, _filee_cache_loaded
    with _filee_cache_lock:
        if _filee_cache_loaded:
            return
        _filee_cache_loaded = True
        try:
            with open(TARGETS_CACHE, encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception:
            return
        # Filee 升级过就整份作废（格式支持是跟着版本变的）
        if version and str(data.get('_filee_version') or '') != version:
            return
        for key, val in data.items():
            if not key.startswith('_') and isinstance(val, list):
                _filee_cache[key] = [str(x) for x in val if x]


def _save_targets_cache(version=''):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = {'_filee_version': version}
        data.update(_filee_cache)
        tmp = TARGETS_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, TARGETS_CACHE)
    except Exception:
        pass


def filee_targets(ext, exe='', use_cache=True, fetch=True):
    """`filee formats <ext>` 问出「这个格式能转成什么」（只取现在就能用的那行）

    fetch=False 时只查内存/磁盘缓存，绝不起进程 —— 界面线程算下拉框时用这个，
    真正的查询交给后台线程（起一次进程问一个格式，几百毫秒）。
    """
    ext = (ext or '').lower()
    if not ext:
        return []
    exe = exe or find_filee()
    if not exe:
        return []
    ver = filee_version(exe) if use_cache else ''
    _load_targets_cache(ver)
    if use_cache:
        with _filee_cache_lock:
            if ext in _filee_cache:
                return list(_filee_cache[ext])
    if not fetch:
        return []
    try:
        proc = subprocess.run([exe, 'formats', ext], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW, timeout=40)
        text = (proc.stdout or b'').decode('utf-8', 'replace')
    except Exception:
        return []
    targets = parse_formats_output(text)
    with _filee_cache_lock:
        _filee_cache[ext] = targets
    _save_targets_cache(ver)
    return list(targets)


def parse_formats_output(text):
    """解析 `filee formats png` 的输出：
         PNG image (.png, .png) converts to:
           a, b, c
           (with a download: d, e)      ← 这行要装引擎才能用，跳过
       只取「现在就能用」的第一行。
    """
    lines = [ln.rstrip() for ln in str(text or '').splitlines() if ln.strip()]
    if not lines:
        return []
    for i, line in enumerate(lines):
        if 'converts to' in line.lower() and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt.startswith('('):       # (nothing with the engines installed now)
                return []
            out = []
            for part in nxt.split(','):
                token = part.strip().lower()
                if re.match(r'^[a-z0-9+._-]{1,12}$', token):
                    out.append(token)
            return _dedupe(out)
    return []


def targets_for(ext, include_filee=True, fetch_filee=True):
    """给界面用：这个扩展名能转成的所有格式（Filee 优先、内置兜底，去重后保持「常用在前」）"""
    ext = (ext or '').lower()
    fe = filee_targets(ext, fetch=fetch_filee) if include_filee else []
    bi = builtin_targets(ext)
    out = _dedupe(list(fe) + list(bi))
    return out


def target_label(target):
    if target in PDF_OPS:
        return TARGET_LABELS[target]
    t = str(target or '').lower()
    label = TARGET_LABELS.get(t)
    return '%s（.%s）' % (label, t) if label else '.%s' % t


# ---------------------------------------------------------------- 输出路径
def unique_path(path):
    """同名文件已存在就加 (1)、(2)……（Filee 默认也是这么干）"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = '%s (%d)%s' % (base, i, ext)
        if not os.path.exists(cand):
            return cand
    return '%s (%d)%s' % (base, int(time.time()), ext)


def _stem_of(path):
    """文件名去掉后缀（`.tar.gz` 这种复合后缀整段去掉，别留个 `x.tar` 当文件名）"""
    name = os.path.basename(str(path or ''))
    low = name.lower()
    for suf in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tgz', '.tbz2', '.txz'):
        if low.endswith(suf):
            return name[:len(name) - len(suf)]
    return os.path.splitext(name)[0]


def output_path(src, target, outdir=None, overwrite=False, suffix=''):
    """算输出文件名：跟原文件同名、换个后缀、放指定目录

    suffix 是给「压缩」「取页」这类同名同格式的操作用的，免得把原文件当成输出。
    """
    outdir = outdir or os.path.dirname(os.path.abspath(src))
    stem = _stem_of(src)
    if target in PDF_OPS or target == 'pdf':
        name = stem + (suffix or '') + '.pdf'
    else:
        name = '%s%s.%s' % (stem, suffix or '', target)
    path = os.path.join(outdir, name)
    return path if overwrite else unique_path(path)


# ---------------------------------------------------------------- 引擎路由
def _image_engine_ok():
    return _has_module('PIL')


def builtin_engine_for(src_ext, target):
    """内置引擎里谁接这一单（接不了返回空串）"""
    src_ext = (src_ext or '').lower()
    kind = kind_of('x.' + src_ext)
    if target in PDF_OPS:
        return 'pypdf' if (_has_module('pypdf') and kind == 'pdf') else ''
    if kind == 'image':
        if target in _pillow_targets() and _pillow_readable(src_ext):
            return 'pillow'
        return ''
    if kind in ('audio', 'video'):
        if not find_ffmpeg():
            return ''
        if target in _AUDIO_ARGS or (kind == 'video' and (target in _VIDEO_ARGS or target == 'gif')):
            return 'ffmpeg'
        return ''
    if kind == 'pdf':
        if target == 'txt' and _has_module('pypdf'):
            return 'pypdf'
        return ''
    if kind == 'office':
        if src_ext in OFFICE_EXTS and target in ('txt', 'csv', 'tsv', 'json'):
            return 'stdlib'
        return ''
    if kind == 'table':
        if target in ('csv', 'tsv', 'json', 'xlsx'):
            return 'stdlib'
        return ''
    if kind == 'archive':
        if target == '7z' and not _has_module('py7zr'):
            return ''
        if target in ('zip', '7z', 'tar', 'tgz'):
            return 'stdlib'
        return ''
    if kind == 'text':
        if target == 'txt':
            return 'stdlib'
        return ''
    return ''


def route(src, target, prefer_filee=True):
    """这条转换交给谁：'filee' / 'pillow' / 'ffmpeg' / 'pypdf' / 'stdlib' / '' """
    ext = ext_of(src)
    if target in PDF_OPS:
        # PDF 的合并/拆分/取页/压缩：pypdf 是兜底 —— 有 Filee 的话 convert_file 里会先试它的预设
        return 'pypdf' if _has_module('pypdf') else ''
    if prefer_filee and find_filee() and target in filee_targets(ext):
        return 'filee'
    return builtin_engine_for(ext, target)


def no_engine_hint(src, target):
    """找不到引擎时给人话，而不是「转换失败」四个字"""
    ext = ext_of(src)
    kind = kind_of(src)
    if target in PDF_OPS:
        return 'PDF 的合并/拆分/取页/压缩：装 pypdf（pip install pypdf），或者装 Filee 便携版'
    if kind in ('audio', 'video'):
        return '音视频转换要 ffmpeg：装一个（winget install Gyan.FFmpeg）或把 ffmpeg 放进 PATH'
    if kind == 'image':
        return '图片转换要 Pillow：pip install pillow'
    if kind == 'pdf':
        return ('PDF 这一版只能转文本（pypdf）；PDF→图片/Word 需要装 Filee：'
                '把便携版解压到 tools/Filee/ 即可')
    if not kind:
        return '%s 这个格式内置引擎不认得，装 Filee（255 种格式）就能转' % ('.' + ext if ext else '该文件')
    return '%s → %s 内置引擎做不了，装 Filee 就能转' % (('.' + ext), ('.' + target))


# ---------------------------------------------------------------- 实现：图片（Pillow）
def _save_image(im, out, target, quality=90):
    from PIL import Image
    fmt = dict(_PIL_FORMATS).get(target)
    if not fmt:
        raise RuntimeError('不支持的图片目标格式：%s' % target)
    frames = getattr(im, 'n_frames', 1) or 1
    params = {}
    if fmt in ('JPEG', 'WEBP', 'AVIF'):
        if im.mode in ('RGBA', 'LA', 'P'):
            im = im.convert('RGBA')
            bg = Image.new('RGB', im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        params['quality'] = max(1, min(100, int(quality)))
    elif fmt == 'PDF':
        if im.mode in ('RGBA', 'LA', 'P'):
            im = im.convert('RGB')
        params['resolution'] = 100.0
    elif fmt == 'GIF':
        if im.mode not in ('P', 'L'):
            im = im.convert('P', palette=Image.ADAPTIVE)
    elif fmt == 'ICO':
        im = im.convert('RGBA') if im.mode != 'RGBA' else im
        if max(im.size) > 256:
            im = im.copy()
            im.thumbnail((256, 256), Image.LANCZOS)
    if frames > 1 and fmt in ('GIF', 'TIFF', 'WEBP', 'PDF'):
        frames_imgs = []
        for i in range(1, min(frames, 200)):
            try:
                im.seek(i)
                frames_imgs.append(im.copy().convert(im.mode))
            except Exception:
                break
        try:
            im.seek(0)
        except Exception:
            pass
        if frames_imgs:
            params['save_all'] = True
            params['append_images'] = frames_imgs
    im.save(out, fmt, **params)


def convert_image(src, target, out, quality=90, on_progress=None):
    from PIL import Image, ImageOps
    with Image.open(src) as im:
        try:
            im = ImageOps.exif_transpose(im)
        except Exception:
            pass
        if on_progress:
            on_progress(30.0)
        _save_image(im, out, target, quality)
    if on_progress:
        on_progress(100.0)
    return out


# ---------------------------------------------------------------- 实现：音视频（ffmpeg）
def media_duration(path):
    """时长（秒），拿不到就 0（进度条退化成不确定态）"""
    probe = find_ffprobe()
    if not probe:
        return 0.0
    try:
        proc = subprocess.run([probe, '-v', 'error', '-show_entries', 'format=duration',
                               '-of', 'default=nw=1:nk=1', path],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW, timeout=30)
        return float((proc.stdout or b'').decode('utf-8', 'replace').strip() or 0)
    except Exception:
        return 0.0


def build_ffmpeg_args(src, target, out, scale_width=0):
    """拼 ffmpeg 参数（-progress pipe:1 把进度打成机器可读的行）"""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError('没找到 ffmpeg')
    args = [ff, '-y', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-progress', 'pipe:1', '-nostats', '-i', src]
    if target in _AUDIO_ARGS:
        args += ['-vn', '-map', '0:a:0?'] + _AUDIO_ARGS[target]
    elif target == 'gif':
        w = int(scale_width or 480)
        args += ['-an', '-vf', 'fps=12,scale=%d:-2:flags=lanczos' % w]
    else:
        args += _VIDEO_ARGS.get(target, ['-c:v', 'libx264', '-crf', '23', '-c:a', 'aac'])
    args.append(out)
    return args


def convert_media(src, target, out, on_progress=None, scale_width=0):
    args = build_ffmpeg_args(src, target, out, scale_width)
    duration = media_duration(src)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=CREATE_NO_WINDOW, encoding='utf-8', errors='replace',
                            bufsize=1)
    t0 = time.time()
    try:
        for line in proc.stdout:
            line = (line or '').strip()
            if not line or not on_progress:
                continue
            if line.startswith('out_time_ms=') or line.startswith('out_time_us='):
                try:
                    sec = float(line.split('=', 1)[1]) / 1e6
                except Exception:
                    continue
                if duration > 0:
                    on_progress(min(99.0, sec * 100.0 / duration))
            elif line.startswith('progress=end'):
                on_progress(100.0)
    except Exception:
        pass
    err = ''
    try:
        err = (proc.stderr.read() or '') if proc.stderr else ''
    except Exception:
        err = ''
    code = proc.wait()
    if code != 0:
        raise RuntimeError(explain_error(err) or ('ffmpeg 退出码 %s' % code))
    if on_progress:
        on_progress(100.0)
    return out


def explain_error(text):
    """把常见报错翻成人话"""
    low = str(text or '').lower()
    hints = (
        ('unknown encoder', '这个 ffmpeg 没编进该编码器（换个 build 或改目标格式）'),
        ('no such file', '文件不见了或被占用'),
        ('permission denied', '没权限写这个目录（换个输出位置试试）'),
        ('invalid data found', '文件损坏或格式跟后缀对不上'),
        ('does not contain any stream', '这个文件里没有能转的音视频流'),
        ('decoder not found', '缺解码器（这个 ffmpeg build 不支持该源格式）'),
        ('moov atom not found', 'MP4 没写完（下载没完成？）'),
        ('is not supported', '这个格式不被支持，建议装 Filee'),
        ('no space left', '磁盘满了'),
    )
    for key, hint in hints:
        if key in low:
            return hint
    return ''


# ---------------------------------------------------------------- 实现：PDF（pypdf）
def parse_pages(spec, total):
    """页码范围解析：'1-3,5,8-' → [0,1,2,4,7]（0 基、去重、按原顺序）

    写法跟打印对话框一个习惯：`1-3`、`5`、`8-`（到末页）、逗号/空格分隔。
    留空、或者一个能认的都没有 → 全部页。
    """
    total = int(total or 0)
    if total <= 0:
        return []
    spec = str(spec or '').strip()
    if not spec:
        return list(range(total))
    picked = []
    for chunk in re.split(r'[,，;；\s]+', spec):
        if not chunk:
            continue
        m = re.match(r'^(\d+)\s*[-~—]\s*(\d*)$', chunk)
        if m:
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else total
        else:
            try:
                a = b = int(chunk)
            except ValueError:
                continue
        if a > b:
            a, b = b, a
        for i in range(max(1, a), min(total, b) + 1):
            if (i - 1) not in picked:
                picked.append(i - 1)
    picked.sort()
    return picked or list(range(total))


def pdf_pages_suffix(spec):
    """把页码范围变成文件名后缀：'1-3,5' → '_第1-3、5页'（同名同格式的操作用）"""
    s = str(spec or '').strip()
    s = re.sub(r'[,，;；\s]+', '、', s)
    s = re.sub(r'[^0-9\-、]+', '', s).strip('、')
    s = re.sub(r'-{2,}', '-', s)
    return '_第%s页' % s if s else '_取页'


def _pdf_page_indexes(reader, pages):
    return parse_pages(pages, len(reader.pages))


def convert_pdf_text(src, out, on_progress=None, pages=''):
    from pypdf import PdfReader
    reader = PdfReader(src)
    idx = _pdf_page_indexes(reader, pages)
    total = max(1, len(idx))
    chunks = []
    for n, i in enumerate(idx):
        try:
            chunks.append(reader.pages[i].extract_text() or '')
        except Exception:
            chunks.append('')
        if on_progress:
            on_progress(min(99.0, (n + 1) * 100.0 / total))
    text = '\n\n'.join(c.strip('\n') for c in chunks)
    if not text.strip():
        text = '（这份 PDF 里没有可提取的文字 —— 可能是扫描件，需要 OCR）'
    with open(out, 'w', encoding='utf-8') as f:
        f.write(text)
    if on_progress:
        on_progress(100.0)
    return out


def pdf_subset(src, out, pages='', on_progress=None):
    """只挑指定页，写成一个新 PDF（`PAGES_PDF`：一进一出，格式不变）"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(src)
    idx = _pdf_page_indexes(reader, pages)
    writer = PdfWriter()
    total = max(1, len(idx))
    for n, i in enumerate(idx):
        writer.add_page(reader.pages[i])
        if on_progress:
            on_progress(min(99.0, (n + 1) * 100.0 / total))
    with open(out, 'wb') as f:
        writer.write(f)
    if on_progress:
        on_progress(100.0)
    return out


def split_pdf(src, outdir, pages='', stem='', overwrite=False, on_progress=None):
    """每页拆成一个 PDF（`SPLIT_PDF`：一进多出），返回产物列表"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(src)
    idx = _pdf_page_indexes(reader, pages)
    stem = stem or _stem_of(src)
    outs = []
    total = max(1, len(idx))
    for n, i in enumerate(idx):
        name = '%s_第%d页.pdf' % (stem, i + 1)
        out = os.path.join(outdir, name)
        if not overwrite:
            out = unique_path(out)
        writer = PdfWriter()
        writer.add_page(reader.pages[i])
        with open(out, 'wb') as f:
            writer.write(f)
        outs.append(out)
        if on_progress:
            on_progress(min(99.0, (n + 1) * 100.0 / total))
    if on_progress:
        on_progress(100.0)
    return outs


def compress_pdf(src, out, pages='', on_progress=None):
    """瘦身：压缩每页的内容流 + 去掉重复对象/孤儿对象（不重采样图片，画质不动）"""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(src)
    idx = _pdf_page_indexes(reader, pages)
    writer = PdfWriter()
    total = max(1, len(idx))
    for n, i in enumerate(idx):
        page = reader.pages[i]
        try:
            page.compress_content_streams()      # 内容流 Flate 压缩
        except Exception:
            pass
        writer.add_page(page)
        if on_progress:
            on_progress(min(99.0, (n + 1) * 100.0 / total))
    # pypdf 6.x 的新名字是 remove_duplicates / remove_unreferenced，老的会发弃用警告
    for kwargs in ({'remove_duplicates': True, 'remove_unreferenced': True},
                   {},
                   {'remove_identicals': True, 'remove_orphans': True}):
        try:
            writer.compress_identical_objects(**kwargs)
            break
        except Exception:
            continue
    with open(out, 'wb') as f:
        writer.write(f)
    if on_progress:
        on_progress(100.0)
    return out


def merge_pdfs(sources, out, on_progress=None, pages=''):
    from pypdf import PdfWriter, PdfReader
    writer = PdfWriter()
    total = max(1, len(sources))
    for i, src in enumerate(sources):
        reader = PdfReader(src)
        for j in _pdf_page_indexes(reader, pages):
            writer.add_page(reader.pages[j])
        if on_progress:
            on_progress(min(99.0, (i + 1) * 100.0 / total))
    with open(out, 'wb') as f:
        writer.write(f)
    if on_progress:
        on_progress(100.0)
    return out


# ---------------------------------------------------------------- 实现：Office（标准库读 OOXML）
def _zip_read_xml(zf, name):
    return ET.fromstring(zf.read(name))


def docx_text(path):
    with zipfile.ZipFile(path) as z:
        root = _zip_read_xml(z, 'word/document.xml')
    paras = []
    for p in root.iter(_NS_W + 'p'):
        buf = []
        for node in p.iter():
            if node.tag == _NS_W + 't':
                buf.append(node.text or '')
            elif node.tag == _NS_W + 'tab':
                buf.append('\t')
            elif node.tag in (_NS_W + 'br', _NS_W + 'cr'):
                buf.append('\n')
        paras.append(''.join(buf))
    return '\n'.join(paras)


def _xlsx_sheet_paths(z):
    """workbook.xml 的 <sheet r:id> → 真正的 worksheets/sheetN.xml 路径（别硬编码 sheet1）"""
    try:
        wb = _zip_read_xml(z, 'xl/workbook.xml')
        rels = _zip_read_xml(z, 'xl/_rels/workbook.xml.rels')
    except Exception:
        return []
    rel_map = {}
    for rel in rels.iter(_NS_PKG_REL + 'Relationship'):
        rel_map[rel.get('Id')] = rel.get('Target') or ''
    out = []
    for sheet in wb.iter(_NS_S + 'sheet'):
        rid = sheet.get(_NS_R + 'id')
        target = rel_map.get(rid, '')
        if not target:
            continue
        if target.startswith('/'):
            path = target.lstrip('/')
        else:
            path = os.path.normpath(os.path.join('xl', target)).replace('\\', '/')
        out.append((sheet.get('name') or '', path))
    return out


def xlsx_rows(path, sheet_index=0):
    """读一个工作表成二维列表（字符串/数字原样，日期不解析 —— 够用就行）"""
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = _zip_read_xml(z, 'xl/sharedStrings.xml')
            for si in root.iter(_NS_S + 'si'):
                shared.append(''.join(t.text or '' for t in si.iter(_NS_S + 't')))
        sheets = _xlsx_sheet_paths(z)
        if not sheets:
            names = [n for n in z.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml$', n)]
            sheets = [('', n) for n in sorted(names)]
        if not sheets:
            return []
        idx = max(0, min(int(sheet_index or 0), len(sheets) - 1))
        root = _zip_read_xml(z, sheets[idx][1])
    rows = []
    for row in root.iter(_NS_S + 'row'):
        cells = {}
        for c in row.iter(_NS_S + 'c'):
            ref = c.get('r') or ''
            col = _col_index(ref)
            ctype = c.get('t') or 'n'
            val = ''
            if ctype == 's':
                v = c.find(_NS_S + 'v')
                try:
                    val = shared[int((v.text or '0').strip())]
                except Exception:
                    val = ''
            elif ctype == 'inlineStr':
                val = ''.join(t.text or '' for t in c.iter(_NS_S + 't'))
            else:
                v = c.find(_NS_S + 'v')
                val = (v.text or '') if v is not None else ''
            cells[col] = val
        if not cells:
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, '') for i in range(width)])
    return rows


def _col_index(ref):
    letters = ''.join(ch for ch in str(ref) if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return max(0, idx - 1)


def pptx_text(path):
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.match(r'ppt/slides/slide\d+\.xml$', n)]
        names.sort(key=lambda n: int(re.search(r'slide(\d+)\.xml$', n).group(1)))
        blocks = []
        for name in names:
            root = _zip_read_xml(z, name)
            lines = []
            for p in root.iter(_NS_A + 'p'):
                text = ''.join(t.text or '' for t in p.iter(_NS_A + 't'))
                if text.strip():
                    lines.append(text)
            if lines:
                no = int(re.search(r'slide(\d+)\.xml$', name).group(1))
                blocks.append('【第 %d 页】\n%s' % (no, '\n'.join(lines)))
    return '\n\n'.join(blocks)


def _write_text(out, text):
    with open(out, 'w', encoding='utf-8', newline='') as f:
        f.write(text or '')


def convert_office(src, target, out, on_progress=None):
    ext = ext_of(src)
    if ext == 'docx' and target == 'txt':
        _write_text(out, docx_text(src))
    elif ext == 'pptx' and target == 'txt':
        _write_text(out, pptx_text(src))
    elif ext == 'xlsx' and target in ('csv', 'tsv', 'json'):
        rows = xlsx_rows(src)
        if target == 'json':
            _write_json_rows(out, rows)
        else:
            _write_delim(out, rows, '\t' if target == 'tsv' else ',')
    else:
        raise RuntimeError('%s → %s 内置引擎做不了' % (ext, target))
    if on_progress:
        on_progress(100.0)
    return out


# ---------------------------------------------------------------- 实现：表格（CSV/TSV/JSON/XLSX）
def _read_table(src):
    """把 CSV/TSV/JSON 读成二维列表（第一行当表头由调用方决定）"""
    ext = ext_of(src)
    if ext == 'json':
        with open(src, encoding='utf-8-sig') as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get('data') or data.get('rows') or [data]
        rows = []
        if data and isinstance(data[0], dict):
            keys = []
            for item in data:
                for k in item.keys():
                    if k not in keys:
                        keys.append(k)
            rows.append(keys)
            for item in data:
                rows.append([_json_cell(item.get(k)) for k in keys])
        elif data and isinstance(data[0], (list, tuple)):
            rows = [[_json_cell(c) for c in row] for row in data]
        else:
            rows = [['value']] + [[_json_cell(v)] for v in (data or [])]
        return rows
    text, _enc = _read_text_any(src)
    delim = '\t' if ext == 'tsv' else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|').delimiter
        except Exception:
            delim = ','
    return [row for row in csv.reader(text.splitlines(), delimiter=delim)]


def _json_cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    return str(value)


def _write_json_rows(out, rows):
    if not rows:
        _write_text(out, '[]')
        return
    head = rows[0]
    items = []
    for row in rows[1:]:
        item = {}
        for i, key in enumerate(head):
            item[str(key)] = row[i] if i < len(row) else ''
        items.append(item)
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _write_delim(out, rows, delim):
    with open(out, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter=delim, lineterminator='\r\n')
        for row in rows:
            writer.writerow(['' if c is None else c for c in row])


def _xml_escape(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def _col_letter(idx):
    letters = ''
    idx += 1
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def write_xlsx(rows, out, sheet_name='Sheet1'):
    """手写一个最小 xlsx（inlineStr 写字符串，不搞 sharedStrings）"""
    sheet_rows = []
    for r, row in enumerate(rows):
        cells = []
        for c, value in enumerate(row):
            ref = '%s%d' % (_col_letter(c), r + 1)
            if value is None or value == '':
                continue
            text = str(value)
            try:
                num = float(text)
                is_num = not text.strip().lower() in ('nan', 'inf', '-inf')
            except Exception:
                is_num = False
            if is_num:
                cells.append('<c r="%s"><v>%s</v></c>' % (ref, text))
            else:
                cells.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                             % (ref, _xml_escape(text)))
        sheet_rows.append('<row r="%d">%s</row>' % (r + 1, ''.join(cells)))
    dimension = 'A1:%s%d' % (_col_letter(max(0, max((len(r) for r in rows), default=1) - 1)),
                             max(1, len(rows)))
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<dimension ref="%s"/><sheetData>%s</sheetData></worksheet>' % (dimension, ''.join(sheet_rows))
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>'
        % _xml_escape(sheet_name or 'Sheet1')[:31]
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.worksheet+xml"/></Types>'
    )
    tmp = out + '.part'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', workbook_xml)
        z.writestr('xl/_rels/workbook.xml.rels', rels_xml)
        z.writestr('xl/worksheets/sheet1.xml', sheet_xml)
    os.replace(tmp, out)
    return out


def convert_table(src, target, out, on_progress=None):
    rows = _read_table(src)
    if target == 'xlsx':
        write_xlsx(rows, out)
    elif target == 'json':
        _write_json_rows(out, rows)
    elif target in ('csv', 'tsv'):
        _write_delim(out, rows, '\t' if target == 'tsv' else ',')
    else:
        raise RuntimeError('表格转 %s 内置引擎做不了' % target)
    if on_progress:
        on_progress(100.0)
    return out


# ---------------------------------------------------------------- 实现：文本
def _read_text_any(path):
    """猜编码读文本：BOM → utf-8 → gb18030 → utf-16 → 硬解（中文乱码最常见的三种都覆盖）"""
    with open(path, 'rb') as f:
        raw = f.read()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'big5', 'utf-16'):
        try:
            return raw.decode(enc), enc
        except Exception:
            continue
    return raw.decode('utf-8', 'replace'), 'utf-8(replace)'


def convert_text(src, out, on_progress=None):
    text, enc = _read_text_any(src)
    _write_text(out, text)
    if on_progress:
        on_progress(100.0)
    return out


# ---------------------------------------------------------------- 实现：压缩包
def _extract_archive(src, dest):
    ext = ext_of(src)
    if ext == 'zip':
        with zipfile.ZipFile(src) as z:
            z.extractall(dest)
    elif ext == '7z':
        import py7zr
        with py7zr.SevenZipFile(src, 'r') as z:
            z.extractall(dest)
    else:
        with tarfile.open(src) as t:
            t.extractall(dest)


def _pack_archive(src_dir, out, target):
    items = []
    for root, dirs, files in os.walk(src_dir):
        for name in files:
            full = os.path.join(root, name)
            items.append((full, os.path.relpath(full, src_dir)))
    if target == 'zip':
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for full, rel in items:
                z.write(full, rel)
    elif target == '7z':
        import py7zr
        with py7zr.SevenZipFile(out, 'w') as z:
            for full, rel in items:
                z.write(full, rel)
    elif target == 'tgz':
        with tarfile.open(out, 'w:gz') as t:
            for full, rel in items:
                t.add(full, rel)
    elif target == 'tar':
        with tarfile.open(out, 'w') as t:
            for full, rel in items:
                t.add(full, rel)
    else:
        raise RuntimeError('压缩包转 %s 内置引擎做不了' % target)


def convert_archive(src, target, out, on_progress=None):
    tmp = tempfile.mkdtemp(prefix='petcv_')
    try:
        _extract_archive(src, tmp)
        if on_progress:
            on_progress(50.0)
        _pack_archive(tmp, out, target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if on_progress:
        on_progress(100.0)
    return out


# ---------------------------------------------------------------- Filee CLI
# Filee 的 `convert` 本身没有页码/拆分/压缩**参数**，但它的 **preset** 里有这三个操作
# （`filee presets` 里能查到 merge-pdf / pdf-split / compress-pdf，用 `-p <id>` 调用）。
# 所以：没填页码时先让 Filee 的预设干（它的压缩会重采样图片，比 pypdf 狠得多），
# 填了页码、或者它这一版没这几个预设，再退回 pypdf。
PDF_PRESETS = {MERGE_PDF: 'merge-pdf', SPLIT_PDF: 'pdf-split', COMPRESS_PDF: 'compress-pdf'}
_preset_cache = None
_preset_lock = threading.Lock()


def parse_presets_output(text):
    """解析 `filee presets` 那张表：`ID    NAME    TARGET`（列之间是两个以上空格）"""
    out = {}
    for line in str(text or '').splitlines():
        line = line.rstrip()
        if not line.strip() or line.strip().upper().startswith('ID '):
            continue
        parts = re.split(r'\s{2,}', line.strip())
        if len(parts) >= 3 and parts[0] and parts[2]:
            out[parts[0]] = parts[1]
    return out


def filee_presets(exe='', fetch=True):
    """Filee 装了哪些预设（起一次进程问一次，之后记在内存里）"""
    global _preset_cache
    exe = exe or find_filee()
    if not exe:
        return {}
    with _preset_lock:
        if _preset_cache is not None:
            return dict(_preset_cache)
    if not fetch:
        return {}
    try:
        proc = subprocess.run([exe, 'presets'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=CREATE_NO_WINDOW, timeout=30)
        text = (proc.stdout or b'').decode('utf-8', 'replace')
    except Exception:
        return {}
    got = parse_presets_output(text)
    with _preset_lock:
        _preset_cache = got
    return dict(got)


def filee_pdf_preset(target):
    """这个 PDF 操作在 Filee 里对应的预设 id（没有 → 空串，交给 pypdf）"""
    pid = PDF_PRESETS.get(target) or ''
    if not pid:
        return ''
    return pid if pid in filee_presets() else ''


def build_filee_args(exe, sources, target, outdir='', overwrite=False, quality=0, keep_dates=False):
    args = [exe, 'convert']
    if target and target not in PDF_OPS:
        args += ['--to', str(target)]
    if outdir:
        args += ['--output', outdir]
    if overwrite:
        args += ['--overwrite']
    if quality:
        args += ['--quality', str(int(quality))]
    if keep_dates:
        args += ['--keep-dates']
    args += ['--json']
    args += list(sources)
    return args


def build_filee_preset_args(exe, sources, preset, outdir='', overwrite=False, keep_dates=False):
    args = [exe, 'convert', '--preset', str(preset)]
    if outdir:
        args += ['--output', outdir]
    if overwrite:
        args += ['--overwrite']
    if keep_dates:
        args += ['--keep-dates']
    args += ['--json']
    args += list(sources)
    return args


def _spawn_filee(args, timeout=0):
    """跑一次 filee-cli 并把 --json 解析成统一结构，返回 (results, error)"""
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=CREATE_NO_WINDOW, timeout=timeout or None)
    except subprocess.TimeoutExpired:
        return [], 'Filee 跑超时了'
    except Exception as exc:
        return [], '启动 Filee 失败：%s' % exc
    out = (proc.stdout or b'').decode('utf-8', 'replace')
    err = (proc.stderr or b'').decode('utf-8', 'replace').strip()
    data = None
    start = out.find('{')
    if start >= 0:
        try:
            data = json.loads(out[start:])
        except Exception:
            data = None
    results = []
    if isinstance(data, dict):
        for item in data.get('files') or []:
            state = str(item.get('state') or '').lower()
            results.append({
                'source': item.get('source') or '',
                'ok': state in ('done', 'skipped'),
                'skipped': state == 'skipped',
                'outputs': list(item.get('outputs') or []),
                'error': item.get('error') or '',
                'notice': item.get('notice') or '',
            })
    if not results:
        # 没 JSON（参数错、没东西可转…）→ 把 stderr 原文当原因，但翻成人话
        code = proc.returncode
        if code == 2:
            return [], 'Filee 参数有问题：%s' % (err or '未知')[:120]
        if code == 3:
            return [], 'Filee 说没东西可转（格式不对或文件不认）'
        if err:
            return [], err[:200]
    return results, ''


def run_filee(sources, target, outdir='', overwrite=False, quality=0, timeout=0, keep_dates=False):
    """跑一次 filee-cli，返回 (results, error)

    results: [{'source','ok','outputs','error'}]；error 是整体性的错误（参数错/没东西可转）
    """
    exe = find_filee()
    if not exe:
        return [], '没找到 Filee'
    return _spawn_filee(build_filee_args(exe, sources, target, outdir, overwrite,
                                         quality, keep_dates), timeout)


def run_filee_preset(sources, preset, outdir='', overwrite=False, timeout=0, keep_dates=False):
    """用 Filee 的预设跑一次（PDF 的合并/拆分/压缩就走这里）"""
    exe = find_filee()
    if not exe:
        return [], '没找到 Filee'
    return _spawn_filee(build_filee_preset_args(exe, sources, preset, outdir,
                                                overwrite, keep_dates), timeout)


def _filee_one(src, target, outdir, overwrite, quality=0, keep_dates=False):
    """单文件走 Filee，返回 {'ok','out','outputs','error'}"""
    results, err = run_filee([src], target, outdir, overwrite, quality, keep_dates=keep_dates)
    if err and not results:
        return {'ok': False, 'out': '', 'error': err}
    for item in results:
        if os.path.abspath(item.get('source') or '') == os.path.abspath(src) or not item.get('source'):
            if item['ok']:
                outs = list(item.get('outputs') or [])
                return {'ok': True, 'out': outs[0] if outs else '', 'outputs': outs, 'error': ''}
            return {'ok': False, 'out': '', 'error': item.get('error') or 'Filee 转换失败'}
    return {'ok': False, 'out': '', 'error': 'Filee 没有回报结果'}


def _filee_one_preset(src, preset, outdir, overwrite, keep_dates=False):
    """单文件走 Filee 预设，返回 {'ok','out','outputs','error'}"""
    results, err = run_filee_preset([src], preset, outdir, overwrite, keep_dates=keep_dates)
    if err and not results:
        return {'ok': False, 'out': '', 'error': err}
    for item in results:
        if os.path.abspath(item.get('source') or '') == os.path.abspath(src) or not item.get('source'):
            if item['ok']:
                outs = list(item.get('outputs') or [])
                return {'ok': True, 'out': outs[0] if outs else '', 'outputs': outs, 'error': ''}
            return {'ok': False, 'out': '', 'error': item.get('error') or 'Filee 预设没成'}
    return {'ok': False, 'out': '', 'error': 'Filee 没有回报结果'}


def _compress_note(src, out):
    """压缩前后体积对比（压不动也直说，别吹）"""
    try:
        before, after = os.path.getsize(src), os.path.getsize(out)
    except Exception:
        return ''
    if not before:
        return ''
    if after < before:
        return '原 %s → 现在 %s（小了 %d%%）' % (fmt_size(before), fmt_size(after),
                                              int((before - after) * 100.0 / before))
    return '这份已经压过/主要是图片，瘦不下来（%s → %s）' % (fmt_size(before), fmt_size(after))


def copy_dates(src, out):
    """把原文件的创建/修改时间盖到产物上（Filee `--keep-dates` 的内置实现）"""
    try:
        st = os.stat(src)
        os.utime(out, (st.st_atime, st.st_mtime))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 单文件入口
def convert_file(src, target, outdir=None, overwrite=False, quality=90, on_progress=None,
                 allow_filee=True, _engine='', keep_dates=False, pages=''):
    """转一个文件，返回 {'ok','out','engine','error'}（拆分时多一个 'outs' 列表）"""
    src = os.path.abspath(str(src or ''))
    if not os.path.isfile(src):
        return {'ok': False, 'out': '', 'engine': '', 'error': '文件不存在：%s' % src}
    if target == MERGE_PDF:
        return {'ok': False, 'out': '', 'engine': '',
                'error': '合并 PDF 要一次给多个文件（面板里选中多个 PDF 再点转换）'}
    outdir = outdir or os.path.dirname(src)
    if str(target).lower() == ext_of(src) and target not in PDF_OPS:
        # 已经是这个格式了：不重编码，原样当成功（批量统一转格式时会出现）
        return {'ok': True, 'out': src, 'engine': 'skip', 'skipped': True, 'error': ''}
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception as exc:
        return {'ok': False, 'out': '', 'engine': '', 'error': '建不了输出目录：%s' % exc}
    # PDF 的拆分/压缩/取页：Filee 有对应预设、又没填页码 → 先让它干
    # （实测 Filee 的 compress-pdf 会把图片重采样，10.2 KB → 6.9 KB / -33%；
    #   同样的文件 pypdf 只压内容流 + 去重，只小 2%。填了页码就 pypdf 上，因为预设不接受页码）
    if target in PDF_OPS and target != MERGE_PDF and allow_filee \
            and not str(pages or '').strip():
        pid = filee_pdf_preset(target)
        if pid:
            one = _filee_one_preset(src, pid, outdir, overwrite, keep_dates)
            if one['ok']:
                outs = list(one.get('outputs') or [])
                if on_progress:
                    on_progress(100.0)
                if target == SPLIT_PDF:
                    return {'ok': True, 'out': outs[0] if outs else '', 'outs': outs,
                            'engine': 'filee', 'error': ''}
                return {'ok': True, 'out': one['out'], 'engine': 'filee',
                        'note': _compress_note(src, one['out']) if target == COMPRESS_PDF else '',
                        'error': ''}
            if _engine:
                return {'ok': False, 'out': '', 'engine': 'filee', 'error': one.get('error') or ''}
    engine = _engine or route(src, target, prefer_filee=allow_filee)
    if not engine:
        return {'ok': False, 'out': '', 'engine': '', 'error': no_engine_hint(src, target)}
    ext = ext_of(src)
    try:
        if engine == 'filee':
            res = _filee_one(src, target, outdir, overwrite, quality, keep_dates)
            if res['ok']:
                if on_progress:
                    on_progress(100.0)
                return {'ok': True, 'out': res['out'], 'engine': 'filee', 'error': ''}
            if not allow_filee:
                return {'ok': False, 'out': '', 'engine': 'filee', 'error': res['error']}
            # Filee 这一单没成 → 内置引擎接着干（能兜就兜）
            fallback = builtin_engine_for(ext, target)
            if not fallback:
                return {'ok': False, 'out': '', 'engine': 'filee', 'error': res['error']}
            engine = fallback
        if target == SPLIT_PDF:                       # 一进多出，单独一条路
            outs = split_pdf(src, outdir, pages, overwrite=overwrite, on_progress=on_progress)
            if keep_dates:
                for o in outs:
                    copy_dates(src, o)
            return {'ok': True, 'out': outs[0] if outs else '', 'outs': outs,
                    'engine': engine, 'error': ''}
        suffix = ''
        if target == PAGES_PDF:
            suffix = pdf_pages_suffix(pages)
        elif target == COMPRESS_PDF:
            suffix = '_压缩'
        out = output_path(src, target, outdir, overwrite, suffix)
        if engine == 'pillow':
            convert_image(src, target, out, quality, on_progress)
        elif engine == 'ffmpeg':
            convert_media(src, target, out, on_progress)
        elif engine == 'pypdf':
            if target == PAGES_PDF:
                pdf_subset(src, out, pages, on_progress)
            elif target == COMPRESS_PDF:
                compress_pdf(src, out, pages, on_progress)
            else:
                convert_pdf_text(src, out, on_progress, pages)
        elif engine == 'stdlib':
            kind = kind_of(src)
            if kind == 'office':
                convert_office(src, target, out, on_progress)
            elif kind == 'table':
                convert_table(src, target, out, on_progress)
            elif kind == 'archive':
                convert_archive(src, target, out, on_progress)
            else:
                convert_text(src, out, on_progress)
        else:
            return {'ok': False, 'out': '', 'engine': engine, 'error': '未知引擎：%s' % engine}
        if keep_dates:
            copy_dates(src, out)
        res = {'ok': True, 'out': out, 'engine': engine, 'error': ''}
        if target == COMPRESS_PDF:
            note = _compress_note(src, out)
            if note:
                res['note'] = note
        return res
    except Exception as exc:
        return {'ok': False, 'out': '', 'engine': engine,
                'error': explain_error(exc) or str(exc) or '转换失败'}


# ---------------------------------------------------------------- 批量任务
def short_target(target):
    """任务摘要里用的短名（下拉里那串「PDF（每页拆成一个）」太长）"""
    return {MERGE_PDF: '合并 PDF', SPLIT_PDF: '拆分 PDF',
            COMPRESS_PDF: '压缩 PDF', PAGES_PDF: 'PDF 取页'}.get(target, str(target))


class ConvertJob(object):
    """一批文件转成同一个格式：后台线程跑，进度/结果回吐给界面

    能全走 Filee 的话就拼成**一次**进程调用（少开进程、一个 --json 拿全部结果）；
    混着内置引擎的活儿就一个一个来，进度才能按文件往上走。
    """

    def __init__(self, sources, target, outdir=None, overwrite=False, quality=90,
                 on_event=None, allow_filee=True, keep_dates=False, pages=''):
        if isinstance(sources, str):     # 防呆：单个路径当一串字符会变成几十个"文件"
            sources = [sources]
        self.sources = [os.path.abspath(s) for s in (sources or []) if s]
        self.target = target
        self.outdir = outdir or ''          # 空 = 各自跟原文件放一起
        self.overwrite = bool(overwrite)
        self.quality = quality
        self.keep_dates = bool(keep_dates)
        self.pages = str(pages or '')       # PDF 页码范围（'1-3,5'），只有 PDF 相关的活儿用它
        self.on_event = on_event or (lambda job, kind, data: None)
        self.state = 'waiting'
        self.percent = 0.0
        self.done_count = 0
        self.fail_count = 0
        self.results = []                   # [{'source','ok','out','engine','error'}]
        self.error = ''
        self.allow_filee = allow_filee
        self._thread = None
        self._cancel = False

    # ---------- 对外 ----------
    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name='pet-convert', daemon=True)
        self._thread.start()
        return self

    def cancel(self):
        self._cancel = True
        return True

    def active(self):
        return self.state in ('waiting', 'running')

    def summary(self):
        name = '%s → %s' % (os.path.basename(self.sources[0]) if self.sources else '',
                            short_target(self.target))
        if len(self.sources) > 1:
            name = '%s 等 %d 个 → %s' % (os.path.basename(self.sources[0]), len(self.sources),
                                         short_target(self.target))
        if self.state == 'done':
            if self.fail_count:
                return '⚠ %s（%d 成 %d 败）' % (name, self.done_count, self.fail_count)
            return '✔ %s' % name
        if self.state == 'canceled':
            return '✕ 已取消 · %s' % name
        if self.state == 'error':
            return '✕ %s · %s' % (name, self.error[:50])
        return '⇄ %d%% · %s' % (int(self.percent), name)

    # ---------- 内部 ----------
    def _emit(self, kind, data=None):
        try:
            self.on_event(self, kind, data or {})
        except Exception:
            pass

    def _set_state(self, state):
        self.state = state
        self._emit('state', {'state': state})

    def _outdir_for(self, src):
        return self.outdir or os.path.dirname(src)

    def _convert_one(self, src, on_progress=None, allow_filee=False):
        """单个文件走内置引擎（带上面板里的质量/时间戳/页码设置）

        PDF 四操作例外：`allow_filee` 也放行 —— 这里的「走 Filee」指的是它的
        pdf-split / compress-pdf **预设**（convert_file 里先试预设，再退 pypdf），
        不会把 `pdf+split` 这种伪格式喂给 `--to`。
        """
        return convert_file(src, self.target, self._outdir_for(src), self.overwrite,
                            self.quality, on_progress,
                            allow_filee=allow_filee or self.target in PDF_OPS,
                            keep_dates=self.keep_dates, pages=self.pages)

    def _run(self):
        total = max(1, len(self.sources))
        self._set_state('running')
        # 已经是目标格式的（批量统一转格式时很常见：一堆图里本来就有 png）直接跳过，别重编码一遍
        todo, skips = [], []
        for s in self.sources:
            if str(self.target).lower() == ext_of(s) and self.target not in PDF_OPS:
                skips.append(s)
            else:
                todo.append(s)
        for s in skips:
            self.done_count += 1
            self.results.append({'source': s, 'ok': True, 'out': s, 'engine': 'skip',
                                 'skipped': True, 'error': ''})
            self._emit('file_done', {'source': s, 'ok': True, 'out': s, 'engine': 'skip',
                                     'skipped': True, 'error': '', 'index': -1,
                                     'total': total, 'percent': self.percent})
        if not todo:
            self._finish()
            return
        self.sources = todo
        total = max(1, len(todo))
        # ① 能整批交给 Filee 吗（同一个目标格式、Filee 认得每个源格式）
        if self.allow_filee and find_filee() and self.target not in PDF_OPS:
            srcs = self.sources
            if srcs and all(self.target in filee_targets(ext_of(s)) for s in srcs):
                if self.outdir:
                    self._run_filee_batch(srcs, self.outdir)
                    return
                # 输出「跟各自原文件放一起」→ 按目录分组，一组一次调用
                groups = {}
                for s in srcs:
                    groups.setdefault(os.path.dirname(s), []).append(s)
                if len(groups) == 1:
                    self._run_filee_batch(list(groups.values())[0])
                    return
                self._run_filee_groups(groups)
                return
        # ② 合并 PDF 是「多进一出」，单独一条路
        if self.target == MERGE_PDF:
            self._run_merge()
            return
        # ③ 内置引擎逐文件
        for i, src in enumerate(self.sources):
            if self._cancel:
                self._set_state('canceled')
                return
            self._emit('file_start', {'index': i, 'total': total, 'source': src})
            frac = [0.0]

            def on_prog(pct, _i=i, _frac=frac):
                _frac[0] = float(pct or 0)
                self.percent = min(99.0, (_i + _frac[0] / 100.0) * 100.0 / total)
                self._emit('progress', {'percent': round(self.percent, 1),
                                        'index': _i, 'total': total})

            res = self._convert_one(src, on_prog)
            self._record(src, res, total, i)
        self._finish()

    def _record(self, src, res, total, index):
        res = dict(res or {})
        res.setdefault('source', src)
        self.results.append(res)
        if res.get('ok'):
            self.done_count += 1
        else:
            self.fail_count += 1
        self.percent = min(100.0, (index + 1) * 100.0 / max(1, total))
        self._emit('file_done', dict(res, index=index, total=total,
                                     percent=round(self.percent, 1)))
        self._emit('progress', {'percent': round(self.percent, 1), 'index': index, 'total': total})

    def _finish(self):
        if self._cancel:
            self._set_state('canceled')
            return
        self.percent = 100.0
        if self.fail_count and not self.done_count:
            self._set_state('error')
            self.error = (self.results[0].get('error') if self.results else '转换失败') or '转换失败'
            self._emit('error', {'error': self.error})
            return
        self._set_state('done')
        outs = []
        for r in self.results:
            if r.get('skipped'):
                continue
            outs.extend(r.get('outs') or ([r['out']] if r.get('out') else []))
        self._emit('done', {'converted': self.done_count, 'failed': self.fail_count,
                            'skipped': sum(1 for r in self.results if r.get('skipped')),
                            'outputs': outs})

    def _run_filee_batch(self, srcs, outdir=''):
        if isinstance(srcs, str):        # 防呆：别让一个路径被当成"一串单字符文件"
            srcs = [srcs]
        results, err = run_filee(srcs, self.target, outdir or '', self.overwrite,
                                 self.quality, keep_dates=self.keep_dates)
        if err and not results:
            # 整批挂了 → 退回逐文件内置引擎（有的话）
            self._emit('notice', {'text': 'Filee 没成（%s），改用内置引擎' % err})
            total = len(srcs)
            for i, src in enumerate(srcs):
                if self._cancel:
                    self._set_state('canceled')
                    return
                self._emit('file_start', {'index': i, 'total': total, 'source': src})
                res = self._convert_one(src)
                self._record(src, res, total, i)
            self._finish()
            return
        total = max(1, len(srcs))
        by_src = {os.path.abspath(r.get('source') or ''): r for r in results}
        for i, src in enumerate(srcs):
            if self._cancel:
                self._set_state('canceled')
                return
            item = by_src.get(os.path.abspath(src))
            if item is None:
                res = {'ok': False, 'out': '', 'engine': 'filee', 'error': 'Filee 没回报这个文件'}
            elif item['ok']:
                res = {'ok': True, 'out': (item.get('outputs') or [''])[0],
                       'engine': 'filee', 'error': ''}
            else:
                # 单文件失败再试一次内置引擎
                res = self._convert_one(src)
                if not res.get('ok'):
                    res['error'] = item.get('error') or res.get('error') or 'Filee 转换失败'
            self._record(src, res, total, i)
        if err:
            self._emit('notice', {'text': err})
        self._finish()

    def _run_filee_groups(self, groups):
        total = max(1, len(self.sources))
        index = 0
        for outdir, srcs in groups.items():
            if self._cancel:
                self._set_state('canceled')
                return
            results, err = run_filee(srcs, self.target, outdir, self.overwrite,
                                     self.quality, keep_dates=self.keep_dates)
            by_src = {os.path.abspath(r.get('source') or ''): r for r in results}
            for src in srcs:
                if self._cancel:
                    self._set_state('canceled')
                    return
                item = by_src.get(os.path.abspath(src))
                if item and item['ok']:
                    res = {'ok': True, 'out': (item.get('outputs') or [''])[0],
                           'engine': 'filee', 'error': ''}
                else:
                    res = self._convert_one(src)
                    if not res.get('ok'):
                        res['error'] = (item or {}).get('error') or res.get('error') or '转换失败'
                self._record(src, res, total, index)
                index += 1
            if err:
                self._emit('notice', {'text': err})
        self._finish()

    def _run_merge(self):
        pdfs = [s for s in self.sources if ext_of(s) == 'pdf']
        if len(pdfs) < 2:
            self.error = '合并 PDF 至少要两个 PDF 文件'
            self._set_state('error')
            self._emit('error', {'error': self.error})
            return
        src = pdfs[0]
        outdir = self._outdir_for(src)
        if self.pages.strip():
            self._emit('notice', {'text': '页码范围 %s 对每个 PDF 分别生效' % self.pages.strip()})
        elif self.allow_filee and filee_pdf_preset(MERGE_PDF):
            # 没填页码、Filee 又有 merge-pdf 预设 → 让它合（比 pypdf 稳，书签/表单也能带过去）
            results, err = run_filee_preset(pdfs, filee_pdf_preset(MERGE_PDF), outdir,
                                            self.overwrite, keep_dates=self.keep_dates)
            outs = []
            for item in results:
                outs.extend(item.get('outputs') or [])
            outs = [p for p in outs if os.path.isfile(p)]
            if not err and outs:
                self.done_count = len(pdfs)
                self.results = [{'source': s, 'ok': True, 'out': outs[0], 'engine': 'filee',
                                 'error': ''} for s in pdfs]
                self._emit('file_done', {'source': '（%d 个 PDF 合并）' % len(pdfs), 'ok': True,
                                         'out': outs[0], 'engine': 'filee', 'error': '',
                                         'index': 0, 'total': 1, 'percent': 100.0})
                self._finish()
                return
            self._emit('notice', {'text': 'Filee 的合并预设没成（%s），改用 pypdf' % (err or '没产物')})
        try:
            os.makedirs(outdir, exist_ok=True)
            name = '%s_合并.pdf' % _stem_of(src)
            out = os.path.join(outdir, name)
            if not self.overwrite:
                out = unique_path(out)
            merge_pdfs(pdfs, out, lambda pct: self._emit('progress', {'percent': pct, 'index': 0,
                                                                     'total': 1}), self.pages)
            if self.keep_dates:
                copy_dates(src, out)
        except Exception as exc:
            self.error = str(exc) or '合并失败'
            self._set_state('error')
            self._emit('error', {'error': self.error})
            return
        self.results = [{'source': s, 'ok': True, 'out': out, 'engine': 'pypdf', 'error': ''}
                        for s in pdfs]
        self.done_count = len(pdfs)
        self._emit('file_done', {'source': '（%d 个 PDF 合并）' % len(pdfs), 'ok': True, 'out': out,
                                 'engine': 'pypdf', 'error': '', 'index': 0, 'total': 1,
                                 'percent': 100.0})
        self._finish()


# ---------------------------------------------------------------- 给界面用的工具
def expand_paths(paths, recursive=True, max_files=2000):
    """把拖进来的路径摊平成文件清单：文件夹按需要递归展开（对应 Filee 的 `-r`）

    返回 (files, notes)。notes 是给人看的提示（跳过隐藏文件 / 超上限 / 空文件夹）。
    """
    files, notes = [], []
    seen, hidden = set(), 0
    skip_dirs = ('$recycle.bin', 'node_modules', '__pycache__', 'system volume information')
    for raw in paths or []:
        p = os.path.abspath(str(raw))
        if os.path.isfile(p):
            cand = [p]
        elif os.path.isdir(p):
            cand = []
            if recursive:
                for root, dirs, names in os.walk(p):
                    dirs[:] = [d for d in dirs
                               if not d.startswith('.') and d.lower() not in skip_dirs]
                    for n in sorted(names):
                        if n.startswith('.'):
                            hidden += 1
                            continue
                        cand.append(os.path.join(root, n))
            else:
                try:
                    names = sorted(os.listdir(p))
                except Exception:
                    names = []
                for n in names:
                    full = os.path.join(p, n)
                    if n.startswith('.'):
                        hidden += 1
                    elif os.path.isfile(full):
                        cand.append(full)
            if not cand:
                notes.append('文件夹里没找到文件：%s' % os.path.basename(p))
        else:
            continue
        for f in cand:
            key = f.lower()
            if key in seen:
                continue
            seen.add(key)
            files.append(f)
            if len(files) >= max_files:
                notes.append('文件太多，先收前 %d 个' % max_files)
                return files, notes
    if hidden:
        notes.append('跳过 %d 个隐藏文件' % hidden)
    return files, notes


def group_by_kind(paths):
    """{kind: [paths]} —— 面板要按类型分组才知道该列哪些目标格式"""
    out = {}
    for p in paths or []:
        out.setdefault(kind_of(p) or 'other', []).append(p)
    return out


def common_targets(paths, include_filee=True, fetch_filee=True):
    """一批文件共同能转成的格式（求交集）

    单个文件 → 用 targets_for（排掉自身格式）；
    多个文件 → 用 kind_targets 求交集（含自身格式），这样一堆 jpg/png 也能统一转成 png，
    已经是 png 的那些在转换时直接跳过。
    `fetch_filee=False` 时只看 Filee 的缓存（界面线程用，不起进程）。
    """
    if not paths:
        return []
    if len(paths) == 1:
        return targets_for(ext_of(paths[0]), include_filee, fetch_filee)
    per = [_all_targets(ext_of(p), include_filee, fetch_filee) for p in paths]
    sets = [set(x) for x in per]
    return [t for t in per[0] if all(t in s for s in sets)]


def fmt_size(num):
    try:
        num = float(num or 0)
    except Exception:
        return '—'
    if num <= 0:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num < 1024 or unit == 'GB':
            return ('%.1f %s' % (num, unit)) if unit != 'B' else ('%d B' % num)
        num /= 1024.0
    return '—'


def open_folder(path):
    """资源管理器里打开（文件就选中它，目录就进去）—— 跟下载那边同一套行为"""
    try:
        import pet_download
        return pet_download.open_folder(path)
    except Exception:
        pass
    try:
        if path and os.path.isfile(path):
            if os.name == 'nt':
                subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
                return True
            path = os.path.dirname(path)
        if path and os.path.isdir(path):
            if os.name == 'nt':
                os.startfile(path)      # noqa: S606
                return True
            subprocess.Popen(['xdg-open', path])
            return True
    except Exception:
        pass
    return False


if __name__ == '__main__':      # 手动排查用：python pet_convert.py 某个文件 [目标格式]
    argv = sys.argv[1:]
    if argv:
        src_path = argv[0]
        dst = argv[1] if len(argv) > 1 else 'png'
        print('文件:', src_path, '类型:', kind_of(src_path) or '?')
        print('能转成:', ', '.join(targets_for(ext_of(src_path))) or '（无）')
        print('路由:', route(src_path, dst) or '（没人接）')
        print('结果:', convert_file(src_path, dst))
    else:
        print(status())
        print('内置引擎:', builtin_engines())
