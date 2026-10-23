# plugins/ —— MusicFree 插件（第三方，不入库）

这里的 `.js` **不进仓库**（`.gitignore` 已排除）：它们来自 MusicFree 插件订阅，GPL-3.0，
版权归各插件作者；随时可以用「➕ 订阅」一键重装，没必要占仓库体积（57 个插件约 20MB）。

## 怎么装

桌宠 → 🎵 音乐 → **➕ 订阅** → 粘贴订阅地址 → 自动下载并刷新插件列表：

| 订阅地址 | 说明 |
|---|---|
| `https://hebijunge.github.io/musicfree-plugins/plugins.json` | 实测可用，57 个音源（QQ/网易云/酷狗/酷我/咪咕/汽水/B站/喜马拉雅/蜻蜓/JOOX/千千…），按分类自测过 |
| `https://ghproxy.net/https://raw.githubusercontent.com/hebijunge/musicfree-plugins/master/plugins-ghproxy.json` | 上面那个的国内加速通道，内容一致 |
| `https://qwerwhr.github.io/musicfree-plugins/plugins.json` | 76 个源，QQ/网易云/酷我/酷狗各家「变体」合集（元力/溯音/念心/腾讯音乐/千千…），会员曲命中率高但一半以上已失效 |
| `https://musicfreepluginshub.2020818.xyz/plugins.json` | 聚合站，GitHub Actions 每天自动检测，只留还能用的；装的时候个别会报 418（反爬），重试或换 `pages.dev` 通道 |

这三家装齐了大约 **150 个能搜歌的源**。同一个平台（比如网易云）会有十来份实现，
所以面板下拉框里的值用的是**插件文件名**而不是 platform —— platform 全都叫 `netease`，
只有文件名分得开；同名源会自动带上文件名后缀。

**别指望「一个源听所有歌」**：会员/独家曲各家都只给试听，插件能做的是「不登录拿到 128k 完整版」。
遇到只有 1 分钟的歌，点面板上的 **🔀 换源**，它会拿歌名去别的源找同名同时长的完整版。

装完点「🔄 更新插件」会重拉所有保存过的订阅（版本没变自动跳过）。
也可以手动把任意 MusicFree 插件的 `.js` 丢进这个目录（文件名随意），重启桌宠生效。

## 目录里几个约定文件

| 文件 | 作用 |
|---|---|
| `_plugins.json` | 自动生成：文件名 → 中文名 / 版本 / 来源订阅（下拉框的中文名、「已是最新」判断都靠它） |
| `_disabled/` | 暂时不想用的插件挪进来就加载不到（保留文件，随时挪回去） |
| `../vars.json` | 插件用户变量，如 Jamendo 的 `clientId`（格式见 `musicfree/README.md`） |

## 生效顺序

宿主只加载本目录**顶层**的 `*.js`，子目录忽略 —— 所以拿不准的插件可以挪进 `_disabled/` 试一试。
