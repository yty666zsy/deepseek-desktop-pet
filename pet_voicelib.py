# -*- coding: utf-8 -*-
"""本地预训练声线库（v6.92）：把 GitHub/HuggingFace 上免费开源的 TTS 模型装进朗读列表

使用者原话：「把你能找到的所有 github 免费训练好的声线都下载下来，放在朗读声线的列表中」。

为什么要单独一层，而不是继续用 edge / SAPI：
  · 在线 edge-tts 音质好但**要联网**，且声线是微软的（换不了"自己的"声音）
  · 离线 SAPI/WinRT 不联网但**很机械**
  · 免费开源的预训练中文模型（VITS / MeloTTS / Kokoro / Piper）已经能做到
    "不联网 + 拟人"，而且都发布在 GitHub/HuggingFace 上，**下一个就是自己的**

技术选型：**sherpa-onnx**（k2-fsa/sherpa-onnx）当推理引擎，理由：
  1. CPU 就能跑（不用显卡），单文件 ONNX，`pip install sherpa-onnx` 一个包搞定
  2. 官方把 Piper / MeloTTS / Kokoro / VITS 全导出成同一套 ONNX 格式 → **一个引擎吃下所有模型**
  3. 纯 C++ 内核 + Python 绑定，没有 torch 那 2GB 依赖，桌宠启动不受影响

这三个模型族在本文件里怎么区分：
  · kind='vits'  ：VITS/MeloTTS/Piper 系（一个 onnx + tokens + lexicon + 分词词典）
  · kind='kokoro'：Kokoro 多语言（多一个 voices.bin 音色包 + espeak-ng-data）

声线清单（v6.92 收录的全部免费中文模型，合计约 1.2 GB / 2000+ 说话人）：

| key | 模型 | 说话人 | 说明 |
|---|---|---|---|
| zh-ll | sherpa-onnx-vits-zh-ll | 5 | 中文女声，中英混读自然 |
| fanchen-c | vits-zh-hf-fanchen-C | 187 | 花尘音色库，女声为主 |
| fanchen-wnj | vits-zh-hf-fanchen-wnj | 1 | 花尘男声（wnj） |
| theresa | vits-zh-hf-theresa | 804 | 大杂烩女声库 |
| eula | vits-zh-hf-eula | 804 | 大杂烩女声库（Eula） |
| aishell3 | vits-zh-aishell3 | 174 | 数据集合成，普通话标准 |
| melo-zh-en | vits-melo-tts-zh_en | 1 | 米壳 MeloTTS 中英双语 |
| kokoro | kokoro-multi-lang-v1_1 | 103 | Kokoro 82M，中英日多语，音质最好 |

维护须知：新增模型只需往 MODELS 里加一条（repo/onnx/speakers），
下载器与下拉列表都会自动带上，**不用改 UI 代码**。
"""
import json
import os
import threading

# 模型根目录（打包时随包，不进 git）
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'voices')

# 每个 VITS 模型的通用文件（有就下，没有就跳过 —— 用 include 规则过滤，不写死文件名列表）
_VITS_TEXT = ('tokens.txt', 'lexicon.txt', 'date.fst', 'number.fst', 'phone.fst',
              'new_heteronym.fst', 'G_multisperaker_latest.json')
_KOKORO_TEXT = ('tokens.txt', 'lexicon-us-en.txt', 'lexicon-zh.txt', 'lexicon-gb-en.txt',
                'date-zh.fst', 'number-zh.fst', 'phone-zh.fst')

MODELS = [
    dict(key='zh-ll', repo='csukuangfj/sherpa-onnx-vits-zh-ll', kind='vits',
         title='LL 中文 5 声线', note='女声为主，中英混读自然', speakers=5,
         onnx='model.onnx', text=_VITS_TEXT, dirs=('dict/',), mb=129),
    dict(key='fanchen-c', repo='csukuangfj/vits-zh-hf-fanchen-C', kind='vits',
         title='花尘 C · 187 声线', note='女声大库，音色差异明显', speakers=187,
         onnx='vits-zh-hf-fanchen-C.onnx', text=_VITS_TEXT, dirs=('dict/',), mb=131),
    dict(key='fanchen-wnj', repo='csukuangfj/vits-zh-hf-fanchen-wnj', kind='vits',
         title='花尘 WNJ · 男声', note='单个男声，偏低沉', speakers=1,
         onnx='vits-zh-hf-fanchen-wnj.onnx', text=_VITS_TEXT, dirs=('dict/',), mb=131),
    dict(key='theresa', repo='csukuangfj/vits-zh-hf-theresa', kind='vits',
         title='Theresa · 804 声线', note='巨型女声库，可慢慢挑', speakers=804,
         onnx='theresa.onnx', text=_VITS_TEXT, dirs=('dict/',), mb=132),
    dict(key='eula', repo='csukuangfj/vits-zh-hf-eula', kind='vits',
         title='Eula · 804 声线', note='巨型女声库（另一套）', speakers=804,
         onnx='eula.onnx', text=_VITS_TEXT, dirs=('dict/',), mb=132),
    dict(key='aishell3', repo='csukuangfj/vits-zh-aishell3', kind='vits',
         title='AISHELL-3 · 174 声线', note='普通话最标准，但只有 8 kHz（像电话）', speakers=174,
         onnx='vits-aishell3.int8.onnx', text=_VITS_TEXT, dirs=(), mb=63),
    dict(key='melo-zh-en', repo='csukuangfj/vits-melo-tts-zh_en', kind='vits',
         title='MeloTTS 中英', note='中英双语，语速稳（int8）', speakers=1,
         onnx='model.int8.onnx', text=_VITS_TEXT, dirs=('dict/',), mb=86),
    dict(key='kokoro', repo='csukuangfj/kokoro-multi-lang-v1_1', kind='kokoro',
         title='Kokoro 1.1 · 103 声线', note='音质最好，中英日多语', speakers=103,
         onnx='model.onnx', text=_KOKORO_TEXT, dirs=('dict/', 'espeak-ng-data/'),
         voices='voices.bin', mb=400),
]

BY_KEY = {m['key']: m for m in MODELS}

# 多说话人模型不能把 800 个全塞进下拉框 —— 每个模型挑几个代表，剩下的去「本地声线库」里翻
REPRESENTATIVES = 5


def model_dir(key):
    return os.path.join(ROOT, key)


def onnx_path(key):
    m = BY_KEY.get(key) or {}
    return os.path.join(model_dir(key), m.get('onnx') or 'model.onnx')


def is_installed(key):
    """模型装好了没：onnx 在 + tokens 在，才算（下到一半的不算）"""
    d = model_dir(key)
    if not os.path.isfile(onnx_path(key)):
        return False
    return os.path.isfile(os.path.join(d, 'tokens.txt'))


def installed():
    return [m['key'] for m in MODELS if is_installed(m['key'])]


def missing():
    return [m['key'] for m in MODELS if not is_installed(m['key'])]


def spec(key, speaker=0):
    """下拉框里存的配置值：'sherpa:<key>/<speaker>'"""
    return 'sherpa:%s/%d' % (key, int(speaker or 0))


def split(name):
    """'fanchen-c/12' → ('fanchen-c', 12)；容错：只给 key 就当 0 号"""
    raw = str(name or '').strip()
    key, _, spk = raw.partition('/')
    key = key.strip()
    try:
        spk = int(spk)
    except (TypeError, ValueError):
        spk = 0
    if key not in BY_KEY:
        key = (installed() or [''])[0]
        spk = 0                     # 模型都不认识，说话人号也没有意义
    return key, max(0, spk)


def label(key, speaker=None):
    m = BY_KEY.get(key)
    if not m:
        return str(key)
    if speaker is None:
        return '本地 · %s' % m['title']
    return '本地 · %s #%d' % (m['title'], int(speaker))


def speakers(key):
    m = BY_KEY.get(key) or {}
    try:
        return max(1, int(m.get('speakers') or 1))
    except (TypeError, ValueError):
        return 1


def representative_speakers(key, limit=REPRESENTATIVES):
    """给下拉框用的代表音色：说话人少就全给，多的就均匀抽几个"""
    n = speakers(key)
    if n <= limit:
        return list(range(n))
    step = max(1, n // limit)
    picks = [i * step for i in range(limit)]
    picks[-1] = min(picks[-1], n - 1)
    return sorted(set(picks))


def choices():
    """朗读声线下拉里追加的本地条目：[(配置值, 显示名, 引擎), …]（只列已下载的）

    设计取舍：**只列代表音色**，不再额外塞一条「更多去本地声线库」的汇总项 ——
    那条汇总项本身就是 #0 号的克隆，用户点它会以为"打开了库"却只是换了音色（实测踩过）。
    想要更多音色，设置页「🎚 本地声线库…」就在旁边。
    """
    out = []
    for key in installed():
        m = BY_KEY[key]
        if speakers(key) <= 1:
            out.append((spec(key, 0), '本地 · %s（%s）' % (m['title'], m['note']), 'sherpa'))
            continue
        for spk in representative_speakers(key):
            out.append((spec(key, spk), '%s（共 %d 个）' % (label(key, spk), speakers(key)),
                        'sherpa'))
    return out


def all_voices():
    """「本地声线库」对话框用：[(key, speaker, 显示名)]，把全部说话人都列出来"""
    out = []
    for key in installed():
        for spk in range(speakers(key)):
            out.append((key, spk, label(key, spk)))
    return out


# ---------- 推理（sherpa-onnx） ----------
_TTS = {}
_LOCK = threading.Lock()


def espeak_data_root(key):
    """给 kokoro 找一份 **ASCII 路径** 的 espeak-ng-data，返回它的上一级目录（没装 kokoro 返回 ''）

    两个实测过的坑（都写在这里免得再踩）：
    1. sherpa-onnx 的 Windows 版**不认 `data_dir`**，espeak-ng 只读环境变量 `ESPEAK_DATA_PATH`；
       不设它，espeak 会去找编译期写死的 `/usr/share/espeak-ng-data`，找不到就**直接 exit(1)
       把整个进程干掉**（不是抛异常，Python 层抓不住 —— 桌宠会凭空消失）。
    2. `ESPEAK_DATA_PATH` 里**不能有中文**：本项目在 `F:\\桌宠\\…` 下，直接指过去 espeak 会静默失败
       再走回默认路径同样 exit(1)。所以路径不纯 ASCII 时，把音素表复制到 ASCII 的缓存目录（17 MB，只做一次）。
    """
    d = model_dir(key)
    src = os.path.join(d, 'espeak-ng-data')
    if not os.path.isdir(src):
        return ''
    if all(ord(ch) < 128 for ch in d):
        return d
    cache_parent = os.path.join(os.environ.get('LOCALAPPDATA')
                               or os.environ.get('TEMP') or os.path.expanduser('~'),
                               'desktop-pet-tts')
    dst = os.path.join(cache_parent, 'espeak-ng-data')
    if not os.path.isfile(os.path.join(dst, 'phontab')):
        try:
            import shutil
            shutil.rmtree(dst, ignore_errors=True)
            os.makedirs(cache_parent, exist_ok=True)
            shutil.copytree(src, dst)
        except Exception:
            return ''                      # 复制失败就当没有，让上层报"合成失败"而不是崩
    return cache_parent


def build_config(key):
    """按模型类型拼 sherpa_onnx.OfflineTtsConfig（vits 与 kokoro 字段不同）"""
    import sherpa_onnx
    m = BY_KEY[key]
    d = model_dir(key)
    tokens = os.path.join(d, 'tokens.txt')
    dict_dir = os.path.join(d, 'dict')
    if not os.path.isdir(dict_dir):
        dict_dir = ''
    if m['kind'] == 'kokoro':
        root = espeak_data_root(key)
        if root:
            os.environ['ESPEAK_DATA_PATH'] = root      # 必须在建 OfflineTts 之前设（espeak 初始化时读）
        ko = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=onnx_path(key),
            voices=os.path.join(d, m.get('voices') or 'voices.bin'),
            tokens=tokens,
            data_dir=os.path.join(root, 'espeak-ng-data') if root else '',   # 多语靠 espeak 音素
            dict_dir=dict_dir,
            lexicon=','.join(os.path.join(d, f) for f in ('lexicon-us-en.txt', 'lexicon-zh.txt')
                             if os.path.isfile(os.path.join(d, f))),
        )
        mc = sherpa_onnx.OfflineTtsModelConfig(kokoro=ko, num_threads=2, provider='cpu')
        # kokoro 只支持逐句合成（传 2 会被忽略并刷一条警告）
        return sherpa_onnx.OfflineTtsConfig(model=mc, max_num_sentences=1)
    else:
        lexicon = os.path.join(d, 'lexicon.txt')
        vi = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=onnx_path(key),
            lexicon=lexicon if os.path.isfile(lexicon) else '',
            tokens=tokens,
            dict_dir=dict_dir,
            data_dir='',
        )
        mc = sherpa_onnx.OfflineTtsModelConfig(vits=vi, num_threads=2, provider='cpu')
    return sherpa_onnx.OfflineTtsConfig(model=mc, max_num_sentences=2)


def get_tts(key):
    """取（并缓存）模型实例。加载 100MB 模型要 1-3 秒，绝不能每句话重载"""
    with _LOCK:
        tts = _TTS.get(key)
        if tts is None:
            import sherpa_onnx
            tts = sherpa_onnx.OfflineTts(build_config(key))
            _TTS[key] = tts
        return tts


def drop_cache(key=None):
    with _LOCK:
        if key is None:
            _TTS.clear()
        else:
            _TTS.pop(key, None)


def synth(key, speaker, text, out_path, speed=1.0):
    """合成到 WAV。返回 (ok, err)。"""
    try:
        import sherpa_onnx
    except Exception as e:
        return False, 'sherpa_onnx 不可用（pip install sherpa-onnx）：%s' % type(e).__name__
    if not is_installed(key):
        return False, '模型没下载：%s' % key
    try:
        tts = get_tts(key)
        audio = tts.generate(str(text or ''), sid=int(speaker), speed=float(speed or 1.0))
        if audio is None or not len(audio.samples):
            return False, '合成没有产出音频'
        sherpa_onnx.write_wave(str(out_path), audio.samples, audio.sample_rate)
    except Exception as e:
        return False, '本地合成失败：%s' % str(e)[:120]
    if not os.path.isfile(out_path) or os.path.getsize(out_path) < 512:
        return False, '合成音频为空'
    return True, ''


def disk_usage():
    """已下载占多少 MB（给设置页显示）"""
    total = 0
    for key in installed():
        for base, _dirs, files in os.walk(model_dir(key)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
    return round(total / (1024.0 * 1024.0), 1)


def summary():
    """一句话状态：'已装 3/8（1024 MB）'"""
    got = installed()
    return '已装 %d/%d（%s MB）' % (len(got), len(MODELS), disk_usage())


def save_index(data, path):
    """小工具：原子写 JSON（下载器记录已完成文件用）"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
