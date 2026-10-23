# -*- coding: utf-8 -*-
"""v6.92 本地预训练声线库（pet_voicelib / voice_io 的 sherpa 分支 / 下载器规则）

使用者原话：「把你能找到的所有 github 免费训练好的声线都下载下来，放在朗读声线的列表中」。

这里守的是四件事：
1. 清单（MODELS）本身别写坏 —— key 唯一、仓库/文件/说话人数齐全；
2. 「已装」判定要稳 —— 下到一半的模型不能被当成可用（否则朗读时炸）；
3. 800 个说话人不能全塞进下拉框 —— 下拉只给代表音色，全量走「本地声线库」；
4. 本地模型坏了不能把桌宠念哑 —— 引擎顺序必须是 sherpa → edge（**不降级到系统声线**，v6.94）。
"""
import importlib.util
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pet_voicelib as vlib          # noqa: E402
import voice_io                      # noqa: E402


def _load_fetcher():
    """tools/fetch_voices.py 不是包，按路径加载"""
    path = os.path.join(BASE, 'tools', 'fetch_voices.py')
    spec = importlib.util.spec_from_file_location('fetch_voices', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_root(tmp_path, monkeypatch):
    """把模型根目录挪到临时目录，避免碰真实的 1.2 GB 下载"""
    root = tmp_path / 'voices'
    root.mkdir()
    monkeypatch.setattr(vlib, 'ROOT', str(root))
    monkeypatch.setattr(vlib, '_TTS', {}, raising=False)
    return root


def _install(root, key, onnx_bytes=1024):
    """伪造一个「已下载」的模型目录"""
    m = vlib.BY_KEY[key]
    d = os.path.join(str(root), key)
    os.makedirs(os.path.join(d, 'dict'), exist_ok=True)
    with open(os.path.join(d, m['onnx']), 'wb') as f:
        f.write(b'0' * onnx_bytes)
    with open(os.path.join(d, 'tokens.txt'), 'w', encoding='utf-8') as f:
        f.write('a 0\n')
    return d


# ---------- ① 清单 ----------

def test_manifest_covers_all_free_chinese_models():
    """清单里得有 8 个以上中文模型、合计 2000+ 说话人（这就是「全部能找到的」）"""
    keys = [m['key'] for m in vlib.MODELS]
    assert len(keys) == len(set(keys)), '模型 key 重了'
    assert len(vlib.MODELS) >= 8
    assert sum(vlib.speakers(k) for k in keys) >= 2000
    for m in vlib.MODELS:
        assert m['repo'] and m['onnx'].endswith('.onnx') and m['title']
        assert int(m['mb']) > 0 and vlib.speakers(m['key']) >= 1
        assert m['kind'] in ('vits', 'kokoro')


def test_manifest_paths_point_inside_root(fake_root):
    for m in vlib.MODELS:
        assert os.path.dirname(vlib.onnx_path(m['key'])) == vlib.model_dir(m['key'])
        assert vlib.model_dir(m['key']).startswith(str(fake_root))


# ---------- ② 已装判定 ----------

def test_half_downloaded_model_is_not_installed(fake_root):
    """只有 onnx 没有 tokens.txt = 下到一半 → 不能当可用"""
    m = vlib.BY_KEY['zh-ll']
    d = os.path.join(str(fake_root), 'zh-ll')
    os.makedirs(d)
    with open(os.path.join(d, m['onnx']), 'wb') as f:
        f.write(b'0' * 100)
    assert vlib.is_installed('zh-ll') is False
    assert 'zh-ll' in vlib.missing()
    with open(os.path.join(d, 'tokens.txt'), 'w', encoding='utf-8') as f:
        f.write('a 0\n')
    assert vlib.is_installed('zh-ll') is True
    assert 'zh-ll' in vlib.installed() and 'zh-ll' not in vlib.missing()


def test_installed_empty_when_nothing_downloaded(fake_root):
    assert vlib.installed() == []
    assert len(vlib.missing()) == len(vlib.MODELS)
    assert list(vlib.choices()) == [], '没装东西时下拉里不该冒出本地条目'


# ---------- ③ 配置值 -------------

def test_spec_and_split_roundtrip():
    assert vlib.spec('kokoro', 12) == 'sherpa:kokoro/12'
    assert vlib.split('kokoro/12') == ('kokoro', 12)
    assert vlib.split('kokoro') == ('kokoro', 0)
    assert vlib.split('kokoro/abc') == ('kokoro', 0), '脏数据当 0 号，不能抛异常'


def test_split_unknown_key_falls_back_to_installed(fake_root):
    _install(fake_root, 'melo-zh-en')
    key, spk = vlib.split('不存在的模型/7')
    assert key == 'melo-zh-en' and spk == 0
    assert vlib.split('') == ('melo-zh-en', 0)


def test_label_says_local():
    assert vlib.label('zh-ll').startswith('本地 · ')
    assert '#3' in vlib.label('zh-ll', 3)


# ---------- ④ 下拉只给代表音色 ----------

def test_representative_speakers_sampling():
    assert vlib.representative_speakers('zh-ll') == [0, 1, 2, 3, 4], '≤5 个就全给'
    picks = vlib.representative_speakers('theresa')          # 804
    assert len(picks) == vlib.REPRESENTATIVES
    assert picks == sorted(set(picks))
    assert all(0 <= p < 804 for p in picks)
    assert vlib.representative_speakers('theresa', limit=3)[-1] < 804


def test_choices_short_list_per_model(fake_root):
    _install(fake_root, 'theresa')                            # 804 声线
    got = vlib.choices()
    assert len(got) == vlib.REPRESENTATIVES, '下拉里不该出现 800 条'
    assert all(eng == 'sherpa' for _spec, _lab, eng in got)
    assert all(val.startswith('sherpa:theresa/') for val, _lab, _e in got)
    # 别再加"更多去本地声线库"的汇总项：它本身就是 #0 的克隆，点了只是换音色（实测踩过）
    assert len(set(v for v, _l, _e in got)) == len(got), '下拉条目不能重复'
    assert all('804' in lab for _v, lab, _e in got), '标签里要写清这模型一共多少个音色'


def test_choices_single_speaker_model(fake_root):
    _install(fake_root, 'fanchen-wnj')                        # 1 个男声
    got = vlib.choices()
    assert len(got) == 1
    assert got[0][0] == 'sherpa:fanchen-wnj/0'
    assert '男声' in got[0][1]


def test_all_voices_lists_every_speaker(fake_root):
    _install(fake_root, 'aishell3')
    assert len(vlib.all_voices()) == 174
    assert vlib.all_voices()[0][1] == 0


def test_summary_and_disk_usage(fake_root):
    assert vlib.disk_usage() == 0.0 or vlib.disk_usage() >= 0
    _install(fake_root, 'zh-ll', onnx_bytes=1024 * 1024)
    assert vlib.disk_usage() >= 1.0
    text = vlib.summary()
    assert '1/%d' % len(vlib.MODELS) in text and 'MB' in text


# ---------- ⑤ 引擎接线（sherpa 优先，坏了降级） ----------

def test_parse_voice_spec_accepts_sherpa():
    assert voice_io.parse_voice_spec('sherpa:kokoro/12') == ('sherpa', 'kokoro/12')


def test_engine_plan_puts_sherpa_first(tmp_path):
    v = voice_io.VoiceIO(base_dir=str(tmp_path), voice='sherpa:kokoro/12')
    assert v.engine_plan() == ['sherpa', 'edge'], '本地模型坏了降级到在线声线（不再是系统机械音）'
    v.set_config(allow_system_tts=True)
    assert v.engine_plan() == ['sherpa', 'edge', 'offline'], '显式打开才允许系统声线兜底'


def test_synth_any_uses_local_model(tmp_path, monkeypatch):
    seen = {}

    def fake_synth(key, spk, text, out, speed=1.0):
        seen.update(key=key, spk=spk, text=text, speed=speed)
        with open(out, 'wb') as f:
            f.write(b'RIFF' + b'0' * 1000)
        return True, ''

    monkeypatch.setattr(vlib, 'synth', fake_synth)
    v = voice_io.VoiceIO(base_dir=str(tmp_path), voice='sherpa:kokoro/12', rate='+15%')
    path, kind, engine = v._synth_any('你好', str(tmp_path / 'a'))
    assert engine == 'sherpa' and kind == 'wav' and path.endswith('.wav')
    assert seen['key'] == 'kokoro' and seen['spk'] == 12
    assert abs(seen['speed'] - 1.15) < 0.01, '语速要换算成倍率传下去'


def test_synth_any_falls_back_to_edge_when_model_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(vlib, 'synth', lambda *a, **k: (False, '模型文件坏了'))
    v = voice_io.VoiceIO(base_dir=str(tmp_path), voice='sherpa:kokoro/12')
    monkeypatch.setattr(v, '_synth_edge',
                        lambda text, p: (open(p, 'wb').write(b'x' * 999) and (True, '')))
    path, kind, engine = v._synth_any('你好', str(tmp_path / 'b'))
    assert engine == 'edge' and kind == 'mp3'
    assert v.fell_back is True and '模型文件坏了' in v.last_error


# ---------- ⑥ 下载器只取推理必需文件 ----------

def test_fetcher_skips_training_junk():
    fv = _load_fetcher()
    m = vlib.BY_KEY['aishell3']
    assert fv.wanted(m, 'vits-aishell3.int8.onnx') is True, '清单指定的 onnx 要下'
    assert fv.wanted(m, 'vits-aishell3.onnx') is False, '另一份导出（116MB）不要'
    assert fv.wanted(m, 'G_AISHELL.pth') is False, '456MB 训练权重不要'
    assert fv.wanted(m, 'rule.far') is False, '172MB 规则包不要'
    assert fv.wanted(m, 'README.md') is False
    assert fv.wanted(m, 'tokens.txt') is True and fv.wanted(m, 'lexicon.txt') is True
    assert fv.wanted(m, 'date.fst') is True, '数字/日期正则必需'
    # 带 jieba 词典的那几个模型：中文分词词典必须下
    assert fv.wanted(vlib.BY_KEY['zh-ll'], 'dict/jieba.dict.utf8') is True
    assert fv.wanted(vlib.BY_KEY['kokoro'], 'dict/jieba.dict.utf8') is True


def test_fetcher_kokoro_needs_voices_and_espeak():
    fv = _load_fetcher()
    m = vlib.BY_KEY['kokoro']
    assert fv.wanted(m, 'model.onnx') is True
    assert fv.wanted(m, 'voices.bin') is True, 'kokoro 的音色包'
    assert fv.wanted(m, 'espeak-ng-data/phondata') is True, '多语种音素表'
    assert fv.wanted(m, 'lexicon-zh.txt') is True


def test_fetcher_reads_system_proxy_for_urllib():
    """Python 的 urllib 不读注册表，直连 HuggingFace 会 10060；下载器必须自己找代理"""
    fv = _load_fetcher()
    assert hasattr(fv, '_system_proxy') and hasattr(fv, '_build_openers')
    openers = fv._build_openers()
    assert openers, '至少要有一个（代理或直连）opener 可用'


# ---------- ⑦ espeak 音素表（kokoro 专有坑） ----------

def test_espeak_data_root_copies_out_of_non_ascii_path(tmp_path, monkeypatch):
    """ESPEAK_DATA_PATH 里有中文 espeak 会静默失败并 exit(1) → 必须复制到 ASCII 缓存目录

    真实场景：本项目就装在 `F:\\桌宠\\…`（中文目录），第一版直接指过去，
    整个 Python 进程被 espeak 干掉（不是抛异常，抓都抓不住）。
    """
    root = tmp_path / '桌宠' / 'voices'
    src = root / 'kokoro' / 'espeak-ng-data'
    src.mkdir(parents=True)
    (src / 'phontab').write_bytes(b'x')
    (src / 'en_dict').write_bytes(b'y')
    monkeypatch.setattr(vlib, 'ROOT', str(root))
    cache = tmp_path / 'ascii-cache'
    monkeypatch.setenv('LOCALAPPDATA', str(cache))
    got = vlib.espeak_data_root('kokoro')
    assert got and all(ord(c) < 128 for c in got), '返回的路径必须是纯 ASCII：%r' % got
    assert os.path.isfile(os.path.join(got, 'espeak-ng-data', 'phontab')), '音素表要复制过去'
    assert vlib.espeak_data_root('kokoro') == got, '第二次直接复用，不重复复制'


def test_espeak_data_root_keeps_ascii_path_when_possible(tmp_path, monkeypatch):
    root = tmp_path / 'voices'
    (root / 'kokoro' / 'espeak-ng-data').mkdir(parents=True)
    monkeypatch.setattr(vlib, 'ROOT', str(root))
    assert vlib.espeak_data_root('kokoro') == vlib.model_dir('kokoro'), '路径本来就是 ASCII 就别复制'


def test_espeak_data_root_empty_without_kokoro(fake_root):
    assert vlib.espeak_data_root('kokoro') == '', '没装 kokoro 时返回空串（上层据此不设环境变量）'
