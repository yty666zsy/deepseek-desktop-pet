# musicfree/ —— MusicFree 插件宿主

桌宠的音乐功能不是「嵌一个 MusicFree 界面」，而是**把 MusicFree 的插件当音源库用**。

## 为什么这么做

- MusicFree 本体（[maotoumao/MusicFree](https://github.com/maotoumao/MusicFree)，AGPL-3.0）是
  React Native 应用，界面塞不进 PySide6 窗口；
- 但官方文档写明：**插件本质是导出特定数据结构的 CommonJS 模块，可以直接在 node.js 里调用**
  （https://musicfree.catcat.work/plugin/introduction.html）；
- 于是这里只做一件事：在 Node 里 `require` 插件，通过 stdin/stdout 一行一 JSON 提供
  `search` / `getMediaSource` / `getLyric`，Python 侧（`pet_music_free.py`）拿到直链交给
  mpv 播放、拿到 LRC 交给头顶气泡。

## 文件

| 文件 | 作用 |
|---|---|
| `host.js` | 插件宿主：一行一请求 `{"id":1,"method":"search","args":{...}}` → 一行一响应 |
| `package.json` | 插件需要的 npm 依赖（axios / cheerio / dayjs / he / crypto-js / qs / webdav / big-integer） |
| `plugins/` | MusicFree 插件（**第三方**，不入库，见 `plugins/README.md`） |
| `vars.json` | 插件用户变量（需自己创建，见下） |

## 一次性准备

```powershell
node -v          # 需要 Node.js（本机实测 v22）
cd musicfree
npm install      # 只需一次，装完 node_modules/ 被 .gitignore 忽略
node host.js list   # 自测：列出已装插件
```

## 装插件：用订阅（推荐）

桌宠里 🎵 音乐 → **➕ 订阅** → 填 `https://hebijunge.github.io/musicfree-plugins/plugins.json`
→ 57 个音源全部装好（实测 68/68 插件加载成功）。也可以手动把插件 `.js` 丢进 `plugins/`。

MusicFree 的订阅就是一个 json 清单，格式宽得很，宿主都认：

```json
{"desc":"0.1.0","plugins":[{"name":"QQ音乐","url":"…/qq-source.plugin.v1.9.23.js","version":"1.9.23"}]}
```

## 插件用户变量（Jamendo / Navidrome / WebDAV 这类要配的）

在 `musicfree/vars.json` 里按插件 platform 分组的键值，插件调 `env.getUserVariables()` 时拿到：

```json
{
  "jamendo":    {"clientId": "在 developer.jamendo.com 免费申请的 Client ID"},
  "navidrome":  {"url": "http://127.0.0.1:4533", "username": "me", "password": "…"},
  "webdav":     {"url": "https://dav.example.com/music", "username": "me", "password": "…"}
}
```

没配的时候，这类插件搜歌会直接抛「未配置 Client ID」之类的明确提示（不会静默失败）。

## 命令行自测

```powershell
node host.js list
node host.js search bilibili "周杰伦 晴天" 1 music
```

## 插件许可（重要）

`plugins/` 里的 `.js` 来自 MusicFree 社区订阅，**GPL-3.0**，版权归各插件作者。它们是**独立文件、
运行时加载**，本项目的 Python/JS 宿主代码没有复制、也没有链接它们的源码；也因此不随仓库分发。
若要二次分发本项目，请自行确认插件许可与各音源站点的服务条款。
