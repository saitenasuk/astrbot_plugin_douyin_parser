# SnowLuma 兼容性与 Highway 网络排查

## 已核对的接口

基于 SnowLuma v1.14.22，源码提交 `87527cb7641a5a42f8f0efb73cb066102e004dee`：

- `packages/onebot/src/actions/extended.ts`：`download_file` 支持 `url` 和 `headers` 字符串列表（`Referer=...`），返回 `{file}`；文件保存在后端的 `data/downloads`。
- `packages/protocol/src/highway/utils.ts`：支持 `file://` URL 并做路径解码。插件直接调用后端发送，不依赖 AstrBot 访问后端本地文件。
- `packages/onebot/src/types.ts`：失败响应带 `status=failed`、`retcode`、`wording`。AstrBot 的 aiocqhttp 会将其放到 `ActionFailed.result`，插件现在保留脱敏后的诊断。
- `packages/protocol/src/highway/highway-client.ts`：Highway 上传通过 Node 原生 TCP 连接到 QQ 返回的 `host` / `port`。单次连接默认超时 10 秒；传输失败最多尝试 3 次。

插件同时保留 NapCat 兼容性，不需要配置专属后端类型；这不代表所有 OneBot 实现都支持 `download_file` 或视频合并转发。

## 此次日志说明了什么

```text
cmdId=1005 chunk=1048576/39116665 offset=0
Error: TCP connect timeout
```

媒体已经进入 SnowLuma 的 QQ 视频上传流程。首个分块、偏移 0 的 TCP 连接就失败，并非上传到一半后超限，也不能据此认定编码错误。单纯更换 DTK 下载接口、减小文件、延长 DTK 等待或 CDN 下载超时，无法修复已经失败的 TCP 建连。

文字消息和媒体上传不是同一条数据通道。已有图片也可能命中服务器缓存，不需要完整上传。因此“文字 / 封面能发送”不等于“QQ Highway 可达”。

## 在正确的网络环境检查

1. **先记录版本和部署方式。** 在 OneBot 调试入口调用 `get_version_info`，记录 `app_version`；说明是否 Docker、宿主机网络 / bridge 网络、是否使用代理。
2. **临时开启 SnowLuma trace。** 启动环境变量为 `SNOWLUMA_LOG_LEVEL=trace`；也可使用支持调整运行日志级别的 WebUI。只复现一次，不要长期保留大量 trace。
3. **找到目标地址。** 从 `highway_put_start` 记录中提取该次视频的 `host`、`port`、`cmdId`、`totalBytes`。不要只根据域名猜测固定的端口，QQ 可以返回不同 IP 和端口。
4. **在 SnowLuma 进程所在网络命名空间运行 TCP 检查。** TCP 检查不发送 QQ 账号、票据、文件或聊天消息，也不验证 QQ 的上传鉴权。

宿主机部署可直接运行：

```bash
node scripts/check_highway.cjs '<trace 中的 host>' '<trace 中的 port>'
```

Docker 部署示例，替换容器名、host 和 port：

```bash
docker cp scripts/check_highway.cjs <snowluma容器名>:/tmp/check_highway.cjs
docker exec <snowluma容器名> node /tmp/check_highway.cjs '<host>' '<port>'
```

结果为 JSON：

- `status=connected`：该次 TCP 建连成功。只证明这一步，不证明 QQ 上传一定成功；如仍失败，需要后续上传拒绝码或读超时日志。
- `TCP_CONNECT_TIMEOUT`：检查容器路由、防火墙、出口策略及覆盖这条 TCP 路径的代理 / 网络规则。
- `ECONNREFUSED`：目标明确拒绝连接，检查是否使用了本次日志中的正确端口，以及后端选择的节点是否可用。
- `ENOTFOUND` / `EAI_AGAIN`：检查相同容器的 DNS。若日志本身是 IP 地址，DNS 不是该次目标地址的解析问题。

SnowLuma 的这段 Highway 上传代码直接使用 `net.createConnection`，没有读取 `HTTP_PROXY` / `HTTPS_PROXY` 的逻辑。仅设这些环境变量不等于这条 TCP 连接已走代理。不要为了排查直接关闭防火墙或暴露 OneBot/WebUI；应针对实际目标地址和端口检查出站连接。

## 插件侧的降级边界

- 普通视频失败时可返回作品链接及短期有效媒体直链（`sendLinkOnFailure`，默认开启），**不会反复重传视频**。
- 收到明确的 Highway 上传失败响应时，封面尝试改发文字；实况图尝试改发静态图，最多一次。
- OneBot 响应超时、无法确定是否已经发送时，不自动重发相册，避免重复消息。
- 静态图上传也不可达时仍会失败；降级不等于修复网络。
- 直链可能过期或受平台 Referer / 访问规则限制；插件的媒体大小 / 时长保护不会通过直链降级被绕过。

## 提交排查信息时

只需要提供：SnowLuma `app_version`、部署方式、是否代理、相关 `host` / `port`、TCP 检查结果、脱敏后的插件诊断。不要公开整个 trace、`headHex`、`sigSession`、`sessionKey`、Cookie、Token 或 OneBot access token。
