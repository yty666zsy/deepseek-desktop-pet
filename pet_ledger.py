# -*- coding: utf-8 -*-
"""pet_ledger.py — 记账内核（v6.89，使用者拍板 1A 2A 3A 4A 5A 6A）
==============================================================
**单式记账**：一条一笔（日期 / 金额 / 收支 / 分类 / 账户 / 备注）。
纯逻辑，不 import Qt —— 好写单测，也方便「聊天里说一句就记」。

为什么自己写、不嵌现成的
------------------------
调研结论（写在这里免得以后重走）：Firefly III（AGPL，PHP+Docker）是自托管复式账本，
Actual Budget（MIT，Node+React）是信封预算 App，Beancount 是纯文本复式 DSL ——
**都是「打开网页/App 填表单」的形态**，塞不进 PySide6 桌宠，AGPL 也不能借代码。
真正借过来的只有三样东西：
  · 规则自动分类（Firefly III 的 rule engine 思路）→ 我们的 `CAT_WORDS` 词典
  · 每类别月度限额（Actual 的信封预算）→ `budgets` + `budget_state()`
  · 分类树命名（Beancount 的 Expenses:Food 那套）→ 中文扁平分类 + 图标
桌宠真正赢的地方是**入口**：说「午饭 32」就记上了，不用打开任何窗口。

数据文件（ledger.json，跟 memories.json 一个待遇：原子写、坏了当空账本、绝不静默丢数据）
{
  "version": 1,
  "entries": [{"id","date","amount","kind","cat","account","note","src","created"}],
  "budgets": {"餐饮": 1500},
  "cats": ["咖啡", "宠物"]        ← 用户自定义分类
}

解析顺序（本地优先，AI 兜底 —— 拍板 3A）：
  日期 → 账户 → 金额 → 分类 → 备注，各步都从文本里**原地摘掉**已认出的部分，
  剩下的才算备注（所以「昨天 晚饭 85 微信」的备注是「晚饭」，不是一整句）
"""
import json
import os
import re
import time
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LEDGER_PATH = os.path.join(BASE_DIR, 'ledger.json')

# 配置键（lg_ 前缀，跟 pw_/cv_/dl_/mu_ 一套命名）
CFG_ACCOUNT = 'lg_account'     # 上次记账用的账户
CFG_MONTH = 'lg_month'         # 上次看的月份
CFG_ALERT = 'lg_alert'         # 超预算时桌宠要不要吭声（默认开）
CFG_CATS = 'lg_cats'           # 自定义分类（也存一份在 config 里，方便设置窗口改）

MAX_ENTRIES = 20000            # 账本上限（防手滑导入把文件撑爆）
MAX_AMOUNT = 1e8               # 单笔金额上限

# ---------------------------------------------------------------- 分类
# 每组：分类名 / 图标 / 关键词（命中即归类，先匹配组内词多的那组，避免「奶茶」被「茶」抢）
CAT_GROUPS = (
    ('餐饮', '🍚', ('午饭', '晚饭', '早饭', '早餐', '午餐', '晚餐', '夜宵', '外卖', '奶茶',
                    '咖啡', '火锅', '烧烤', '炸鸡', '汉堡', '面条', '包子', '食堂', '小笼包',
                    '下午茶', '吃饭', '聚餐', '零食', '水果', '饮料', '蛋糕', '甜品', '饭')),
    ('交通', '🚕', ('打车', '滴滴', '地铁', '公交', '加油', '油费', '停车', '过路费', '高铁',
                    '火车', '机票', '飞机', '车票', '共享单车', '骑车', '顺风车', '代驾')),
    ('购物', '🛍', ('淘宝', '京东', '拼多多', '衣服', '裤子', '外套', '鞋子', '化妆品', '护肤',
                    '数码', '耳机', '充电器', '键盘', '背包', '钱包', '包包', '双肩包', '快递',
                    '网购', '买个')),
    ('日用', '🧺', ('超市', '日用品', '纸巾', '洗衣液', '洗发水', '牙膏', '杂货', '便利店',
                    '垃圾袋', '清洁')),
    ('居住', '🏠', ('房租', '水电', '水费', '电费', '物业', '燃气', '取暖', '宽带', '家具',
                    '维修', '装修', '中介费')),
    ('医疗', '💊', ('医院', '挂号', '看病', '买药', '药', '体检', '牙医', '眼镜', '口罩',
                    '疫苗', '门诊')),
    ('娱乐', '🎮', ('电影', '游戏', 'KTV', '唱歌', '演出', '门票', '会员', '视频会员', '订阅',
                    '桌游', '按摩', '旅游', '剧本杀', '演唱会')),
    ('学习', '📚', ('买书', '书', '课程', '学费', '文具', '考试', '培训', '网课', '打印',
                    '论文', '资料')),
    ('人情', '🎁', ('红包', '礼金', '礼物', '请客', '份子', '孝敬', '给妈', '给爸', '随礼')),
    ('通讯', '📱', ('话费', '流量', '充值', '手机费', '宽带费')),
    ('其他', '📦', ()),
)
# 收入分类（钱进来了也得分门别类，不然报表里「其他」会吃成一个大饼）
INCOME_GROUPS = (
    ('工资', '💰', ('工资', '薪水', '奖金', '绩效', '年终奖', '补贴')),
    ('报销', '🧾', ('报销',)),
    ('红包', '🧧', ('红包', '压岁钱', '礼金')),
    ('退款', '↩️', ('退款', '退了', '返现', '退货')),
    ('理财', '📈', ('利息', '分红', '理财', '收益', '基金', '股票')),
    ('其他收入', '🪙', ()),
)
CATS = [g[0] for g in CAT_GROUPS]
INCOME_CATS = [g[0] for g in INCOME_GROUPS]
CAT_ICON = {}
CAT_ICON.update({g[0]: g[1] for g in CAT_GROUPS})
CAT_ICON.update({g[0]: g[1] for g in INCOME_GROUPS})
ALL_CATS = CATS + [c for c in INCOME_CATS if c not in CATS]
_CAT_WORDS = [(g[0], g[2]) for g in CAT_GROUPS]
_INCOME_CAT_WORDS = [(g[0], g[2]) for g in INCOME_GROUPS]

# 收入关键词（出现即按收入记；「红包」要看向哪边，见 _guess_kind）
INCOME_WORDS = ('工资', '发工资', '薪水', '奖金', '年终奖', '绩效', '补贴', '报销', '退款',
                '退了', '收入', '分红', '利息', '中奖', '卖出', '收到', '进账', '返现')
RECEIVE_WORDS = ('收到', '领了', '收了', '抢到', '进账')

ACCOUNT_WORDS = ('微信', '支付宝', '现金', '银行卡', '信用卡', '花呗', '云闪付', '现金卡')
DEFAULT_ACCOUNTS = ('微信', '支付宝', '现金', '银行卡', '信用卡')

WEEKDAY_CN = ('一', '二', '三', '四', '五', '六', '日')
_WEEKDAY_MAP = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6, '七': 0}

_MONTH_RE = re.compile(r'^(\d{4})[-/.](\d{1,2})$')
_DATE_FULL_RE = re.compile(r'(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?')
_DATE_MD_RE = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]')
_DATE_D_RE = re.compile(r'(?<!\d)(\d{1,2})\s*[日号](?!\d)')
_NUM_RE = re.compile(r'(?<![\d.])(\d{1,9}(?:\.\d{1,2})?)(?![\d.])')
_MONEY_RE = re.compile(r'(?:¥|￥|\$)?\s*(\d{1,9}(?:\.\d{1,2})?)\s*(万元|万|千元|千|块|元|钱|rmb|RMB|k|K)')


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


def cfg_text(key, default=''):
    v = cfg_read().get(key)
    return default if v is None or v == '' else str(v)


def cfg_num(key, default=0):
    try:
        v = cfg_read().get(key)
        return default if v is None else float(v)
    except Exception:
        return default


def cfg_bool(key, default=True):
    v = cfg_read().get(key)
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ('0', 'false', 'no', '', 'off')
    return bool(v)


# ---------------------------------------------------------------- 日期工具
def today(d=None):
    return d or datetime.date.today()


def today_str(d=None):
    return today(d).strftime('%Y-%m-%d')


def norm_date(value):
    """把各种写法收成 YYYY-MM-DD；认不出返回 ''（不猜、不瞎补）"""
    if value is None:
        return ''
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime('%Y-%m-%d')
    s = str(value).strip()
    if not s:
        return ''
    m = _DATE_FULL_RE.search(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(y, mo, d).strftime('%Y-%m-%d')
        except Exception:
            return ''
    m = re.match(r'^(\d{4})(\d{2})(\d{2})$', s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime('%Y-%m-%d')
        except Exception:
            return ''
    return ''


def norm_month(value):
    """月份归一成 YYYY-MM；认不出返回本月"""
    if value is None or str(value).strip() == '':
        return today_str()[:7]
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime('%Y-%m')
    s = str(value).strip()
    m = _MONTH_RE.match(s)
    if m:
        return '%04d-%02d' % (int(m.group(1)), int(m.group(2)))
    d = norm_date(s)
    if d:
        return d[:7]
    m = _DATE_MD_RE.search(s)
    if m:
        return '%s-%02d' % (today_str()[:4], int(m.group(1)))
    return today_str()[:7]


def shift_month(month, delta):
    """月份加减（-1 = 上个月），跨年也对"""
    y, m = int(str(month)[:4]), int(str(month)[5:7])
    idx = (y * 12 + (m - 1)) + int(delta)
    return '%04d-%02d' % (idx // 12, idx % 12 + 1)


def month_label(month):
    """2026-10 → 2026年10月"""
    month = norm_month(month)
    return '%s年%d月' % (month[:4], int(month[5:7]))


def days_in_month(month):
    month = norm_month(month)
    y, m = int(month[:4]), int(month[5:7])
    nxt = shift_month(month, 1)
    d1 = datetime.date(y, m, 1)
    d2 = datetime.date(int(nxt[:4]), int(nxt[5:7]), 1)
    return (d2 - d1).days


def weekday_cn(date_str):
    d = norm_date(date_str)
    if not d:
        return ''
    try:
        return '周' + WEEKDAY_CN[datetime.date(*[int(x) for x in d.split('-')]).weekday()]
    except Exception:
        return ''


def days_between(a, b):
    """b - a 的天数（跟照片墙一个语义）"""
    da, db = norm_date(a), norm_date(b)
    if not (da and db):
        return None
    try:
        return (datetime.date(*[int(x) for x in db.split('-')])
                - datetime.date(*[int(x) for x in da.split('-')])).days
    except Exception:
        return None


# ---------------------------------------------------------------- 解析（本地优先）
def _parse_date(text):
    """从话里认出日期并把它从文本里摘掉 → (date_str, rest, hit, source)

    source: 'rel'（昨天/前天…，自带月份含义，不再按月偏移）
            'abs'（3 号 / 12月3日 / 周三…，可能要按「上月」偏移）
            'none'（没认出来，用今天）
    """
    s = text or ''
    t = today()

    for word, delta in (('大前天', -3), ('前天', -2), ('昨天', -1), ('昨日', -1),
                        ('今天', 0), ('今日', 0), ('刚才', 0), ('刚刚', 0),
                        ('明天', 1), ('后天', 2)):
        if word in s:
            d = t + datetime.timedelta(days=delta)
            return d.strftime('%Y-%m-%d'), s.replace(word, ' '), word, 'rel'

    m = _DATE_FULL_RE.search(s)
    if m:
        d = norm_date(m.group(0))
        if d:
            return d, s[:m.start()] + ' ' + s[m.end():], m.group(0), 'abs'

    m = _DATE_MD_RE.search(s)
    if m:
        mo, dd = int(m.group(1)), int(m.group(2))
        try:
            d = datetime.date(t.year, mo, dd)
            if d > t + datetime.timedelta(days=1):     # 说「12月」但还没到 → 指去年
                d = datetime.date(t.year - 1, mo, dd)
            return d.strftime('%Y-%m-%d'), s[:m.start()] + ' ' + s[m.end():], m.group(0), 'abs'
        except Exception:
            pass

    m = _DATE_D_RE.search(s)
    if m:
        dd = int(m.group(1))
        try:
            d = datetime.date(t.year, t.month, dd)
            if d > t:
                # 「30号」但本月只有 29 天 / 还没到 → 往前一个月
                prev = shift_month(today_str(), -1)
                d = datetime.date(int(prev[:4]), int(prev[5:7]), dd)
            return d.strftime('%Y-%m-%d'), s[:m.start()] + ' ' + s[m.end():], m.group(0), 'abs'
        except Exception:
            pass

    m = re.search(r'(\d{1,2})\s*天前', s)
    if m:
        d = t - datetime.timedelta(days=int(m.group(1)))
        return d.strftime('%Y-%m-%d'), s[:m.start()] + ' ' + s[m.end():], m.group(0), 'rel'

    m = re.search(r'(上上周|上周|上星期|这周|这星期|本周|周|星期)\s*([一二三四五六日天七])', s)
    if m:
        pref, wd = m.group(1), m.group(2)
        target = _WEEKDAY_MAP.get(wd, 0)
        base = t - datetime.timedelta(days=(t.weekday() - target) % 7)   # 本周那个星期几
        if pref in ('上周', '上星期'):
            base -= datetime.timedelta(days=7)
        elif pref == '上上周':
            base -= datetime.timedelta(days=14)
        return base.strftime('%Y-%m-%d'), s[:m.start()] + ' ' + s[m.end():], m.group(0), 'abs'

    return today_str(), s, '', 'none'


def _month_shift(text):
    """「上月/上个月/下个月」这类相对月份 → 偏移几个月（0 表示没提到）"""
    s = text or ''
    if re.search(r'上上个月|上上月', s):
        return -2
    if re.search(r'上月|上个月|上一个月', s):
        return -1
    if re.search(r'下下个月|下下月', s):
        return 2
    if re.search(r'下个月|下月', s):
        return 1
    return 0


def _shift_date_month(date_str, delta):
    """把日期整体挪几个月（该日不存在就落到月末，比如 31 号 → 2 月 28）"""
    if not delta:
        return date_str
    target = shift_month(date_str[:7], delta)
    y, m = int(target[:4]), int(target[5:7])
    d = min(int(date_str[8:10]), days_in_month(target))
    try:
        return datetime.date(y, m, d).strftime('%Y-%m-%d')
    except Exception:
        return date_str


def _parse_account(text):
    s = text or ''
    for word in ACCOUNT_WORDS:
        if word in s:
            return word, s.replace(word, ' ', 1)
    return '', s


def _to_number(num, unit):
    v = float(num)
    if unit in ('万', '万元'):
        v *= 10000
    elif unit in ('千', '千元', 'k', 'K'):
        v *= 1000
    return round(v, 2)


def _parse_amount(text):
    """从话里认出金额并摘掉 → (amount or None, rest, hit_text)"""
    s = text or ''
    # 带钱单位的最可信（「32 块」「1.2万」「¥99」）
    m = _MONEY_RE.search(s)
    if m:
        v = _to_number(m.group(1), m.group(2))
        if 0 < v <= MAX_AMOUNT:
            return v, s[:m.start()] + ' ' + s[m.end():], m.group(0).strip()
    # 否则取最后一个数字（「买菜 12 块 3 斤」这种取 12 会错，但带单位的上面已经拦了）
    cands = list(_NUM_RE.finditer(s))
    for m in reversed(cands):
        v = float(m.group(1))
        if 0 < v <= MAX_AMOUNT:
            return round(v, 2), s[:m.start()] + ' ' + s[m.end():], m.group(1)
    return None, s, ''


def _guess_kind(text, cat=''):
    """收支方向：默认支出；收入词命中 → 收入（「收到红包」算收入，「发红包」算支出）"""
    s = text or ''
    if any(w in s for w in RECEIVE_WORDS):
        return 'income'
    if any(w in s for w in INCOME_WORDS) and not re.search(r'(发|买|付|交|还|给)\s*' + re.escape(cat or '红包'), s):
        return 'income'
    return 'expense'


def guess_cat(text, cats=None, kind='expense'):
    """关键词归类：命中词最多的组胜出；同分按组顺序（餐饮在最前，日常最多）

    kind 决定查哪张词表 —— 钱进来的「红包/工资」和花出去的「红包/工资」不是一类，
    不分开的话报表里「其他」会被收入吃成一个大饼。"""
    s = text or ''
    table = _INCOME_CAT_WORDS if kind == 'income' else _CAT_WORDS
    best, best_hits, best_word = '', 0, ''
    for cat, words in table:
        if not words:
            continue
        hits = [w for w in words if w in s]
        if len(hits) > best_hits or (hits and len(hits) == best_hits and not best):
            best, best_hits, best_word = cat, len(hits), (sorted(hits, key=len, reverse=True)[0] if hits else '')
    for cat in (cats or []):
        if cat and cat in s and not best:
            return cat, cat
    fallback = '其他收入' if kind == 'income' else '其他'
    return (best or fallback), best_word


# 「这些词不算信息」：记进备注只会显得啰嗦
_NOTE_STOP = ('一次', '一共', '总共', '花了', '花销', '用了', '付了', '消费', '支出',
              '记一笔', '记一下', '记账', '块钱', '块钱的', '大概', '大约', '好像',
              '上上个月', '上上月', '上个月', '上月', '下个月', '下月', '这个月', '本月')


def clean_note(text):
    """把摘完日期/金额/账户后的残留整理成一句人话"""
    s = re.sub(r'[，,。;；!！?？、]+', ' ', text or '')
    s = re.sub(r'\s+', ' ', s).strip(' -─—:：')
    s = re.sub(r'^(花了|花|一共|总共|用了|付了|记一笔|记|支出|消费)\s*', '', s)
    for w in _NOTE_STOP:
        s = s.replace(w, ' ')
    s = re.sub(r'\s+', ' ', s).strip(' -─—:： ')
    return s


def parse_entry(text, cats=None, accounts=None):
    """把一句话解析成一笔账（本地规则；解析不出的部分放进 missing，交给 AI 兜底）

    返回 dict：
      ok       金额认出来了就是 True（其余字段都有默认值，能直接记）
      date/amount/kind/cat/account/note
      missing  缺的字段名列表（目前只可能是 ['金额']）
      hit      解析到的原文片段（给用户回显「我把『昨天』认成 10-20」）
    """
    raw = (text or '').strip()
    out = {'ok': False, 'date': today_str(), 'amount': 0.0, 'kind': 'expense',
           'cat': '其他', 'account': '', 'note': '', 'missing': [], 'hit': {}, 'raw': raw}
    if not raw:
        out['missing'] = ['内容']
        return out

    date, rest, date_hit, date_src = _parse_date(raw)
    shift = _month_shift(raw)
    if shift and date_src == 'abs':
        date = _shift_date_month(date, shift)
    account, rest = _parse_account(rest)
    amount, rest2, amount_hit = _parse_amount(rest)

    # 先定收支方向（它决定查哪张分类词表），再归类
    kind = _guess_kind(raw)
    cat, cat_hit = guess_cat(raw, cats, kind)

    out['date'] = date
    out['account'] = account or cfg_text(CFG_ACCOUNT, '')
    out['cat'] = cat
    out['kind'] = kind
    out['hit'] = {'date': date_hit or ('上月' if shift else ''),
                  'account': account, 'amount': amount_hit, 'cat': cat_hit}

    if amount is None:
        out['missing'] = ['金额']
        out['note'] = clean_note(rest2) or raw
        return out

    out['amount'] = amount
    out['ok'] = True
    note = clean_note(rest2)
    if not note or note == cat_hit:
        note = cat_hit or cat
    out['note'] = note[:60]
    return out


# ---------------------------------------------------------------- 金额/展示
def fmt_amount(value, sign=False):
    """3200 → ¥3,200；32.5 → ¥32.50（整数不带小数，省得一列全是 .00）；sign=True 时负数带符号"""
    try:
        v = float(value or 0)
    except Exception:
        v = 0.0
    prefix = '-¥' if (sign and v < 0) else '¥'
    return prefix + _num_text(abs(v))


def _num_text(v):
    if abs(v - round(v)) < 0.005:
        return '{:,}'.format(int(round(v)))
    return '{:,.2f}'.format(v)


def money(value):
    return _num_text(float(value or 0))


def kind_label(kind):
    return '收入' if kind == 'income' else '支出'


def cat_icon(cat):
    return CAT_ICON.get(cat, '📦')


# ---------------------------------------------------------------- 账本
class LedgerBook(object):
    """ledger.json 的读写 + 统计 + 预算。线程安全（面板和 AI 工具可能同时动）"""

    def __init__(self, path=None):
        import threading
        self.path = path or LEDGER_PATH
        self._lock = threading.RLock()
        self.data = self._load()

    # ---------- 存取 ----------
    def _blank(self):
        return {'version': 1, 'entries': [], 'budgets': {}, 'cats': []}

    def _load(self):
        try:
            if not os.path.isfile(self.path):
                return self._blank()
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            # 文件坏了：留着原文件（别覆盖），先用空账本，界面会提示
            try:
                if os.path.isfile(self.path):
                    os.replace(self.path, self.path + '.broken-%d' % int(time.time()))
            except Exception:
                pass
            return self._blank()
        if not isinstance(data, dict):
            return self._blank()
        data.setdefault('version', 1)
        data.setdefault('entries', [])
        data.setdefault('budgets', {})
        data.setdefault('cats', [])
        if not isinstance(data['entries'], list):
            data['entries'] = []
        if not isinstance(data['budgets'], dict):
            data['budgets'] = {}
        if not isinstance(data['cats'], list):
            data['cats'] = []
        return data

    def reload(self):
        with self._lock:
            self.data = self._load()
        return self

    def save(self):
        with self._lock:
            self.data['entries'] = self.data['entries'][-MAX_ENTRIES:]
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        return True

    # ---------- 分类 ----------
    def cats(self, kind=None):
        """分类下拉的候选（支出/收入各一套；自定义分类两边都出现）"""
        if kind == 'income':
            base = list(INCOME_CATS)
        elif kind == 'expense':
            base = list(CATS)
        else:
            base = list(ALL_CATS)
        extra = [c for c in (self.data.get('cats') or []) if c and c not in base]
        return base + extra

    def cats_for(self, kind):
        return self.cats(kind)

    def add_cat(self, cat):
        cat = str(cat or '').strip()
        if not cat or cat in self.cats():
            return False
        self.data.setdefault('cats', []).append(cat)
        self.save()
        return True

    # ---------- 增删改查 ----------
    def _new_id(self):
        base = 'e%d' % int(time.time() * 1000)
        taken = {e.get('id') for e in self.data['entries']}
        if base not in taken:
            return base
        i = 1
        while ('%s_%d' % (base, i)) in taken:
            i += 1
        return '%s_%d' % (base, i)

    def normalize(self, entry):
        """字段归一（坏数据不写进账本；金额非法的直接拒）"""
        e = dict(entry or {})
        e['date'] = norm_date(e.get('date')) or today_str()
        try:
            amount = round(float(e.get('amount') or 0), 2)
        except Exception:
            amount = 0.0
        if amount <= 0 or amount > MAX_AMOUNT:
            return None
        e['amount'] = amount
        e['kind'] = 'income' if str(e.get('kind')) in ('income', '收入', 'in') else 'expense'
        cat = str(e.get('cat') or '其他').strip() or '其他'
        e['cat'] = cat
        acct = str(e.get('account') or '').strip()
        e['account'] = acct
        e['note'] = str(e.get('note') or '').strip()[:60]
        e['src'] = str(e.get('src') or 'manual')[:16]
        e['created'] = str(e.get('created') or
                           datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))
        if not e.get('id'):
            e['id'] = self._new_id()
        return e

    def add(self, entry):
        """加一笔（返回存进去的那条；金额非法返回 None）"""
        e = self.normalize(entry)
        if e is None:
            return None
        with self._lock:
            self.data['entries'].append(e)
            self.save()
        return e

    def get(self, eid):
        for e in self.data['entries']:
            if e.get('id') == eid:
                return e
        return None

    def update(self, eid, **fields):
        with self._lock:
            e = self.get(eid)
            if e is None:
                return None
            merged = dict(e)
            merged.update({k: v for k, v in fields.items() if k in
                           ('date', 'amount', 'kind', 'cat', 'account', 'note')})
            fixed = self.normalize(merged)
            if fixed is None:
                return None
            fixed['id'] = eid
            fixed['created'] = e.get('created', fixed['created'])
            idx = self.data['entries'].index(e)
            self.data['entries'][idx] = fixed
            self.save()
        return fixed

    def delete(self, eid):
        with self._lock:
            e = self.get(eid)
            if e is None:
                return False
            self.data['entries'].remove(e)
            self.save()
        return True

    def clear_month(self, month):
        month = norm_month(month)
        with self._lock:
            n = len(self.data['entries'])
            self.data['entries'] = [e for e in self.data['entries']
                                    if str(e.get('date', ''))[:7] != month]
            removed = n - len(self.data['entries'])
            if removed:
                self.save()
        return removed

    def count(self):
        return len(self.data['entries'])

    def entries(self, month=None, kind=None, cat=None, query=''):
        """按条件取（月份 / 收支 / 分类 / 关键词），按日期倒序"""
        out = []
        month = norm_month(month) if month else ''
        q = (query or '').strip().lower()
        for e in self.data['entries']:
            if month and str(e.get('date', ''))[:7] != month:
                continue
            if kind and e.get('kind') != kind:
                continue
            if cat and e.get('cat') != cat:
                continue
            if q:
                blob = ('%s %s %s %s' % (e.get('note', ''), e.get('cat', ''),
                                         e.get('account', ''), e.get('amount', ''))).lower()
                if q not in blob:
                    continue
            out.append(e)
        out.sort(key=lambda x: (str(x.get('date', '')), str(x.get('created', '')), str(x.get('id', ''))),
                 reverse=True)
        return out

    def months(self):
        """出现过账目的月份，新的在前（当前月永远排第一，方便连着记）"""
        seen = {str(e.get('date', ''))[:7] for e in self.data['entries'] if e.get('date')}
        seen.add(today_str()[:7])
        return sorted([m for m in seen if re.match(r'^\d{4}-\d{2}$', m)], reverse=True)

    # ---------- 统计 ----------
    def stats(self, month=None):
        month = norm_month(month)
        exp = inc = 0.0
        n_exp = n_inc = 0
        days = set()
        for e in self.data['entries']:
            if str(e.get('date', ''))[:7] != month:
                continue
            days.add(str(e.get('date')))
            if e.get('kind') == 'income':
                inc += float(e.get('amount') or 0)
                n_inc += 1
            else:
                exp += float(e.get('amount') or 0)
                n_exp += 1
        d = {
            'month': month,
            'label': month_label(month),
            'expense': round(exp, 2),
            'income': round(inc, 2),
            'balance': round(inc - exp, 2),
            'count': n_exp + n_inc,
            'expense_count': n_exp,
            'income_count': n_inc,
            'days': len(days),
            'avg_per_day': round(exp / max(1, len(days)), 2) if days else 0.0,
        }
        return d

    def by_cat(self, month=None):
        """分类占比：[{cat, amount, pct, count}] 从多到少"""
        month = norm_month(month)
        agg = {}
        total = 0.0
        for e in self.data['entries']:
            if e.get('kind') == 'income':
                continue
            if str(e.get('date', ''))[:7] != month:
                continue
            cat = e.get('cat') or '其他'
            a = float(e.get('amount') or 0)
            agg.setdefault(cat, [0.0, 0])
            agg[cat][0] += a
            agg[cat][1] += 1
            total += a
        rows = [{'cat': c, 'amount': round(v[0], 2), 'count': v[1],
                 'pct': round(v[0] / total * 100, 1) if total else 0.0}
                for c, v in agg.items()]
        rows.sort(key=lambda r: -r['amount'])
        return rows

    def by_day(self, month=None):
        """按天汇总（柱状图用）：[(日期, 支出)]"""
        month = norm_month(month)
        agg = {}
        for e in self.data['entries']:
            if e.get('kind') == 'income':
                continue
            d = str(e.get('date', ''))
            if d[:7] != month:
                continue
            agg[d] = agg.get(d, 0.0) + float(e.get('amount') or 0)
        return sorted(((d, round(v, 2)) for d, v in agg.items()), key=lambda x: x[0])

    def monthly(self, n=6, end_month=None):
        """最近 n 个月：[(YYYY-MM, 支出, 收入)] 从旧到新"""
        end_month = norm_month(end_month)
        months = [shift_month(end_month, -(i)) for i in range(int(n) - 1, -1, -1)]
        out = []
        for m in months:
            s = self.stats(m)
            out.append((m, s['expense'], s['income']))
        return out

    def daily_budget_burn(self, month=None):
        """本月已用预算比例（有预算的分类里，花得最多那类的占比）"""
        states = [b for b in self.budget_state(month) if b['limit'] > 0]
        if not states:
            return None
        worst = max(states, key=lambda b: b['pct'])
        return worst

    # ---------- 预算 ----------
    def budgets(self):
        out = {}
        for k, v in (self.data.get('budgets') or {}).items():
            try:
                val = float(v)
            except Exception:
                continue
            if val > 0:
                out[str(k)] = round(val, 2)
        return out

    def set_budget(self, cat, limit):
        cat = str(cat or '').strip()
        if not cat:
            return False
        try:
            limit = round(float(limit), 2)
        except Exception:
            limit = 0.0
        with self._lock:
            if limit <= 0:
                (self.data.get('budgets') or {}).pop(cat, None)
            else:
                self.data.setdefault('budgets', {})[cat] = limit
            self.save()
        return True

    def budget_state(self, month=None):
        """每个有预算的分类：花多少、剩多少、超没超"""
        month = norm_month(month)
        spent = {r['cat']: r['amount'] for r in self.by_cat(month)}
        out = []
        for cat, limit in sorted(self.budgets().items()):
            s = spent.get(cat, 0.0)
            left = round(limit - s, 2)
            pct = round(s / limit * 100, 1) if limit else 0.0
            out.append({'cat': cat, 'limit': limit, 'spent': round(s, 2),
                        'left': left, 'pct': pct, 'over': left < 0})
        return out

    def over_budget(self, month=None):
        return [b for b in self.budget_state(month) if b['over']]

    def near_budget(self, month=None, pct=80.0):
        return [b for b in self.budget_state(month) if not b['over'] and b['pct'] >= pct]

    # ---------- 人话 ----------
    def summary_line(self, month=None):
        s = self.stats(month)
        if not s['count']:
            return '%s还没记账' % s['label']
        txt = '%s：支出 %s · 收入 %s · 结余 %s（%d 笔）' % (
            s['label'], fmt_amount(s['expense']), fmt_amount(s['income']),
            fmt_amount(s['balance']), s['count'])
        cats = self.by_cat(s['month'])[:3]
        if cats:
            txt += '｜最多：' + '、'.join('%s %s（%s%%）' % (c['cat'], fmt_amount(c['amount']), c['pct'])
                                        for c in cats)
        return txt

    def budget_line(self, month=None):
        month = norm_month(month)
        states = self.budget_state(month)
        if not states:
            return '还没设预算（面板里「预算」可以按分类设月度限额）'
        over = [b for b in states if b['over']]
        near = [b for b in states if (not b['over']) and b['pct'] >= 80]
        bits = []
        for b in over:
            bits.append('%s 超了 %s（花了 %s / 预算 %s）'
                        % (b['cat'], fmt_amount(-b['left']), fmt_amount(b['spent']),
                           fmt_amount(b['limit'])))
        for b in near:
            bits.append('%s 快到了（%s%%，剩 %s）'
                        % (b['cat'], ('%.0f' % b['pct']), fmt_amount(b['left'])))
        if not bits:
            left = sum(b['left'] for b in states)
            return '预算都还宽裕（合计还剩 %s）' % fmt_amount(left)
        return '；'.join(bits)

    # ---------- 导出 ----------
    def export_rows(self, month=None, include_header=True):
        month = month or ''
        rows = []
        if include_header:
            rows.append(['日期', '星期', '收支', '分类', '账户', '金额', '备注', '来源'])
        for e in sorted(self.entries(month if month else None),
                        key=lambda x: str(x.get('date', ''))):
            rows.append([str(e.get('date', '')), weekday_cn(e.get('date')),
                         kind_label(e.get('kind')), str(e.get('cat', '')),
                         str(e.get('account', '')), float(e.get('amount') or 0),
                         str(e.get('note', '')), str(e.get('src', ''))])
        return rows

    def to_csv(self, path, month=None):
        """导出 CSV（Excel 直接双击能开：带 UTF-8 BOM）"""
        import csv
        rows = self.export_rows(month)
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            for r in rows:
                w.writerow(r)
        return path

    def to_xlsx(self, path, month=None):
        """导出 xlsx（借 office_skill.sheet_write；它没装 WPS/Office 时抛错，调用方退回 CSV）"""
        import office_skill as os_
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        rows = self.export_rows(month)
        os_.sheet_write(path, rows=rows, sheet='记账')
        return path

    def export(self, path, month=None):
        """按后缀选：.xlsx → sheet_write，其它 → CSV。返回 (实际路径, 用了哪个引擎)"""
        low = str(path).lower()
        if low.endswith('.xlsx'):
            try:
                return self.to_xlsx(path, month), 'xlsx'
            except Exception:
                alt = os.path.splitext(path)[0] + '.csv'
                return self.to_csv(alt, month), 'csv'
        return self.to_csv(path, month), 'csv'

    # ---------- 给侧栏/卡片用的一行摘要 ----------
    def brief(self, month=None):
        month = norm_month(month)
        s = self.stats(month)
        rows = self.by_cat(month)
        states = self.budget_state(month)
        over = [b for b in states if b['over']]
        left_pos = sum(b['left'] for b in states if b['left'] > 0)
        overspend = -sum(b['left'] for b in states if b['left'] < 0)
        return {
            'month': month,
            'label': month_label(month),
            'expense': money(s['expense']),
            'income': money(s['income']),
            'balance': money(s['balance']),
            'balance_text': fmt_amount(s['balance'], sign=True),
            'count': s['count'],
            'top': ('%s %s' % (rows[0]['cat'], money(rows[0]['amount']))) if rows else '—',
            'top_pct': (rows[0]['pct'] if rows else 0),
            'budget_left': money(left_pos) if states else '',
            'over_amount': money(overspend) if overspend else '',
            'over': ('、'.join(b['cat'] for b in over) if over else ''),
            'over_n': len(over),
            'avg': money(s['avg_per_day']),
        }


def default_book():
    """默认账本（项目根 ledger.json）"""
    return LedgerBook()


def line_for_user(entry, book=None):
    """记完这一笔，回给用户的一句话（带上分类/日期，让他能马上看出认错没有）"""
    if not entry:
        return '这笔没记上（金额不对）'
    txt = '%s %s%s 记到「%s」' % (str(entry.get('date', ''))[5:], kind_label(entry.get('kind')),
                                 fmt_amount(entry.get('amount')), entry.get('cat'))
    if entry.get('note'):
        txt += '（%s）' % entry['note']
    if entry.get('account'):
        txt += ' · %s' % entry['account']
    return txt
