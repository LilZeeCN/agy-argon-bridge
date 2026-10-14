# agy-argon-bridge

将 Antigravity CLI (`agy`) 的 Gemini 文本请求桥接到 Google Cloud Vertex AI 的 `gemini-4-argon`，保留系统指令、工具调用和结构化输出配置。

客户端显示的 Flash 标签可能仍保留；实际后端以 Google 返回的 `modelVersion` 为准。图片生成请求保留原图片模型，不能交给 Argon 文本模型。

## 安装

需要 Python 3.10+、已安装的原版 `agy`、Google Cloud SDK，以及有 Vertex AI 权限且已启用相关 API 的项目。

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
git clone https://github.com/LilZeeCN/agy-argon-bridge.git
cd agy-argon-bridge
./install.sh
source ~/.zshrc
agy
```

安装器将依赖放入 `~/.local/share/agy-argon/venv`，不改全局 Python 环境；项目、区域和端口保存在 `config.env`，macOS 的 launchd 服务也保存相同环境。依赖安装或健康检查失败会中止。项目解析优先使用环境变量，其次为 gcloud 活跃项目、ADC 的默认或 quota project；无法确定时停止，不回退到作者项目。

## 验证真实模型

```bash
python3 bin/verify_argon.py
```

此命令会产生少量 API 用量，并输出 Google 的 `modelVersion`、`responseId` 和用量。`modelVersion` 应为 `gemini-4-argon`。本地 `/healthz` 只确认桥接进程就绪，其 `last_response` 保存最近一帧真实云端响应元数据；启动提示不能替代云端验证。服务日志记录模型和用量元数据，不记录提示词、签名或凭据。

工具调用还应单独验证，例如让 AGY 读取一个包含随机文本的本地文件，并核对返回内容。文本生成通过，不等于所有工具、图片或视频行为均已验证。

2026-10-14 已用原版 AGY 1.3.5 完成 17 项真实功能测试，包括 Skill 自动触发、`/技能名` 手动调用、参考文件读取与附带脚本执行。逐项结果、测试版本及未覆盖范围见 [兼容性验证记录](docs/compatibility.md)。

## 路由与配置

- Gemini 文本和辅助文本请求映射到 Argon；图片模型请求保留原模型，使用同一 Vertex AI 项目，是否可调用取决于该模型在项目中的可用性。
- 保留 `tools`、`toolConfig`、`systemInstruction`、`safetySettings`，以及 SDK 支持的生成配置，包括 `thinkingConfig`、`responseMimeType` 和 schema。
- Argon 支持 LOW、MEDIUM、HIGH。实际 AGY 1.3.5 会将这三个级别表示为旧预算 1000、4000、-1，桥接器将它们映射回对应级别；明确的 `thinkingLevel` 优先，MINIMAL 转换为 LOW。其他旧预算被移除并使用 Argon 默认 MEDIUM。原本的预算数值不等于新的推理级别。
- Argon 忽略 temperature、topP 和 topK，桥接器会移除这三个无效参数。
- 异步云端请求设有 120 秒网络超时，不阻塞健康检查。客户端断开会关闭流；首帧前的上游错误保留 HTTP 状态码，首帧后的错误只能通过 SSE 报告。
- 签名与图片字节经 SDK JSON 序列化可能成为 URL-Safe Base64；`thoughtSignature` 和 `inlineData.data` 转换为 Go 客户端支持的标准 Base64，保留原始字节，不修改工具参数中的普通 `data` 字段。

## 计费与赠金证据

ADC 负责鉴权；调用 Vertex AI 不等于每次费用一定由赠金支付。需要在同一项目的 Cloud Billing 中核对结算账号、赠金的有效期和适用范围、实际服务/SKU 费用及促销抵扣明细。`trafficType=ON_DEMAND` 不是赠金标记，银行卡未扣款也不能证明赠金抵扣。

较强的证据应同时包含同一项目的模型用量费用、对应的 `PROMOTION`/免费赠金抵扣和净费用。赠金余额减少只能证明该结算账号已使用赠金，不能单独证明每一条 Argon 请求已抵扣。费用明细通常一天内出现，有时超过 24 小时。

官方资料：[Argon 安装与迁移](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/get-started-with-gemini-4?hl=en)、[Cloud Billing 报告与延迟](https://docs.cloud.google.com/billing/docs/how-to/reports)、[账单导出与抵扣类型](https://docs.cloud.google.com/billing/docs/how-to/bq-examples)、[Google Cloud Free Program](https://docs.cloud.google.com/free/docs/free-cloud-features)。

## 更换项目或凭据

没有自动多账号轮转。更换 ADC 后，明确设置新的项目并重新安装以更新运行环境和重启服务：

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=NEW_PROJECT_ID
./install.sh
```

## 故障排查

日志：`~/.local/share/agy-argon/bridge.stdout.log`、`bridge.stderr.log`；启动器自行启动的进程使用 `bridge.log`。端口已监听但健康接口无响应时，启动器会报错，不会声称成功。

更改默认端口后重新安装，macOS 服务和启动器才会一致：

```bash
export ARGON_BRIDGE_PORT=8055
./install.sh
python3 bin/verify_argon.py --port 8055
```

只对单次命令指定另一个端口，会启动该端口上的独立桥接进程，不改变已注册的 launchd 服务。

## 开发验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
bash -n install.sh uninstall.sh bin/agy-argon
```

回归覆盖并发健康检查、图片路由和字节编码、系统/工具 token 计算、上游错误状态、签名字节往返、推理级别映射、生成配置及项目解析。真实云端调用与账单抵扣需独立核验。

运行 `./uninstall.sh` 可移除 macOS 服务和安装文件；按脚本提示移除 shell alias。自行启动的独立进程需要先停止。

MIT License。此项目是实验性本地桥接器，实际模型可用性和计费以 Google Cloud 的权限、服务状态及账单为准。
