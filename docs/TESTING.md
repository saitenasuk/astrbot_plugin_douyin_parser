# DTK v5 迁移验收清单

## 本地自动化检查

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m compileall -q main.py dtk_client.py onebot_media.py scripts tests
```

自动测试模拟 AstrBot 及 SnowLuma/NapCat OneBot 后端，不会发送 QQ 消息，也不能证明 QQ 平台一定接受视频或合并转发。覆盖范围包括：v5 鉴权和异步任务、标准媒体模型、统计缺失、限制边界、图文及实况回退、群聊 / 私聊路由、CDN 镜像、共享下载并发、SnowLuma 错误响应与错误脱敏、明确上传失败后的静态图降级，以及超时结果不确定时不重复发送。

## 无 QQ 的真实 REST 自检（可选）

在 AstrBot 可以访问 DTK 的环境中安装 `requirements.txt`，设置 `DTK_BASE_URL`、`DTK_API_KEY`，运行：

```bash
python scripts/check_dtk.py '有效作品链接'
```

输出作品 id、类型、时长、视频大小及图片数量，**不会下载媒体或发送消息**。

- `UNAUTHENTICATED`：核对 Key，确认未被撤销或过期。
- `FORBIDDEN_SCOPE`：为 Key 添加对应平台的读取权限。
- `IDENTITY_POOL_EXHAUSTED`：检查对应平台的身份池，不要反复重试同一链接。
- `INVALID_RESPONSE`：核对地址是否确实为 v5 API，排除误填 `/mcp/`、反向代理重定向或返回控制台 HTML。
- `TASK_TIMEOUT`：检查 DTK worker / 队列；原任务仍可能在运行。日志中的 task id 可以在 DTK 控制台追踪，不会自动重新提交或取消。

## AstrBot + QQ 真实验收（交由使用者执行）

先重载插件，确认配置里的新选项可见。建议先用测试群 / 私聊，开启 `isSendVideo`，只使用自己有权访问和转发的作品。

| 场景 | 操作与预期 |
| --- | --- |
| 抖音视频 | 发送有效短链和完整视频链接；均收到正确标题、作者、时长及可播放视频 |
| 分享文案 | 整段粘贴平台分享文案，链接后有中文标点；正常识别，无需手工删前后文字 |
| 封面开关 | 开启 `isThumbs` 有封面 / 缩略图，关闭无封面，视频发送不受影响 |
| 仅信息 | 关闭 `isSendVideo`；视频、单图、多图均收到作品信息，不发送作品媒体 |
| 单张静态图 | 开启媒体发送；收到图片和信息，不应没有任何回复 |
| 多张静态图 | 收到合并转发，首条为信息，图片顺序与原作相同 |
| 实况图 | 有 clip 时发送可播放视频；超限、下载失败或明确 Highway 上传失败时收到静态图及回退说明；降级只尝试一次 |
| 时长限制 | 将 `videoMaxDuration` 临时设得很小；先收到信息，再收到跳过说明，不下载原视频 |
| 大小限制 | 将 `videoMaxSize` 临时设得很小；超限 / 大小未知时收到跳过说明 |
| 私聊 | 测试视频及多图；消息进入私聊，不误投群聊 |
| 无效作品 | 删除、私密或无权限作品收到简洁错误，插件不抛出未处理异常 |
| Key 错误 | 临时改为无效 Key，收到认证提示；恢复 Key 并重载后可以解析 |
| TikTok | 在 DTK 配好 TikTok 身份和 Key scope 后，分别测试视频和 photo 图文 |
| 并发图文 | 同时发送两条多图作品；下载并发保持配置上限，机器人不因无界下载失去响应 |
| 容器 / Windows | 如 SnowLuma/NapCat 和 AstrBot 不在同一容器或系统，确认 `download_file` 返回的本地路径仍能正常发送 |

若失败，请提供：场景、可公开的作品链接、插件日志中的错误码 / request id / task id、AstrBot / SnowLuma（或 NapCat）/ DTK 版本，以及插件配置的**脱敏**截图。不要提供 API Key、Cookie、Authorization 请求头或完整身份数据。

### SnowLuma 故障复测

1. 使用之前失败的同一视频，保持原大小 / 时长限制；检查普通作品详情仍正常发送。
2. 如 QQ 上传仍失败，插件日志应包含 `action=send_group_msg`、`reason=HIGHWAY_CONNECT_TIMEOUT`、实际 `retcode` 和脱敏后的错误描述，而不是只有 `ActionFailed`。
3. 开启 `sendLinkOnFailure` 时，失败提示应附作品链接和媒体直链；关闭则只有提示。超限跳过的视频不提供绕过限制的媒体直链。
4. 有实况图的相册出现明确上传失败时，应仅尝试一次静态图降级；如果静态图上传也失败，则停止，不无限重试。
5. 按 [网络排查说明](SNOWLUMA.md) 在 SnowLuma 所在容器测实际 Highway 地址。只有 TCP 连接通了仍失败时，才继续检查 QQ 上传拒绝码、会话或后端实现。
