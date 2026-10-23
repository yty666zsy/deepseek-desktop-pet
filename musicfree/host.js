"use strict";
/**
 * host.js —— MusicFree 插件宿主（Node 侧）
 *
 * 为什么要有它：MusicFree 本体是 React Native 应用（AGPL-3.0），界面没法塞进 PySide6；
 * 但官方文档写明「插件本质是一个导出特定数据结构的 CommonJS 模块，可以直接在 node.js 里调用」。
 * 所以这里只做一件事：把插件当函数库加载，通过 stdin/stdout 逐行 JSON 给 Python 侧提供
 *   list   列出已装插件
 *   search 搜索歌曲/歌词
 *   media  取真实音源 URL（+ 需要的请求头）
 *   lyric  取歌词（rawLrc）
 * Python 侧拿到 url 交给 mpv 播放、拿到 rawLrc 交给头顶气泡逐行滚动。
 *
 * 协议：一行一个请求 {"id":1,"method":"search","args":{...}}
 *       一行一个响应 {"id":1,"ok":true,"result":...} / {"id":1,"ok":false,"error":"..."}
 *
 * 命令行自测：node host.js search bilibili 周杰伦 1 music
 *            node host.js media bilibili '<item json>'
 */
const fs = require("fs");
const path = require("path");
const readline = require("readline");

const PLUGIN_DIR = process.env.MUSICFREE_PLUGIN_DIR
  ? path.resolve(process.env.MUSICFREE_PLUGIN_DIR)
  : path.join(__dirname, "plugins");   // 可用环境变量换目录（自测/多套插件时用）
const loaded = new Map();      // 文件名 -> { file, mod, name, error }

// 插件里常见的两种“污染 stdout”的行为，必须挡住，否则一行一 JSON 的协议会被搞坏：
//   1) console.log —— 转到 stderr（调试信息还能看，但不会串进协议）
//   2) 模块加载时就发请求的插件，请求失败会产生未处理的 rejection —— 只记 stderr，绝不退出
for (const name of ["log", "info", "debug", "warn", "trace"]) {
  console[name] = (...a) => {
    try { process.stderr.write(a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ") + "\n"); } catch (e) {}
  };
}
process.on("unhandledRejection", (e) => {
  try { process.stderr.write("[unhandledRejection] " + String((e && e.stack) || e) + "\n"); } catch (x) {}
});
process.on("uncaughtException", (e) => {
  try { process.stderr.write("[uncaughtException] " + String((e && e.stack) || e) + "\n"); } catch (x) {}
});

// 插件可以通过 env.getUserVariables() 拿用户填的变量（Jamendo 的 Client ID、Navidrome 的地址账号等）。
// 变量存在 musicfree/vars.json：{"jamendo": {"clientId": "..."}, "navidrome": {...}}
// 调用某个插件的方法前把 currentVars 指向它自己的那组，插件内外都不需要改。
const VARS_FILE = path.join(__dirname, "vars.json");
let userVars = {};
let currentVars = {};
function loadVars() {
  try {
    userVars = JSON.parse(fs.readFileSync(VARS_FILE, "utf8")) || {};
  } catch (e) {
    userVars = {};
  }
  return userVars;
}
loadVars();

if (!global.env) {
  global.env = {
    getUserVariables: () => currentVars || {},
    os: process.platform,
    appVersion: "0.6.0",
  };
}

function pluginFiles() {
  try {
    return fs.readdirSync(PLUGIN_DIR).filter((n) => n.toLowerCase().endsWith(".js")).sort();
  } catch (e) {
    return [];
  }
}

function loadOne(file) {
  let entry = loaded.get(file);
  if (entry) return entry;
  entry = { file, mod: null, name: path.basename(file, ".js"), error: "" };
  try {
    let mod = require(path.join(PLUGIN_DIR, file));
    // 有一部分插件是 ES 模块打包出来的，真正的东西挂在 module.exports.default 上
    // （迟言QQ / 落月QQ / 弥音QQ / 酷我_竹玥 …），不拆包的话会显示「这个插件没有 search」
    if (mod && typeof mod === "object" && !mod.search && mod.default && typeof mod.default === "object") {
      mod = mod.default;
    }
    entry.mod = mod || null;
    if (mod && typeof mod.platform === "string" && mod.platform.trim()) {
      entry.name = mod.platform.trim();
    }
    if (!mod) entry.error = "插件没有导出任何东西";
  } catch (e) {
    entry.error = String((e && e.message) || e);
  }
  loaded.set(file, entry);
  return entry;
}

function describe(entry) {
  const mod = entry.mod || {};
  return {
    file: entry.file,
    platform: entry.name,
    version: mod.version || "",
    author: mod.author || "",
    error: entry.error,
    supportedSearchType: Array.isArray(mod.supportedSearchType) ? mod.supportedSearchType : [],
    methods: Object.keys(mod).filter((k) => typeof mod[k] === "function"),
  };
}

function listPlugins() {
  return pluginFiles().map((f) => describe(loadOne(f)));
}

function findPlugin(nameOrFile) {
  const want = String(nameOrFile || "").trim();
  if (!want) throw new Error("没指定插件");
  // 先按文件名精确匹配：同一个 platform（qq/netease…）常常装了多套变体，
  // 只有文件名是唯一的，传文件名才能指定「腾讯音乐」而不是笼统的 qq。
  for (const f of pluginFiles()) {
    const e = loadOne(f);
    if (e.mod && (e.file === want || e.file === want + ".js")) return e;
  }
  // 再退回按 platform 找（老调用方式 / 只有一个同名插件时）
  for (const f of pluginFiles()) {
    const e = loadOne(f);
    if (e.mod && e.name === want) return e;
  }
  throw new Error("插件不存在：" + want);
}

async function dispatch(req) {
  const method = req.method;
  const args = req.args || {};

  if (method === "ping") return { pong: true, node: process.version };
  if (method === "list") return { plugins: listPlugins() };
  if (method === "vars") return { vars: loadVars(), file: VARS_FILE };
  if (method === "reload") {
    for (const key of Object.keys(require.cache)) {
      if (key.startsWith(PLUGIN_DIR)) delete require.cache[key];
    }
    loaded.clear();
    return { plugins: listPlugins() };
  }

  const entry = findPlugin(args.plugin);
  const mod = entry.mod;
  // 这个插件自己的用户变量（Jamendo 之类没配就会直接抛配置错误，属于正常提示）
  currentVars = userVars[entry.name] || userVars[entry.file] || {};

  if (method === "search") {
    if (typeof mod.search !== "function") throw new Error(entry.name + " 不支持搜索");
    const type = args.type || "music";
    if (Array.isArray(mod.supportedSearchType) && mod.supportedSearchType.length &&
        mod.supportedSearchType.indexOf(type) < 0) {
      throw new Error(entry.name + " 不支持搜索 " + type);
    }
    const r = (await mod.search(args.query || "", Number(args.page || 1), type)) || {};
    const data = Array.isArray(r.data) ? r.data : [];
    // 协议约定：platform 由宿主补上，用来回传给 getMediaSource/getLyric
    for (const it of data) {
      if (it && typeof it === "object") {
        it.platform = entry.name;
        // 有的插件把时长写成毫秒（wy.js：278961 = 4:39），一律折成秒，
        // 不然进度条、歌词对齐、换源时的时长比对全乱
        const d = Number(it.duration);
        if (isFinite(d) && d > 36000 && d / 1000 < 36000) it.duration = Math.round(d / 1000);
      }
    }
    return { isEnd: r.isEnd !== false, data };
  }

  if (method === "media") {
    const item = args.item || {};
    if (typeof mod.getMediaSource === "function") {
      const r = await mod.getMediaSource(item, args.quality || "standard");
      if (r && r.url) {
        return { url: String(r.url), headers: r.headers || null, userAgent: r.userAgent || "" };
      }
    }
    if (item.url) return { url: String(item.url), headers: null, userAgent: "" };
    throw new Error(entry.name + " 拿不到音源（可能该曲目需要会员/已下架）");
  }

  if (method === "lyric") {
    if (typeof mod.getLyric !== "function") return { rawLrc: "", translation: "" };
    const r = (await mod.getLyric(args.item || {})) || {};
    return {
      rawLrc: typeof r.rawLrc === "string" ? r.rawLrc : "",
      translation: typeof r.translation === "string" ? r.translation : "",
    };
  }

  throw new Error("未知方法：" + method);
}

function reply(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

// ---- 一行一请求的守护模式 ----
if (process.argv.length <= 2) {
  const rl = readline.createInterface({ input: process.stdin });
  rl.on("line", (line) => {
    const text = String(line || "").trim();
    if (!text) return;
    let req;
    try {
      req = JSON.parse(text);
    } catch (e) {
      return;
    }
    Promise.resolve()
      .then(() => dispatch(req))
      .then((result) => reply({ id: req.id, ok: true, result }))
      .catch((e) => reply({ id: req.id, ok: false, error: String((e && e.message) || e) }));
  });
  rl.on("close", () => process.exit(0));
} else {
  // ---- 命令行自测模式 ----
  // list | ping
  // search <插件> <关键词> [页] [类型]
  // media  <插件> '<item JSON>'
  // lyric  <插件> '<item JSON>'
  const method = process.argv[2];
  let args = {};
  if (method === "search") {
    args = {
      plugin: process.argv[3],
      query: process.argv[4] || "",
      page: Number(process.argv[5] || 1),
      type: process.argv[6] || "music",
    };
  } else if (method === "media" || method === "lyric") {
    args = { plugin: process.argv[3], item: JSON.parse(process.argv[4] || "{}") };
  }
  Promise.resolve()
    .then(() => dispatch({ method, args }))
    .then((r) => reply({ ok: true, result: r }))
    .catch((e) => reply({ ok: false, error: String((e && e.message) || e) }));
}
