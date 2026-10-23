# -*- coding: utf-8 -*-
"""pet_reactions.py — 单击立绘的反应表（v6.78）

每次单击立绘随机挑一条：换一张立绘 + 说一句。表里每条 = (立绘, 中文, 英文)。
立绘名对应 assets/<角色>/<角色>_<立绘>.png（pro 缺的那几张由 desktop_pet.asset() 回退）。
立绘留空 = 只说话不换图。
改台词只改这张表，不动逻辑。测试：tests/test_click_reactions.py
"""
import random

REACTIONS = [
    ('happy',      '嘿嘿，被你点到啦',           'Hehe, you got me'),
    ('excited',    '哇！再点一下试试？',         'Whoa! Poke me again?'),
    ('shy',        '别、别突然戳我啦…',          "D-don't poke me out of nowhere..."),
    ('shy_hug',    '再戳我就要抱你了哦',         'One more poke and I hug you'),
    ('proud',      '哼，我本来就很可爱',         "Hmph, I've always been cute"),
    ('angry',      '再戳就真的生气咯',           'Poke again and I get mad'),
    ('sad',        '你是不是好久没来找我了',     "Haven't seen you in ages"),
    ('cry',        '呜…戳疼了啦',                'Waa... that hurt'),
    ('surprised',  '诶？！吓我一跳',             'Huh?! You scared me'),
    ('kiss',       '啵~ 只给你一个人',           'Mwah~ only for you'),
    ('pat',        '摸摸头…还要',                'Head pats... more please'),
    ('dance',      '看我跳一段！',               'Watch me dance!'),
    ('sing',       '♪ 啦啦啦~ 好听吗',           '♪ La la la~ nice?'),
    ('victory',    '今天也要赢！',               'Winning again today!'),
    ('defeat',     '呜哇…输了',                  'Waa... I lost'),
    ('hungry',     '饿了，想吃面',               "I'm hungry, noodles please"),
    ('waving',     '嗨~ 我在这儿呢',             "Hi~ I'm right here"),
    ('sleep',      '困了…让我眯一会儿',          'Sleepy... five more minutes'),
    ('thinking',   '唔…我在算一件事',            'Hmm... calculating something'),
    ('flower',     '这朵花送你',                 'This flower is for you'),
    ('flower_give', '拿着，别弄丢了',             "Take it, don't lose it"),
    ('gift',       '猜猜里面是什么',             'Guess what is inside'),
    ('hug_whale',  '抱着鲸鱼最有安全感',         'Hugging my whale feels safe'),
    ('lying',      '地板凉凉的，很舒服',         'The floor is nice and cool'),
    ('eating',     '吸溜——好吃！',               'Slurp -- so good!'),
    ('coffee',     '咖啡续命中…',                'Refueling with coffee...'),
    ('reading',    '别吵，正看到关键处',         "Quiet, key scene here"),
    ('music',      '这首歌超好听',               'This song is so good'),
    ('exercise',   '一起来运动嘛',               "Let's work out together"),
    ('typing',     '在敲代码，别打扰',           'Writing code, do not disturb'),
    ('phone',      '就刷五分钟…',                'Just five minutes of scrolling...'),
    ('umbrella',   '下雨也不怕',                 'Rain does not scare me'),
    ('thinking',   '你今天话好少哦',             'You are so quiet today'),
    ('happy',      '我一直都在呀',               "I'm always here"),
    ('pat',        '陪我待一会儿嘛',             'Stay with me a while'),
    ('proud',      '戳一下就够了啦',             'One poke is enough'),
]


def pick(last=-1):
    """随机挑一条的下标，尽量别跟上一条重复（last 越界时按纯随机处理）"""
    n = len(REACTIONS)
    if not 0 <= last < n:
        return random.randrange(n)
    i = random.randrange(n - 1)      # 只在"除 last 之外"的 n-1 条里抽
    return i + 1 if i >= last else i
