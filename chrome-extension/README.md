# m3u8-cli 链接检测器（Chrome 扩展）

配合本仓库的 `m3u8-cli` 使用的 Manifest V3 扩展，用于识别网页中的 m3u8 播放列表，并可直接提交到本地 `m3u8-cli serve` 服务下载。

- 无构建步骤：原生 JavaScript，Chrome 直接加载目录即可。
- 网络请求 + DOM 扫描双来源识别，支持 iframe 中的播放器。
- 一键下载时自动携带该 m3u8 请求自己的 `Referer`、`Cookie`、`User-Agent`。
- 弹窗内查看任务进度、取消任务；popup 关闭后仍由后台轮询。

## 安装

1. 打开 Chrome，访问 `chrome://extensions`。
2. 打开右上角“开发者模式”。
3. 点击“加载已解压的扩展程序”，选择本目录（仓库中的 `chrome-extension/`）。
4. 建议把扩展固定到工具栏，方便查看 badge 数量。

扩展更新（修改代码）后，需要在 `chrome://extensions` 中点击刷新；已经打开的页面也要刷新一次，content script 才会重新注入。

## 启动本地下载服务

扩展的“一键下载”依赖 `m3u8-cli serve`。建议固定 token 并只监听本机：

```bash
m3u8-cli serve --host 127.0.0.1 --port 8787 --token mytoken --output-dir ./downloads
```

如果使用 `uv run` 参与开发：

```bash
uv run m3u8-cli serve --host 127.0.0.1 --port 8787 --token mytoken --output-dir ./downloads
```

然后在扩展弹窗的“设置”中填写：

| 设置项 | 说明 |
| --- | --- |
| 服务地址 | 默认 `http://127.0.0.1:8787`，需与 `--host/--port` 一致 |
| Token | 与 `--token` 一致；点击“测试连接”确认 |
| 下载格式 | `mp4`（合并封装）或 `hls`（保留分片） |
| 覆盖已存在的文件 | 对应 serve 的 `overwrite` 字段 |
| 发送捕获到的 Cookie | 默认开启，仅发送给上面配置的服务地址 |
| 复制 CLI 命令时包含 Cookie | 默认关闭；开启后命令会包含 Cookie 原文，注意 shell 历史 |

## 使用

1. 打开包含视频的页面并开始播放，或等待页面加载完成。
2. 扩展图标上的 badge 显示当前标签页识别到的 m3u8 数量。
3. 点击扩展图标：
   - **复制**：复制 m3u8 URL。
   - **复制 CLI**：生成 `m3u8-cli download ...` 命令，按设置决定是否包含 `--referer`、`--cookie`、`--user-agent`。
   - **下载**：提交到本地 serve 服务；若提示文件名冲突，可点击“换名重试”自动加时间戳。
4. “下载任务”区域显示排队中/下载中/已完成/失败/已取消/已中断等状态和分片进度；运行中的任务可以取消。
5. “重新扫描”会请求当前页面的所有 frame 重新执行 DOM 扫描；“清空”只会清除本标签页的识别结果。

识别来源：

- **网络**：`webRequest` 观察页面的实际请求，URL pathname 以 `.m3u8`/`.m3u` 结尾，或响应 `Content-Type` 为 HLS MIME 类型（可识别无扩展名的播放列表地址）。
- **页面**：content script 扫描 `video`/`audio`/`source`、m3u8 链接和脚本文本中的地址。

同一 URL 同时来自两个来源时会合并为一条，并优先保留网络请求捕获的请求头。

## 权限说明

| 权限 | 用途 |
| --- | --- |
| `webRequest` | 观察页面网络请求，识别 m3u8 并捕获该请求的请求头 |
| `storage` | `storage.session` 保存当前标签页候选（仅内存）；`storage.local` 保存设置与任务摘要 |
| `alarms` | popup 关闭后继续轮询下载任务 |
| `<all_urls>` | 在所有 http/https 页面检测；同时允许向配置的本地/远程 serve 地址发起请求 |

扩展不请求 `cookies` 权限：Cookie 只从 m3u8 请求自身的 `Cookie` 头读取（需要 `extraHeaders`），不会主动读取浏览器 Cookie 存储。

由于使用 `<all_urls>`，Chrome 在安装时会提示扩展可以读取和更改所有网站上的数据；这是被动捕获网络请求与请求头的必要代价，扩展不会修改页面内容。

## 隐私与安全

- 只转发白名单请求头：`Referer`、`Cookie`、`User-Agent`，且只使用该 m3u8 请求自己携带的值。
- Cookie 保存在 `chrome.storage.session`（浏览器重启即清空），不会写入 `storage.local`、不同步到云端，也不会出现在弹窗或任务历史中；弹窗只显示“含 Cookie”标记。
- 服务地址不是本机（`127.0.0.1`/`localhost`/`::1`）且开启 Cookie 透传时，设置区会显示警告。
- Token 保存在 `storage.local`，请勿在不信任的共享 Chrome 配置中使用。
- serve 端请求头只保留在内存，不落盘、不在任务响应中返回；详见仓库 README。
- 扩展不会自动提交下载，必须由用户点击，避免页面借扩展触发 serve 请求。
- CORS `*` 不是安全边界，真正的访问控制是 Bearer token。

## 排障

| 现象 | 处理 |
| --- | --- |
| 弹窗提示“未检测到 m3u8” | 确认视频已开始播放；刚安装/重载扩展后刷新页面；尝试“重新扫描” |
| “测试连接”报 `Failed to fetch` | 确认 serve 已启动、地址与端口正确、协议为 `http/https` |
| 401 / “未授权” | 检查 Token；serve 未指定 `--token` 时每次启动都会生成新 token |
| 409 / 目标文件已存在 | 点击“换名重试”，或在设置中开启“覆盖已存在的文件” |
| 下载失败，提示 403 | 源站可能校验 `Referer`/`Cookie`/`User-Agent`；确认候选来自“网络”且页面仍处于登录状态 |
| 扩展重载后页面无反应 | 刷新页面，让 content script 重新注入 |
| badge 数量未变化 | 网络识别只在页面产生新请求时更新；静态链接由 DOM 扫描或“重新扫描”补充 |

## 开发与验证

```bash
# JS 语法
for f in lib.js background.js content.js popup.js; do node --check "$f"; done

# 纯函数单元测试（Node 内置 test runner，无需安装依赖）
node --test lib.test.js

# manifest JSON
python3 -c "import json; json.load(open('manifest.json'))"
```

### 文件职责

| 文件 | 职责 |
| --- | --- |
| `manifest.json` | MV3 配置、权限、content script 注入 |
| `lib.js` | 三端共享的纯函数：URL 判断、MIME 判断、输出名清洗、shell 转义、CLI 命令 |
| `lib.test.js` | `lib.js` 的单元测试 |
| `background.js` | 网络检测、候选与会话存储、badge、serve API 调用、任务轮询、消息路由 |
| `content.js` | DOM 扫描、MutationObserver、增量上报 |
| `popup.html/css/js` | 候选列表、复制、下载、任务进度与设置 |

### 数据与消息

- `storage.session["tab:<tabId>"]`：`{pageUrl, items:[{id,url,source,hasCookie,...}]}`，最多 30 条，标签页关闭或导航时清理。
- `storage.session["hdr:<candidateId>"]`：`{cookie, referer, userAgent}`，仅 background 读取。
- `storage.local["settings"]`：服务地址、token、格式等设置。
- `storage.local["tasks"]`：最近的任务摘要（最多 20 条，优先保留活动任务），不含请求头。
- popup 通过 `chrome.runtime.sendMessage` 发送 `download:submit`、`task:cancel`、`tasks:refresh`、`settings:test`、`cli:build`、`tab:clear`、`dom:rescan`。
- content script 通过 `dom:candidates` 上报 URL，用 `sender.tab.id` 归属标签页。

## 已知限制

- popup 关闭后任务轮询依赖 `chrome.alarms`，最短周期为 30 秒（旧版 Chrome 为 1 分钟）；popup 打开时每 2 秒刷新一次。
- 只做网络请求与 DOM 扫描，不注入 MAIN world 去 hook `fetch`/`XMLHttpRequest`；缓存命中或没有实际请求的地址可能只由 DOM 扫描发现。
- 不支持 Shadow DOM 内的元素；iframe 支持依赖 `all_frames` 和网络层。
- SPA 路由切换不会自动清空候选，使用“清空”或刷新页面。
- 每个标签页最多保留 30 条候选，任务历史最多 20 条。
- 下载能力受 `m3u8-cli` 本身限制（AES-128 identity、无 DRM、不支持 BYTERANGE 等），详见根 README。
- 与 `blob:` URL 相关的播放器流无法直接交给 serve 下载；需要页面实际请求 m3u8 时才能识别。
