# -*- coding: utf-8 -*-
"""权限档位（v6.91）：聊天输入框左边的「放不放权」选择器

使用者原话：「给聊天的地方加一个权限选择，可以选择放不放权，要不然一直问我危险操作很麻烦」。

现在真正会弹确认框的只有两处：
  ① PowerShell：`pet_sysutils.check_dangerous` 是**只读白名单**——AI 只要拼一句复合命令
     （`Get-Process | Where-Object {...}`、`Set-Content` 写个文件）就命中"不在白名单内"
     → 每次都弹框，干正经活儿像在受审
  ② MCP 写类工具：`mcp_confirm_writes` 默认开，每个写操作都问
所以"问不问"该由使用者按当下的心情决定，而不是写死在代码里。

三档（拍板：三档 / 只对**本轮对话**有效）
  strict 🔒 严格       ：跟以前一模一样（非只读命令 + MCP 写工具都问）
  smart  🛡 只问真危险 ：只有**真破坏性**（删/格式化/关机/注册表/服务/杀进程/下载执行…，
                        即 `pet_sysutils.destructive_reason` 那张窄表）和 MCP 标了
                        destructiveHint 的才问；写文件、开程序直接干
  open   🔓 全放权     ：都不问，直接执行（只记审计）

安全边界（三档都一样，别在这些地方偷懒）
  · 只管 **AI 工具执行前**的确认框。删记忆 / 清空对话 / 卸载技能包 / 删 MCP server 这些
    **使用者自己点出来的**操作照样弹框 —— 那不是 AI 自作主张，放权也不该顺手替他点"确定"
  · 切档、以及"因为放权所以没问"的执行，都写审计（`governance.log_event`），
    事后能在审计日志里查清"哪天几点放的权、放权期间跑了什么"
  · 只对**本轮对话**有效：点「✚ 新对话」或重启桌宠 → 自动回到 🔒 严格（不会悄悄一直裸奔）
"""
import pet_sysutils as su

STRICT, SMART, OPEN = 'strict', 'smart', 'open'
LEVELS = (STRICT, SMART, OPEN)

# 图标用单字符（聊天输入框那一排只有 30×30，多一个字就挤爆）
ICON = {STRICT: '🔒', SMART: '🛡', OPEN: '🔓'}
# 按钮颜色走主题 token：严格=低调灰、只问真危险=强调色、全放权=警示红（看得见才不容易忘）
COLOR_TOKEN = {STRICT: 'ui_text_soft', SMART: 'accent', OPEN: 'ui_red'}
# i18n key（文案在 desktop_pet.UI_ZH / UI_EN 里）
LABEL_KEY = {STRICT: 'perm_strict', SMART: 'perm_smart', OPEN: 'perm_open'}
HINT_KEY = {STRICT: 'perm_strict_hint', SMART: 'perm_smart_hint', OPEN: 'perm_open_hint'}


class Decision(object):
    """一次闸门判定的结果

    need_ask  ：要不要弹确认框
    why       ：为什么要问（写进确认框的正文）
    delegated ：本轮**因为放权所以没问**（宿主据此补一条审计）
    """

    __slots__ = ('need_ask', 'why', 'delegated')

    def __init__(self, need_ask=False, why='', delegated=False):
        self.need_ask = bool(need_ask)
        self.why = why or ''
        self.delegated = bool(delegated)

    def __repr__(self):
        return 'Decision(need_ask=%s, delegated=%s, why=%s)' % (
            self.need_ask, self.delegated, self.why[:40])


class Permission(object):
    """当前权限档位（只在内存里：本轮对话有效，新对话/重启自动回严格）"""

    def __init__(self, level=STRICT):
        self._level = level if level in LEVELS else STRICT

    @property
    def level(self):
        return self._level

    def set(self, level):
        """切档；非法值当作没听见。返回"真的变了"（宿主据此提示+记审计）"""
        if level not in LEVELS or level == self._level:
            return False
        self._level = level
        return True

    def reset(self):
        """回到严格（新对话 / 退出时用）"""
        changed = self._level != STRICT
        self._level = STRICT
        return changed

    def icon(self):
        return ICON.get(self._level, ICON[STRICT])


def gate_powershell(level, cmd):
    """PowerShell 命令要不要先问（Decision）"""
    if level == OPEN:
        # 全放权：不问；但如果严格档本来会拦，就标记 delegated 让宿主记审计
        return Decision(False, '', bool(su.check_dangerous(cmd)))
    if level == SMART:
        why = su.destructive_reason(cmd)
        return Decision(bool(why), why or '')
    why = su.check_dangerous(cmd)
    return Decision(bool(why), why or '')


def gate_mcp(level, mcp, full_name):
    """MCP 工具调用要不要先问（Decision）；mcp 得是 mcp_bridge.McpBridge（有 needs_confirm）"""
    if mcp is None:
        return Decision(False, '')
    if level == OPEN:
        need, why = mcp.needs_confirm(full_name)      # 只为了判断"本来该问吗"
        return Decision(False, '', need)
    if level == SMART:
        need, why = mcp.needs_confirm(full_name, only_destructive=True)
        return Decision(need, why)
    need, why = mcp.needs_confirm(full_name)
    return Decision(need, why)


def note_delegated(level, what):
    """因为放权所以没问 —— 补一条审计，事后可查（审计关着时这函数自己会安静返回）"""
    try:
        import governance as gov
        gov.log_event('perm', 'user', 'delegate',
                      '%s（档位=%s，本轮对话）' % (what, level), allowed=True)
    except Exception:
        pass


def note_switch(level, old=None):
    """切档也记一笔：什么时候放开的口子，回头能对上"""
    try:
        import governance as gov
        gov.log_event('perm', 'user', 'set_level',
                      '%s → %s（只对本轮对话有效）' % (old or '?', level), allowed=True)
    except Exception:
        pass
