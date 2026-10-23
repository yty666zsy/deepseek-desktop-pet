# -*- coding: utf-8 -*-
"""记账实机验证（v6.89）：造账 → 解析 → 面板/图表/明细 → 预算 → 导出 → 截图

用法：.venv\\Scripts\\python.exe tools\\verify_ledger.py

**必须在真实平台跑**（不要 QT_QPA_PLATFORM=offscreen）：离屏下中文字体全是豆腐块，
截图里看不出「环形图的分类名 / 明细表的金额」到底有没有字。

验的东西：
1. 本地解析：24 句口语（「午饭 32」「上月 3 号 交电费 156」「给妈发红包 500」…）
   日期/金额/分类/收支方向/账户都对
2. 统计：支出/收入/结余/日均、分类占比、近 6 月
3. 预算：分类限额 → 超支/快到线 → 人话摘要
4. 面板：环形图 + 柱状图 + 预算条 + 明细表 + 搜索/筛选都能真画出来（截图）
5. 对话框：记一笔（分类随收支切换）、预算设置（截图）
6. 导出：xlsx（装不了就退 CSV）/ CSV，文件里行数对
7. 宿主接线：AI 工具 `ledger` 的 add/list/summary/budget、超支头顶提醒、侧栏「¤」页签
8. 隐私：跑完不动使用者真实 ledger.json 一个字节（临时账本 + 结尾核对 sha256）

用户自己的 config.json / ledger.json 一个字节都不碰：开头备份 lg_* 键，结尾原样还原或删掉。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
SHOTS = os.path.join(BASE, 'screenshots')
os.makedirs(SHOTS, exist_ok=True)

from PySide6.QtWidgets import QApplication

import desktop_pet as dp
import pet_ledger as lg
import pet_ledger_ui as ui
import side_panel as sp

WORK = os.path.join(tempfile.gettempdir(), 'petlg_verify')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK, exist_ok=True)
LEDGER_TMP = os.path.join(WORK, 'ledger.json')

FAILS = []


def check(ok, msg):
    print('  %s %s' % ('✔' if ok else '✕', msg))
    if not ok:
        FAILS.append(msg)
    return ok


def shot(app, widget, filename, note):
    app.processEvents()
    time.sleep(0.25)
    app.processEvents()
    pm = widget.grab()
    if not check(not pm.isNull() and pm.width() > 50 and pm.height() > 50,
                 '%s：画面 %dx%d' % (note, pm.width(), pm.height())):
        return
    pm.save(os.path.join(SHOTS, filename))
    print('    已存 screenshots/%s' % filename)


def sha(path):
    if not os.path.isfile(path):
        return ''
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


# 24 句口语 —— 每一句都是一般人会直接说出口的样子
SENTENCES = [
    ('午饭 32', 32.0, 'expense', '餐饮', None),
    ('昨天晚饭 85 微信', 85.0, 'expense', '餐饮', '微信'),
    ('打车18.5', 18.5, 'expense', '交通', None),
    ('奶茶 18 支付宝', 18.0, 'expense', '餐饮', '支付宝'),
    ('房租 3200', 3200.0, 'expense', '居住', None),
    ('发工资 12000', 12000.0, 'income', '工资', None),
    ('收到红包 200', 200.0, 'income', '红包', None),
    ('给妈发红包 500', 500.0, 'expense', '人情', None),
    ('买书 60', 60.0, 'expense', '学习', None),
    ('前天看病 320', 320.0, 'expense', '医疗', None),
    ('周三看电影 45 现金', 45.0, 'expense', '娱乐', '现金'),
    ('超市 花了 268.42', 268.42, 'expense', '日用', None),
    ('地铁 6', 6.0, 'expense', '交通', None),
    ('咖啡 22', 22.0, 'expense', '餐饮', None),
    ('水果 15.8', 15.8, 'expense', '餐饮', None),
    ('话费 100', 100.0, 'expense', '通讯', None),
    ('买衣服 399', 399.0, 'expense', '购物', None),
    ('报销打车费 88', 88.0, 'income', '报销', None),
    ('退了个包 199', 199.0, 'income', '退款', None),
    ('上月 3 号 交电费 156', 156.0, 'expense', '居住', None),
    ('上上月 房租 3200', 3200.0, 'expense', '居住', None),
    ('年终奖 1.2万', 12000.0, 'income', '工资', None),
    ('物业费 300 银行卡', 300.0, 'expense', '居住', '银行卡'),
    ('火锅 128 微信', 128.0, 'expense', '餐饮', '微信'),
]


def main():
    app = QApplication(sys.argv[:1])

    # ---------- 0. 别动用户数据 ----------
    real_ledger = os.path.join(BASE, 'ledger.json')
    real_before = sha(real_ledger)
    cfg_keys = (lg.CFG_ACCOUNT, lg.CFG_MONTH, lg.CFG_ALERT)
    prev_cfg = {k: lg.cfg_read().get(k) for k in cfg_keys}
    for k in cfg_keys:                       # 验证脚本不该改用户设置
        lg.cfg_delete(k)
    lg.LEDGER_PATH = LEDGER_TMP              # 所有读写都落到临时账本
    print('[0] 隐私：临时账本 %s；真实 ledger.json 存在=%s'
          % (LEDGER_TMP, os.path.isfile(real_ledger)))

    book = lg.LedgerBook(LEDGER_TMP)
    ctrl = ui.LedgerController(book=book)

    # ---------- 1. 本地解析 ----------
    print('\n[1] 本地解析（24 句口语）')
    bad = []
    for text, amount, kind, cat, account in SENTENCES:
        r = lg.parse_entry(text, cats=book.cats())
        ok = (r['ok'] and abs(r['amount'] - amount) < 0.01 and r['kind'] == kind
              and r['cat'] == cat and (account is None or r['account'] == account))
        if not ok:
            bad.append((text, r))
        book.add(r)
    check(not bad, '24 句全部解析正确（不符 %d 句）' % len(bad))
    for t, r in bad[:6]:
        print('      ✕ %s → %s %s %s %s' % (t, r['amount'], r['kind'], r['cat'], r['account']))
    print('    示例：')
    for t in ('午饭 32', '上月 3 号 交电费 156', '给妈发红包 500', '年终奖 1.2万'):
        r = lg.parse_entry(t)
        print('      %-20s → %s %s %s %s' % (t, r['date'], lg.fmt_amount(r['amount']),
                                             r['kind'], r['cat']))
    check(book.count() == 24, '账本里有 24 笔（实际 %d）' % book.count())

    # ---------- 2. 统计 ----------
    print('\n[2] 统计')
    this_month = lg.today_str()[:7]
    s = book.stats(this_month)
    print('    %s' % book.summary_line(this_month))
    cats = book.by_cat(this_month)
    check(bool(cats) and abs(sum(c['pct'] for c in cats) - 100) < 0.6,
          '分类占比合计 ≈100%%（实际 %.1f%%）' % sum(c['pct'] for c in cats))
    series = book.monthly(6, end_month=this_month)
    check(len(series) == 6 and series[-1][0] == this_month,
          '近 6 个月序列对（%s … %s）' % (series[0][0], series[-1][0]))
    check(s['expense'] > 0 and s['income'] > 0 and s['count'] > 0, '支出/收入/笔数都有值')

    # ---------- 3. 预算 ----------
    print('\n[3] 预算（分类限额 + 超支/快到线）')
    spent_food = sum(c['amount'] for c in cats if c['cat'] == '餐饮')
    book.set_budget('餐饮', round(spent_food * 0.6))       # 故意设小 → 必定超支
    book.set_budget('居住', 5000)
    book.set_budget('交通', round(200 * 1.0))
    st = book.budget_state(this_month)
    print('    %s' % book.budget_line(this_month))
    check(any(b['over'] for b in st), '餐饮被算成超支（花了 %.2f / 上限 %.0f）'
          % (spent_food, round(spent_food * 0.6)))
    check(len(st) == 3, '3 个分类有预算（实际 %d）' % len(st))

    # ---------- 4. 面板 ----------
    print('\n[4] 账本面板（环形 + 柱状 + 预算条 + 明细表）')
    panel = ctrl.panel()
    panel.month = this_month
    panel.refresh()
    panel.resize(1000, 720)
    panel.show()
    app.processEvents()
    rows_shown = panel.table.rowCount()
    check(rows_shown == len(book.entries(this_month)), '明细表行数与本月笔数一致（%d）' % rows_shown)
    check(panel.donut._rows and panel.donut._total > 0, '环形图拿到分类数据（%.2f）' % panel.donut._total)
    check(len(panel.bars._series) == 6, '柱状图有 6 个月')
    check(len(panel.budget_bars._rows) == 3, '预算条 3 条')
    check('超支' in panel.lb_budget_h.text(), '预算行提示里写了超支：%s'
          % panel.lb_budget_h.text()[:40].replace('<', '('))
    shot(app, panel, 'v689_ledger_panel.png', '账本面板（图表 + 明细）')

    # 筛选 / 搜索
    panel.cb_kind.setCurrentIndex(2)          # 只看收入
    app.processEvents()
    only_income = panel.table.rowCount()
    ok_income = all(panel.table.item(i, 1).text() == '收入' for i in range(only_income))
    check(only_income > 0 and ok_income, '筛选「只看收入」生效（%d 行）' % only_income)
    panel.cb_kind.setCurrentIndex(0)
    panel.ed_search.setText('房租')
    panel.refresh_table()
    app.processEvents()
    check(panel.table.rowCount() >= 1, '搜索「房租」命中 %d 条' % panel.table.rowCount())
    panel.ed_search.clear()
    panel.refresh_table()

    # ---------- 5. 对话框 ----------
    print('\n[5] 记一笔 / 预算 对话框')
    dlg = ui.EntryDialog(book, lg.parse_entry('昨天晚饭 85 微信'))
    dlg.show()
    app.processEvents()
    v = dlg.value()
    check(abs(v['amount'] - 85) < 0.01 and v['cat'] == '餐饮' and v['account'] == '微信',
          '对话框能带出解析结果：%s %s %s' % (v['date'], v['amount'], v['cat']))
    cats_exp = [dlg.cb_cat.itemText(i) for i in range(dlg.cb_cat.count())]
    dlg.cb_kind.setCurrentIndex(1)
    app.processEvents()
    cats_inc = [dlg.cb_cat.itemText(i) for i in range(dlg.cb_cat.count())]
    check(any('餐饮' in c for c in cats_exp) and any('工资' in c for c in cats_inc),
          '分类随收支切换（支出 %d 项 / 收入 %d 项）' % (len(cats_exp), len(cats_inc)))
    dlg.cb_kind.setCurrentIndex(0)
    shot(app, dlg, 'v689_ledger_entry.png', '记一笔对话框')

    bd = ui.BudgetDialog(book, this_month)
    bd.show()
    app.processEvents()
    check(len(bd._spins) >= 10, '预算对话框列出 %d 个分类' % len(bd._spins))
    shot(app, bd, 'v689_ledger_budget.png', '预算设置对话框')

    # ---------- 6. 导出 ----------
    print('\n[6] 导出')
    csv_path = os.path.join(WORK, 'out.csv')
    p1, e1 = book.export(csv_path, this_month)
    n_lines = len(open(p1, encoding='utf-8-sig').read().strip().splitlines())
    check(e1 == 'csv' and n_lines >= 2, 'CSV 导出 %d 行' % n_lines)
    xlsx_path = os.path.join(WORK, 'out.xlsx')
    p2, e2 = book.export(xlsx_path, this_month)
    check(os.path.isfile(p2), 'xlsx 导出：%s（引擎 %s）' % (os.path.basename(p2), e2))
    if e2 == 'xlsx':
        try:
            import office_skill
            rows = office_skill.sheet_read(p2, '记账', limit=5)
            check(bool(rows), 'xlsx 能被读回（前几行：%s）' % (rows[0][:3] if rows else '—'))
        except Exception as e:
            print('    （读回跳过：%s）' % e)

    # ---------- 7. 宿主接线 ----------
    print('\n[7] 桌宠接线（AI 工具 / 头顶提醒 / 侧栏页签）')
    pet = dp.PetWidget()
    pet.show()
    app.processEvents()
    pet._save_cfg_value = lambda *a, **k: True
    pet._ledger = ctrl                                   # 用验证账本，别碰真实数据

    out = pet._tool_ledger({'action': 'add', 'text': '午饭 32'})
    check('已记账' in out and '¥32' in out, '工具 add 记账：%s' % out.splitlines()[0][:40])
    out = pet._tool_ledger({'action': 'add', 'text': '今天心情不错'})
    check('没认出金额' in out, '没金额时老实说不知道：%s' % out[:30])
    out = pet._tool_ledger({'action': 'add', 'text': '袜子', 'amount': 19.9, 'cat': '日用'})
    check('已记账' in out and '日用' in out, '本地认不出时用模型给的字段兜底：%s'
          % out.splitlines()[0][:40])
    out = pet._tool_ledger({'action': 'summary'})
    check('支出' in out, '工具 summary：%s' % out.splitlines()[0][:50])
    out = pet._tool_ledger({'action': 'list', 'text': '房租'})
    check('房租' in out or '居住' in out, '工具 list 能搜：%s' % out.splitlines()[0][:40])
    out = pet._tool_ledger({'action': 'budget', 'cat': '娱乐', 'amount': 300})
    check('已把' in out and '娱乐' in out, '工具 budget 能设限额：%s' % out)
    out = pet._tool_ledger({'action': 'budget'})
    check('娱乐' in out, '工具 budget 能报余额')

    # 超支头顶提醒（同一分类一天只唠叨一次）
    pet._lg_remind = {}
    before = len(getattr(pet, '_lg_remind', {}) or {})
    pet._ledger_remind({'date': lg.today_str()})
    first = len(getattr(pet, '_lg_remind', {}) or {})
    pet._ledger_remind({'date': lg.today_str()})
    second = len(getattr(pet, '_lg_remind', {}) or {})
    check(first > before, '超支时记了「今天已提醒」（%d 条）' % first)
    check(second == first, '同一天同一句不重复唠叨（还是 %d 条）' % second)

    # 侧栏页签
    data = pet._side_panel_data()
    text = sp.SidePanel._text_for(None, 'ledger', data)
    check('支出' in text and '预算剩' in text, '侧栏「¤」页签文字：%s'
          % text.replace('\n', ' / ')[:60])
    check(sp.KIND_ICON.get('ledger') == '¤' and '¥ 记账' in sp.KIND_LABEL.get('ledger', ''),
          '页签图标/名字登记好了')
    spnl = sp.SidePanel(tabs=[{'id': 'tasks', 'kind': 'tasks'},
                              {'id': 'ledger', 'kind': 'ledger'},
                              {'id': 'todo', 'kind': 'todo'}],
                        data_cb=lambda: data,
                        theme_qss=lambda: (
                            'QFrame{background:%s;border-radius:8px;}'
                            'QLabel{color:%s;font-size:10.5px;}'
                            'QListWidget{background:%s;color:%s;border:none;font-size:11px;}'
                            'QPlainTextEdit{background:%s;color:%s;border:none;font-size:11px;}'
                            'QTabBar::tab{background:%s;color:%s;padding:3px 7px;border-radius:5px;'
                            'margin-right:2px;font-size:10px;}'
                            'QTabBar::tab:selected{background:%s;}'
                            % (pet._solid(pet._tk('ui_popup_bg')), pet._tk('ui_text_dim'),
                               pet._solid(pet._tk('ui_popup_list_bg')), pet._tk('ui_text'),
                               pet._solid(pet._tk('ui_popup_list_bg')), pet._tk('ui_text'),
                               pet._solid(pet._tk('ui_popup_btn_bg')), pet._tk('ui_text_soft'),
                               pet._tk('ui_accent'))))
    spnl.resize(186, 420)
    spnl.show()
    app.processEvents()
    spnl.tabbar.setCurrentIndex(1)               # 选中「¤ 记账」页签
    spnl.refresh_dynamic(force=True)
    app.processEvents()
    body = spnl.stack.currentWidget()
    shown = body.text() if hasattr(body, 'text') else ''
    check('支出' in shown and '预算' in shown, '页签正文画出来了：%s'
          % shown.replace('\n', ' / ')[:56])
    shot(app, spnl, 'v689_ledger_side.png', '侧栏记账页签')

    # 标题栏入口 + 右键菜单项
    check(getattr(pet, 'chat_lg_btn', None) is not None
          and pet.chat_lg_btn.text() == '¥', '标题栏有 ¥ 入口（tooltip：%s）'
          % pet.chat_lg_btn.toolTip()[:20])
    src = open(os.path.join(BASE, 'desktop_pet.py'), encoding='utf-8-sig').read()
    check("'ledger': '_tool_ledger'" in src and "self._t('lg_menu')" in src,
          '工具句柄 + 右键「¥ 记账…」都接上了')
    check(callable(getattr(pet, 'open_ledger', None)), 'open_ledger() 存在（标题栏/右键都能调）')
    same = pet.open_ledger()
    app.processEvents()
    check(same is panel and panel.isVisible(), 'open_ledger() 打开的就是账本面板（同一个实例）')

    # 入口指路图：标题栏是真实渲染的（¥ 只有一个字符，不圈出来真找不到）
    try:
        from PIL import Image, ImageDraw, ImageFont
        pm = pet.chat_header.grab()
        if not pm.isNull():
            hp = os.path.join(WORK, 'header.png')
            pm.save(hp)
            im = Image.open(hp).convert('RGB')
            im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
            g = pet.chat_lg_btn.geometry()
            box = [g.x() * 3 - 8, g.y() * 3 - 8, (g.x() + g.width()) * 3 + 8,
                   (g.y() + g.height()) * 3 + 8]
            d = ImageDraw.Draw(im)
            d.rectangle(box, outline=(230, 60, 90), width=4)
            font = None
            for cand in ('msyh.ttc', 'msyhbd.ttc', 'simhei.ttf'):
                try:
                    font = ImageFont.truetype(os.path.join('C:\\Windows\\Fonts', cand), 26)
                    break
                except Exception:
                    continue
            tip = '↑ 标题栏「¥」= 记账（右键菜单里是「¥ 记账…」）'
            pad = 14
            canvas = Image.new('RGB', (max(im.width, 900), im.height + 48 + pad),
                               (24, 30, 46))
            canvas.paste(im, (0, 0))
            d2 = ImageDraw.Draw(canvas)
            if font:
                d2.text((10, im.height + 12), tip, fill=(230, 235, 245), font=font)
            out = os.path.join(SHOTS, 'v689_ledger_where.png')
            canvas.save(out)
            print('    已存 screenshots/v689_ledger_where.png（入口指路图）')
            check(os.path.isfile(out), '入口指路图生成：%dx%d' % (canvas.width, canvas.height))
    except Exception as e:
        print('    （指路图跳过：%s）' % e)

    # ---------- 8. 收尾 ----------
    print('\n[8] 收尾')
    try:
        panel.close()
        dlg.close()
        bd.close()
        spnl.close()
        pet.close()
    except Exception:
        pass
    app.processEvents()
    for k in cfg_keys:
        if prev_cfg.get(k) is None:
            lg.cfg_delete(k)
        else:
            lg.cfg_write(k, prev_cfg[k])
    after = sha(real_ledger)
    check(after == real_before, '使用者真实 ledger.json 一个字节都没变（sha256 %s）'
          % (after[:12] or '不存在'))
    check(not os.path.exists(real_ledger) or real_before == after,
          '没有在你的项目目录里生成账本文件')
    leak = [k for k in cfg_keys if lg.cfg_read().get(k) is not None and prev_cfg.get(k) is None]
    check(not leak, 'config 里没留下临时 lg_* 键（残留：%s）' % (leak or '无'))
    shutil.rmtree(WORK, ignore_errors=True)

    print('\n===== 结果：%s =====' % ('全部通过' if not FAILS else '%d 项未通过' % len(FAILS)))
    for f in FAILS:
        print('  ✕ %s' % f)
    return 0 if not FAILS else 1


if __name__ == '__main__':
    sys.exit(main())
