# astrbot_plugin_douyin_parser

基于 [Douyin_TikTok_Download_API（DTK）v5](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) 的 AstrBot 插件。识别抖音 / TikTok 作品链接和分享文案，在 QQ 群聊或私聊中回复作品信息、视频或图文。

## 环境要求

- AstrBot >= 4.25.1，使用 `aiocqhttp` / OneBot v11 平台。
- SnowLuma（也兼容 NapCat），支持 OneBot v11 的 `download_file`、普通消息及合并转发接口。
- 可访问的 DTK **v5.x** 实例，以及具有对应平台读取权限的 API Key。
- DTK 身份池中有对应平台的可用身份；支持 TikTok 不代表只配置抖音身份也能解析 TikTok。

本版不再调用 v4 的 `/api/hybrid/video_data` 或 `/api/download`，也不兼容 v4 的返回格式。

## 安装与从旧版迁移

1. 更新插件全部文件，重载插件；AstrBot 会根据 `requirements.txt` 安装 `httpx`。
2. 将原有 `url` 改为 **DTK v5 的服务地址**，例如 `http://127.0.0.1:8000`。
   - AstrBot 在容器中时，`127.0.0.1` 指的是 AstrBot 自己，不是宿主机或 DTK 容器；请填写容器网络内的服务名或可达地址。
   - 支持反向代理路径前缀，例如 `https://example.com/dtk`；也接受以 `/api/v1` 结尾的地址。
   - 不要填写控制台页面地址、`/mcp/` 或旧版接口路径。远程访问建议使用 HTTPS，地址不要依赖 HTTP 重定向。
3. 在 DTK 控制台创建 API Key，填写新增的 `apiKey`。
   - 解析抖音需要 `douyin:read`；解析 TikTok 需要 `tiktok:read`。
   - 插件直接让 OneBot 后端下载 CDN 媒体，**不需要** `media:read` / `media:write`，不建议使用管理员 Key。
   - 只有管理员明确开放相应匿名接口时，才可以不填 Key。
4. 核对发送开关、大小 / 时长限制，保存后重载插件。原有配置键保留，无须手工删除旧配置。

> API Key 保存在 AstrBot 插件配置中。请勿把插件配置、Key 或完整鉴权请求头粘贴到群聊或提交到 Git。

## 配置

| 配置键 | 默认值 | 说明 |
| --- | --- | --- |
| `url` | `http://127.0.0.1:8000` | AstrBot 可访问的 DTK v5 地址 |
| `apiKey` | 空 | 通过 `Authorization: Bearer …` 请求头认证 |
| `isSendCover` | `false` | 是否发送普通视频封面；不会改用缩略图 |
| `isThumbs` | `false` | 是否发送独立视频缩略图（`big_thumbs`）；缺失时跳过，不改用封面 |
| `isSendVideo` | `false` | 是否发送作品媒体，包括视频、图文、实况图；关闭仍发送文字信息 |
| `videoMaxDuration` | `0` | 视频最大时长，单位秒；`0` 不限制，实况视频同样适用 |
| `videoMaxSize` | `0` | 视频最大大小，单位 MB，按 1024² 字节换算；`0` 不限制 |
| `apiTimeout` | `30` | 单次 DTK HTTP 请求超时，单位秒 |
| `taskTimeout` | `120` | 提交及等待解析任务的总超时，单位秒 |
| `taskPollInterval` | `2` | 任务轮询间隔，单位秒，最小为 `1` |
| `mediaDownloadTimeout` | `120` | 单个媒体的 OneBot 后端下载超时，单位秒；不包含等待下载并发槽的时间 |
| `downloadConcurrency` | `3` | 全插件共享的后端下载并发上限，最小为 `1` |
| `sendLinkOnFailure` | `true` | 普通视频下载 / 上传失败时附作品链接和媒体直链；不绕过大小 / 时长限制 |

### 封面与缩略图设置

封面和缩略图是两个独立开关，只控制视频详情中的预览图片，不影响视频或图文原图的发送：

| `isSendCover` | `isThumbs` | 视频详情中的图片 |
| --- | --- | --- |
| 关闭 | 关闭 | 不发送预览图片 |
| 开启 | 关闭 | 只发送普通封面 |
| 关闭 | 开启 | 只发送独立缩略图 |
| 开启 | 开启 | 按封面、缩略图的顺序分别发送 |

**从 v0.1.1 升级：**原 `isThumbs` 的配置值保留，但现在只控制缩略图，不再控制封面。若想发送普通封面而不发送缩略图，请开启新增的 `isSendCover`，并关闭 `isThumbs`。两种资源缺失或下载失败时，不会相互替换；另一种已开启且可用的资源仍会发送。

## 回复行为

- **视频**：发送标题、作者、时长及统计；根据独立开关发送普通封面、缩略图和视频。
- **单张图文**：直接发送图片和作品信息，修复旧版单张静态图文没有回复的问题。
- **多张图文**：使用合并转发，第一条是作品信息，后续按原顺序发送图片 / 实况视频。
- **抖音实况图**：使用 v5 `include_raw` 的图片视频地址补充标准图片模型；视频超限或下载失败时回退到静态图片；收到明确的 QQ Highway 上传失败响应时，也仅尝试一次静态图降级。发送响应超时、结果不确定时不会重发相册。
- **只发信息**：关闭 `isSendVideo` 时，图文也会回复作品信息，不再静默丢弃；视频详情是否附带封面 / 缩略图由各自的预览开关决定。
- **缺失数据**：统计和时长的 `null` 显示为“未知”，不伪装成 `0`。
- **大小 / 时长保护**：配置了限制但 DTK 未提供对应值时，保守跳过视频，避免绕过限制。两个限制都为 `0` 时允许下载未知大小 / 时长的视频。
- **下载失败**：按 DTK 返回的 CDN 镜像顺序重试；封面 / 缩略图下载或明确上传失败不影响文字，单张图片下载失败不影响相册其他项。
- **链接范围**：仅处理抖音 / TikTok 作品。主页、直播等非作品链接会给出明确提示。每条消息只处理第一个受支持的链接。

媒体由 **OneBot 后端（SnowLuma/NapCat）所在机器 / 容器**下载和发送。后端既需要访问平台 CDN 下载，也需要访问 QQ 媒体服务器上传；DTK 能解析、文字能发送不代表视频上传链路也可用。媒体请求带平台 Referer 和 User-Agent，不携带 DTK API Key。文件路径来自后端，不要求 AstrBot 与后端共享文件系统；临时文件生命周期由后端管理。

## 实现与验证

- `dtk_client.py`：独立于 AstrBot 的 v5 HTTP 客户端及作品模型。`POST /api/v1/parse` 只提交一次，随后轮询同一个 `/api/v1/tasks/{task_id}`，处理成功信封内的失败任务、鉴权错误和超时。
- `main.py`：QQ 事件、信息排版、媒体限制、OneBot 下载及发送。
- `onebot_media.py`：SnowLuma/NapCat 共用的动作处理。读取 `ActionFailed.result.wording` / `retcode`，区分 CDN 下载与 QQ Highway 上传失败；日志保留脱敏诊断，不在 QQ 中回显原始错误或密钥。
- `tests/`：模拟 v5 HTTP 响应和 AstrBot / SnowLuma / NapCat 的自动化回归测试，不需要启动 AstrBot。

运行本地回归：

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

可单独验证真实 DTK REST 链路，不触发 QQ 发送（会消耗 DTK 配额或命中缓存）：

```bash
export DTK_BASE_URL='http://127.0.0.1:8000'
# 在当前 shell 中填写自己的 Key，不要把实际值分享出去。
read -rsp 'DTK API Key: ' DTK_API_KEY; echo
export DTK_API_KEY
python scripts/check_dtk.py '你的作品链接或完整分享文案'
unset DTK_API_KEY
```

AstrBot + SnowLuma/NapCat + QQ 的实际收发仍需要真实环境验收，详见 [测试清单](docs/TESTING.md)。

## SnowLuma 的 QQ Highway 连接超时

若 SnowLuma 日志出现 `highway upload transport failed ... TCP connect timeout`，且分块为 `offset=0`，错误发生在**下载后的 QQ 上传 TCP 连接**，不是 DTK 解析或 CDN 下载。插件会显示具体失败阶段，并可附视频直链，但这不是对底层网络的修复。

已核对的 SnowLuma v1.14.22 通过 Node `net.createConnection(host, port)` 连接 QQ 返回的 Highway 地址，单次连接默认 10 秒、分块传输失败最多 3 次。仅调大插件的 `apiTimeout` / `mediaDownloadTimeout`、换 CDN 镜像、减小视频尺寸或设置普通 HTTP 代理，不会自动修复这条 TCP 路径。

请按 [SnowLuma 网络排查说明](docs/SNOWLUMA.md) 获取实际 Highway `host` / `port`，在 SnowLuma 所在网络环境运行 `scripts/check_highway.cjs` 验证。不要只测试宿主机，或将全部含会话数据的 trace 日志公开。
