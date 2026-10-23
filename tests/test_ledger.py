# -*- coding: utf-8 -*-
"""记账（v6.89）：解析 + 账本 + 统计 + 预算 + 导出 + 界面

这里锁的是「认得出、算得对、导得出、画得出来」，不联网、不动用户真实数据：
1. 分类表：支出/收入两套、图标齐全、`cats_for` 按收支切换
2. 日期/金额归一：今天/昨天/前天/3 号/上月 3 号/周三/12月1日、32 / 32.5 / 1.2万 / 32 块
3. 本地解析：账户、类别、收支方向（发红包=支出、收红包=收入）、备注不残留「花了」
4. 账本：非法金额不收、改/删/查、按月筛选、月份列表
5. 统计：支出/收入/结余/日均、分类占比、按天、近 6 月
6. 预算：设限额 → 花了多少剩多少、超支/快到线、人话摘要
7. 导出：CSV 带 BOM（Excel 双击不乱码）；xlsx 装不了就退回 CSV
8. 健壮性：坏 json 改名 .broken-* 后当空账本；老文件缺键自动补齐
9. 界面：面板能开、图表能喂数据、明细表行数对、记一笔对话框分类随收支切换
10. 主题：界面用到的 token 全都存在；产品代码里没有硬编码颜色

运行：python -m pytest tests/test_ledger.py -q
"""
import json
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """配置/账本都落临时目录，别把真 config.json 写脏"""
    import pet_ledger as lg
    monkeypatch.setattr(lg, 'CONFIG_PATH', str(tmp_path / 'config.json'), raising=False)
    monkeypatch.setattr(lg, 'LEDGER_PATH', str(tmp_path / 'ledger.json'), raising=False)
    yield


@pytest.fixture
def book(tmp_path):
    import pet_ledger as lg
    return lg.LedgerBook(str(tmp_path / 'ledger.json'))


@pytest.fixture
def today():
    import pet_ledger as lg
    return lg.today_str()


def _add(book, **kw):
    base = {'date': '2026-10-10', 'amount': 10.0, 'kind': 'expense', 'cat': '餐饮',
            'account': '微信', 'note': '午饭'}
    base.update(kw)
    return book.add(base)


# --------------------------------------------------------------------------
# 1. 分类表
# --------------------------------------------------------------------------
def test_cat_tables_not_empty():
    import pet_ledger as lg
    assert lg.CATS and lg.INCOME_CATS
    assert '餐饮' in lg.CATS and '工资' in lg.INCOME_CATS
    assert '其他' in lg.CATS and '其他收入' in lg.INCOME_CATS


def test_every_cat_has_icon():
    import pet_ledger as lg
    for c in lg.CATS + lg.INCOME_CATS:
        assert lg.cat_icon(c), c


def test_cats_for_switches_by_kind(book):
    exp = book.cats_for('expense')
    inc = book.cats_for('income')
    assert '餐饮' in exp and '工资' not in exp
    assert '工资' in inc and '餐饮' not in inc


def test_custom_cat_roundtrip(book):
    assert book.add_cat('宠物') is True
    assert '宠物' in book.cats()
    assert book.add_cat('宠物') is False          # 重复加不收
    assert '宠物' in book.reload().cats()          # 落盘了


# --------------------------------------------------------------------------
# 2. 日期 / 金额
# --------------------------------------------------------------------------
@pytest.mark.parametrize('raw,want', [
    ('2026-10-21', '2026-10-21'),
    ('2026/10/21', '2026-10-21'),
    ('2026年10月21日', '2026-10-21'),
    ('20261021', '2026-10-21'),
    ('', ''),
    ('随便', ''),
    ('2026-02-30', ''),        # 不存在的日期不瞎补
])
def test_norm_date(raw, want):
    import pet_ledger as lg
    assert lg.norm_date(raw) == want


def test_norm_month_and_shift():
    import pet_ledger as lg
    assert lg.norm_month('2026-10') == '2026-10'
    assert lg.norm_month('2026/10') == '2026-10'
    assert lg.norm_month('2026-10-21') == '2026-10'
    assert lg.shift_month('2026-01', -1) == '2025-12'
    assert lg.shift_month('2026-12', 1) == '2027-01'
    assert lg.month_label('2026-10') == '2026年10月'


def test_days_in_month_and_weekday():
    import pet_ledger as lg
    assert lg.days_in_month('2026-02') == 28
    assert lg.days_in_month('2024-02') == 29
    assert lg.weekday_cn('2026-10-21')            # 有值就行，具体星期几随年份
    assert lg.days_between('2026-10-01', '2026-10-21') == 20


@pytest.mark.parametrize('v,want', [
    (32, '¥32'), (32.5, '¥32.50'), (0, '¥0'), (1234.5, '¥1,234.50'), (12000, '¥12,000'),
])
def test_fmt_amount(v, want):
    import pet_ledger as lg
    assert lg.fmt_amount(v) == want


# --------------------------------------------------------------------------
# 3. 本地解析
# --------------------------------------------------------------------------
def test_parse_basic(today):
    import pet_ledger as lg
    r = lg.parse_entry('午饭 32')
    assert r['ok'] and r['amount'] == 32.0
    assert r['cat'] == '餐饮' and r['kind'] == 'expense'
    assert r['date'] == today


def test_parse_yesterday_account(today):
    import pet_ledger as lg
    r = lg.parse_entry('昨天晚饭 85 微信')
    assert r['ok'] and r['amount'] == 85.0
    assert r['account'] == '微信'
    assert r['date'] < today                     # 昨天 < 今天


def test_parse_decimal_and_yuan_word():
    import pet_ledger as lg
    assert lg.parse_entry('打车18.5')['amount'] == 18.5
    assert lg.parse_entry('买菜 32 块')['amount'] == 32.0
    assert lg.parse_entry('买菜 32元')['amount'] == 32.0


def test_parse_wan():
    import pet_ledger as lg
    assert lg.parse_entry('年终奖 1.2万')['amount'] == 12000.0


def test_parse_day_of_month_and_prev_month(today):
    import pet_ledger as lg
    r = lg.parse_entry('上月 3 号 交电费 156')
    assert r['amount'] == 156.0 and r['cat'] == '居住'
    assert r['date'].endswith('-03')
    assert r['date'][:7] == lg.shift_month(today[:7], -1)


def test_parse_weekday():
    import pet_ledger as lg
    r = lg.parse_entry('周三看电影 45 现金')
    assert r['amount'] == 45.0 and r['cat'] == '娱乐' and r['account'] == '现金'


def test_parse_days_ago(today):
    import pet_ledger as lg
    r = lg.parse_entry('前天看病 320')
    assert r['amount'] == 320.0 and r['cat'] == '医疗'
    assert r['date'] < today


def test_parse_income_direction():
    import pet_ledger as lg
    r = lg.parse_entry('发工资 12000')
    assert r['kind'] == 'income' and r['cat'] == '工资'
    r = lg.parse_entry('收到红包 200')
    assert r['kind'] == 'income' and r['cat'] == '红包'
    r = lg.parse_entry('报销打车费 88')
    assert r['kind'] == 'income' and r['cat'] == '报销'


def test_parse_expense_red_packet():
    """「给妈发红包」是花出去的，不能记成收入"""
    import pet_ledger as lg
    r = lg.parse_entry('给妈发红包 500')
    assert r['kind'] == 'expense' and r['cat'] == '人情'


def test_parse_note_has_no_filler():
    import pet_ledger as lg
    r = lg.parse_entry('超市 一次 花了 268.42')
    assert r['amount'] == 268.42 and r['cat'] == '日用'
    assert '花了' not in r['note'] and '一次' not in r['note']


def test_parse_missing_amount():
    import pet_ledger as lg
    r = lg.parse_entry('今天吃了顿好的')
    assert r['ok'] is False
    assert r['missing'] == ['金额']


def test_parse_empty():
    import pet_ledger as lg
    r = lg.parse_entry('   ')
    assert r['ok'] is False and r['missing']


def test_parse_remember_account_from_cfg():
    import pet_ledger as lg
    lg.cfg_write(lg.CFG_ACCOUNT, '支付宝')
    assert lg.parse_entry('奶茶 18')['account'] == '支付宝'
    assert lg.cfg_delete(lg.CFG_ACCOUNT) is True
    assert lg.cfg_text(lg.CFG_ACCOUNT, '') == ''


def test_money_and_labels():
    import pet_ledger as lg
    assert lg.money(12000) == '12,000'
    assert lg.kind_label('income') == '收入'
    assert lg.kind_label('expense') == '支出'


# --------------------------------------------------------------------------
# 4. 账本增删改查
# --------------------------------------------------------------------------
def test_add_entry_fields(book):
    e = _add(book)
    assert e['id'] and e['amount'] == 10.0
    assert e['created'] and e['src'] == 'manual'
    assert book.count() == 1


def test_reject_bad_amount(book):
    assert _add(book, amount=0) is None
    assert _add(book, amount=-5) is None
    assert _add(book, amount='abc') is None
    assert book.count() == 0


def test_update_entry(book):
    e = _add(book)
    got = book.update(e['id'], amount=88.5, note='改了')
    assert got['amount'] == 88.5 and got['note'] == '改了'
    assert book.get(e['id'])['amount'] == 88.5
    assert book.update('nope', amount=1) is None


def test_delete_entry(book):
    e = _add(book)
    assert book.delete(e['id']) is True
    assert book.delete(e['id']) is False
    assert book.count() == 0


def test_entries_filter_and_sort(book):
    _add(book, date='2026-10-01', amount=1)
    _add(book, date='2026-10-20', amount=2, kind='income', cat='工资')
    _add(book, date='2026-09-20', amount=3)
    assert len(book.entries('2026-10')) == 2
    assert len(book.entries('2026-10', kind='income')) == 1
    assert len(book.entries('2026-10', cat='餐饮')) == 1
    rows = book.entries('2026-10')
    assert rows[0]['date'] == '2026-10-20'        # 日期倒序
    assert len(book.entries(query='工资')) == 1


def test_months_list(book, today):
    _add(book, date='2026-09-05')
    _add(book, date='2026-10-05')
    ms = book.months()
    assert '2026-09' in ms and '2026-10' in ms and today[:7] in ms
    assert ms == sorted(ms, reverse=True)


def test_clear_month(book):
    _add(book, date='2026-10-01')
    _add(book, date='2026-10-02')
    _add(book, date='2026-09-01')
    assert book.clear_month('2026-10') == 2
    assert book.count() == 1


# --------------------------------------------------------------------------
# 5. 统计
# --------------------------------------------------------------------------
def test_stats(book):
    _add(book, date='2026-10-01', amount=100)
    _add(book, date='2026-10-02', amount=50.5)
    _add(book, date='2026-10-02', amount=1000, kind='income', cat='工资')
    s = book.stats('2026-10')
    assert s['expense'] == 150.5
    assert s['income'] == 1000.0
    assert s['balance'] == 849.5
    assert s['count'] == 3
    assert s['days'] == 2
    assert s['avg_per_day'] == 75.25


def test_by_cat(book):
    _add(book, cat='餐饮', amount=60)
    _add(book, cat='餐饮', amount=40)
    _add(book, cat='交通', amount=100)
    rows = book.by_cat('2026-10')
    by = {r['cat']: r for r in rows}
    assert by['餐饮']['amount'] == 100.0 and by['餐饮']['count'] == 2
    assert by['餐饮']['pct'] == 50.0
    assert rows[0]['amount'] >= rows[-1]['amount']    # 从多到少


def test_by_cat_excludes_income(book):
    _add(book, cat='餐饮', amount=10)
    _add(book, kind='income', cat='工资', amount=9999)
    assert [r['cat'] for r in book.by_cat('2026-10')] == ['餐饮']


def test_by_day_and_monthly(book):
    _add(book, date='2026-10-01', amount=5)
    _add(book, date='2026-10-01', amount=6)
    _add(book, date='2026-10-03', amount=7)
    days = book.by_day('2026-10')
    assert days[0] == ('2026-10-01', 11.0) and len(days) == 2
    series = book.monthly(6, end_month='2026-10')
    assert len(series) == 6
    assert series[-1][0] == '2026-10' and series[0][0] == '2026-05'


# --------------------------------------------------------------------------
# 6. 预算
# --------------------------------------------------------------------------
def test_set_budget_and_state(book):
    _add(book, cat='餐饮', amount=80)
    book.set_budget('餐饮', 100)
    st = book.budget_state('2026-10')
    assert len(st) == 1
    b = st[0]
    assert b['limit'] == 100 and b['spent'] == 80 and b['left'] == 20
    assert b['over'] is False and b['pct'] == 80.0


def test_budget_over_and_clear(book):
    _add(book, cat='餐饮', amount=150)
    book.set_budget('餐饮', 100)
    over = book.over_budget('2026-10')
    assert len(over) == 1 and over[0]['left'] == -50
    assert book.near_budget('2026-10') == []
    book.set_budget('餐饮', 0)                 # 0 = 取消这个分类的预算
    assert book.budgets() == {}


def test_near_budget(book):
    _add(book, cat='餐饮', amount=90)
    book.set_budget('餐饮', 100)
    assert [b['cat'] for b in book.near_budget('2026-10', 85)] == ['餐饮']
    assert book.near_budget('2026-10', 95) == []


def test_budget_line_and_summary_line(book):
    _add(book, cat='餐饮', amount=150)
    book.set_budget('餐饮', 100)
    assert '超了' in book.budget_line('2026-10')
    assert '餐饮' in book.budget_line('2026-10')
    line = book.summary_line('2026-10')
    assert '支出' in line and '收入' in line
    assert '餐饮' in line


def test_brief_fields(book):
    _add(book, cat='餐饮', amount=30)
    book.set_budget('餐饮', 100)
    b = book.brief('2026-10')
    for k in ('month', 'label', 'expense', 'income', 'balance', 'count', 'top',
              'budget_left', 'over', 'over_n', 'avg'):
        assert k in b
    assert b['over'] == '' and b['over_n'] == 0
    assert b['top'].startswith('餐饮')


# --------------------------------------------------------------------------
# 7. 导出
# --------------------------------------------------------------------------
def test_export_rows_header(book):
    _add(book, date='2026-10-02', amount=12.5, note='咖啡')
    rows = book.export_rows('2026-10')
    assert rows[0][:3] == ['日期', '星期', '收支']
    assert len(rows) == 2
    assert rows[1][0] == '2026-10-02' and rows[1][5] == 12.5


def test_to_csv_bom(book, tmp_path):
    _add(book, note='中文备注')
    p = book.to_csv(str(tmp_path / 'a.csv'), '2026-10')
    raw = open(p, 'rb').read()
    assert raw[:3] == b'\xef\xbb\xbf'                  # BOM：Excel 双击不乱码
    assert '中文备注' in raw.decode('utf-8-sig')


def test_export_falls_back_to_csv(book, tmp_path, monkeypatch):
    """没装 WPS/Office 时 sheet_write 会抛错 → 自动存 CSV，不让用户白点一次"""
    import office_skill
    monkeypatch.setattr(office_skill, 'sheet_write',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('没有 Excel')), raising=True)
    path, engine = book.export(str(tmp_path / 'b.xlsx'), '2026-10')
    assert engine == 'csv' and path.endswith('.csv')


def test_export_csv_path(book, tmp_path):
    path, engine = book.export(str(tmp_path / 'c.csv'), '2026-10')
    assert engine == 'csv' and os.path.isfile(path)


# --------------------------------------------------------------------------
# 8. 健壮性
# --------------------------------------------------------------------------
def test_broken_json_renamed(tmp_path):
    import pet_ledger as lg
    p = tmp_path / 'ledger.json'
    p.write_text('{这不是 json', encoding='utf-8')
    b = lg.LedgerBook(str(p))
    assert b.count() == 0
    assert any(f.name.startswith('ledger.json.broken-') for f in tmp_path.iterdir())


def test_old_file_missing_keys(tmp_path):
    import pet_ledger as lg
    p = tmp_path / 'ledger.json'
    p.write_text(json.dumps({'entries': []}, ensure_ascii=False), encoding='utf-8')
    b = lg.LedgerBook(str(p))
    assert b.data['budgets'] == {} and b.data['cats'] == []
    assert b.count() == 0


def test_save_is_atomic_and_readable(book, tmp_path):
    _add(book, note='原子写')
    p = str(tmp_path / 'ledger.json')
    data = json.load(open(p, encoding='utf-8'))
    assert data['version'] == 1 and len(data['entries']) == 1
    assert not os.path.exists(p + '.tmp')


def test_line_for_user(book):
    import pet_ledger as lg
    e = _add(book, amount=32, date='2026-10-11', note='午饭', account='微信')
    line = lg.line_for_user(e)
    assert '¥32' in line and '餐饮' in line and '微信' in line
    assert lg.line_for_user(None)


# --------------------------------------------------------------------------
# 9. 界面（offscreen）
# --------------------------------------------------------------------------
@pytest.fixture(scope='module')
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_panel_builds_and_refreshes(qapp, book):
    import pet_ledger as lg
    import pet_ledger_ui as ui
    _add(book, cat='餐饮', amount=60, date='2026-10-05')
    _add(book, cat='交通', amount=20, date='2026-10-06')
    book.set_budget('餐饮', 100)
    ctrl = ui.LedgerController(book=book)
    p = ctrl.panel()
    p.goto('2026-10')
    assert p.table.rowCount() == 2
    assert '支出' in p.lb_summary.text() or '餐饮' in p.lb_summary.text()
    assert p.budget_bars._rows and p.budget_bars._rows[0]['cat'] == '餐饮'
    p.close()


def test_panel_filters(qapp, book):
    import pet_ledger_ui as ui
    _add(book, cat='餐饮', amount=60, date='2026-10-05', note='火锅')
    _add(book, kind='income', cat='工资', amount=8000, date='2026-10-06', note='十月工资')
    ctrl = ui.LedgerController(book=book)
    p = ctrl.panel()
    p.goto('2026-10')
    assert p.table.rowCount() == 2
    p.cb_kind.setCurrentIndex(1)                 # 只看支出
    assert p.table.rowCount() == 1
    p.cb_kind.setCurrentIndex(0)
    p.ed_search.setText('工资')
    p.refresh_table()
    assert p.table.rowCount() == 1
    p.close()


def test_entry_dialog_switches_cats(qapp, book):
    import pet_ledger_ui as ui
    d = ui.EntryDialog(book)
    d.cb_kind.setCurrentIndex(1)                 # 收入
    texts = [d.cb_cat.itemText(i) for i in range(d.cb_cat.count())]
    assert any('工资' in t for t in texts)
    d.cb_kind.setCurrentIndex(0)
    texts = [d.cb_cat.itemText(i) for i in range(d.cb_cat.count())]
    assert any('餐饮' in t for t in texts)
    d.sp_amount.setValue(12.5)
    d.cb_cat.setCurrentIndex(0)
    v = d.value()
    assert v['amount'] == 12.5 and v['kind'] == 'expense'
    d.close()


def test_entry_dialog_loads_existing(qapp, book):
    import pet_ledger as lg
    import pet_ledger_ui as ui
    r = lg.parse_entry('昨天晚饭 85 微信')
    d = ui.EntryDialog(book, r)
    v = d.value()
    assert v['amount'] == 85.0 and v['account'] == '微信' and v['cat'] == '餐饮'
    d.close()


def test_budget_dialog_roundtrip(qapp, book):
    import pet_ledger_ui as ui
    book.set_budget('餐饮', 500)
    d = ui.BudgetDialog(book, '2026-10')
    assert d._spins['餐饮'].value() == 500
    d._spins['餐饮'].setValue(300)
    d._spins['交通'].setValue(80)
    out = d.result_budgets()
    assert out['餐饮'] == 300 and out['交通'] == 80
    d.close()


def test_controller_add_text_and_notice(qapp, book):
    import pet_ledger_ui as ui
    ctrl = ui.LedgerController(book=book)
    got = []
    ctrl.notice.connect(got.append)
    ok, reply = ctrl.add_text('午饭 32')
    assert ok and '¥32' in reply
    assert book.count() == 1
    ok2, reply2 = ctrl.add_text('今天心情不错')
    assert ok2 is False and '金额' in reply2
    assert book.count() == 1


def test_controller_budget_tail(qapp, book):
    import pet_ledger_ui as ui
    book.set_budget('餐饮', 50)
    ctrl = ui.LedgerController(book=book)
    ok, reply = ctrl.add_text('午饭 80')
    assert ok and '超预算' in reply
    assert '餐饮' in ctrl.check_budget()


def test_chart_widgets_paint(qapp, book):
    """图表拿到数据能画（paintEvent 不炸）；空数据也要能画"""
    from PySide6.QtGui import QPixmap
    import pet_ledger_ui as ui
    rows = [{'cat': '餐饮', 'amount': 60, 'pct': 60.0, 'count': 2}]
    d = ui.DonutChart()
    b = ui.BarChart()
    bb = ui.BudgetBars()
    for w in (d, b, bb):
        w.resize(360, 180)
        pm = QPixmap(360, 180)
        w.render(pm)                                # 空数据先画一遍
    d.set_data(rows, 100.0)
    b.set_data([('2026-09', 100.0, 0.0), ('2026-10', 60.0, 800.0)])
    bb.set_data([{'cat': '餐饮', 'limit': 100, 'spent': 60, 'left': 40, 'pct': 60, 'over': False},
                 {'cat': '交通', 'limit': 50, 'spent': 90, 'left': -40, 'pct': 180, 'over': True}])
    for w in (d, b, bb):
        pm = QPixmap(360, 180)
        w.render(pm)


# --------------------------------------------------------------------------
# 10. 主题 / 颜色护栏
# --------------------------------------------------------------------------
def test_ledger_tokens_exist():
    import pet_ledger_ui as ui
    import pet_theme
    for k in ui.LEDGER_TOKENS + ui.SLICE_TOKENS:
        assert pet_theme.color(k, '') != '', '缺 token: %s' % k


def test_no_hardcoded_colors_in_new_modules():
    """新模块里不许出现 #rrggbb / rgba( 字面量（颜色只能走 token）"""
    import re
    hex_re = re.compile(r'#[0-9a-fA-F]{3,8}\b')
    rgba_re = re.compile(r'\brgba?\s*\(')
    for name in ('pet_ledger.py', 'pet_ledger_ui.py'):
        src = open(os.path.join(BASE, name), encoding='utf-8').read()
        lines = [ln for ln in src.splitlines() if 'theme-exempt' not in ln]
        body = '\n'.join(lines)
        assert not hex_re.search(body), '%s 里有硬编码 #rrggbb' % name
        assert not rgba_re.search(body), '%s 里有硬编码 rgba(' % name
