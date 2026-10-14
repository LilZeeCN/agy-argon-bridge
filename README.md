# agy-argon-bridge ⚡

> **让 Antigravity CLI (`agy`) 原生接入 Google Cloud Vertex AI 旗舰模型 `Gemini 4 Argon`，合规挂载 300$ Free Trial 算力额度，全量透传本地 Agent 工具调用与多凭据安全轮转。**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![Backend: Vertex AI](https://img.shields.io/badge/Backend-Google_Cloud_Vertex_AI-4285F4.svg)](https://cloud.google.com/vertex-ai)
[![CLI: Antigravity](https://img.shields.io/badge/Client-Antigravity_CLI-8A2BE2.svg)](https://github.com/LilZeeCN/agy-argon-bridge)

<p align="center">
  <img src="assets/00_cover_pro_gemini_4_argon.jpg" alt="Cover" width="850"/>
</p>

---

## 📖 痛点与背景

Google 最新一代旗舰推理模型 **Gemini 4 Argon**（代号 Argon，具备极深的长思考链与高阶代码架构生成能力）在常规 Google AI Studio 中受配额与白名单策略限制——直接使用常规 API Key 调用常返回 `403 Restricted` 拒绝访问。

**而在 Google Cloud Vertex AI 商业云平台中，Argon 作为标准托管模型开放，并且 Google Cloud 为新开发者提供的 300$ Free Trial 体验额度可全额合规抵扣！**

然而，要想让 Google 官方终端 Agent 架构 **Antigravity CLI (`agy`)** 顺利跑在 Vertex AI 的 Argon 之上，传统的通用反向代理会遇到两大致命工程阻碍：

### 💥 阻碍 1：普通转发彻底破坏 Agent 本地工具调用
普通的 API 转发中间件仅传递文本与提示词，忽略了 `agy` 注入的 `tools`（包含 `run_command`、`view_file`、`write_to_file` 等）。这会导致模型丧失本地环境感知能力，退化为普通聊天机器人（例如输入本地路径时提示：“我是云端 AI，无法访问你的本地文件”）。

### 💥 阻碍 2：Go 客户端反序列化崩溃（RFC 4648 Base64 编码冲突）
当开启工具调用后，Vertex AI 返回的 `thoughtSignature`（思考签名）采用的是 **URL-Safe Base64**（包含 `-` 和 `_`）。而 `agy` 客户端是用 **Go 语言** 编写的，Go 标准库 `json.Unmarshal` 对 `[]uint8` / `[]byte` 仅接受 **RFC 4648 标准 Base64**（包含 `+` 和 `/`）。
未清洗的数据会导致 `agy` 客户端在第 6 位字节处瞬间崩溃报错：
```text
⚠ agent executor error: generating and executing: mapToStruct: error unmarshalling input:
json: cannot unmarshal string into Go struct field .candidates.0.content.parts.0.thoughtSignature of type []uint8: illegal base64 data at input byte 6
```

**`agy-argon-bridge` 专为彻底解决上述所有底层协议冲突与工具透传而生，提供企业级高可用的极简本地桥接方案！**

---

## 🚀 核心特性

- 💎 **原生锁定 `gemini-4-argon`**：将客户端请求路由至 Google Cloud Vertex AI 全球节点上的 Argon 旗舰模型。
- 💰 **合规抵扣 300$ 赠金**：使用官方 Application Default Credentials (ADC) 计费鉴权，所有 Token 消耗直接计入账户 300$ 免费赠金池。
- 🛠️ **全量 Agent 本地工具透传**：完美保留 `tools` 声明、系统指令（`systemInstruction`）与动态参数，支持 `run_command`、`view_file`、`write_to_file` 跨轮次链式执行。
- 🛡️ **智能 Base64 编码清洗**：内置双向协议转换，自动将 Vertex AI 的 URL-Safe Base64 `thoughtSignature` 规范化为 RFC 4648 标准 Base64，杜绝 Go 客户端反序列化异常。
- 🖥️ **macOS 开机自启常驻**：基于 `launchd` 实现后台静默守护，开机自启、崩溃自愈、零弹窗打扰。
- ⚡ **无感 CLI 体验**：终端输入 `agy` 自动无感拉起桥接服务并注入代理环境变量，与原生 CLI 使用习惯 100% 一致。

---

## 🏗️ 架构原理解析

```mermaid
flowchart TD
    subgraph Local["开发者本地环境 (macOS / Linux)"]
        CLI["Antigravity CLI (agy)<br/>Go 语言客户端"]
        Bridge["agy-argon-bridge (FastAPI)<br/>127.0.0.1:8050"]
        Launchd["macOS launchd 守护进程"]
        ADC["Google ADC 凭据<br/>~/.config/gcloud/..."]
    end

    subgraph GoogleCloud["Google Cloud Platform"]
        Vertex["Vertex AI API<br/>location: global"]
        Model["Gemini 4 Argon 旗舰模型"]
        Billing["300$ 免费体验额度结算池"]
    end

    Launchd -.->|保持常驻运行| Bridge
    CLI -->|"1. 声明本地 Tools (view_file / run_command)"| Bridge
    Bridge -->|"2. 转换请求透传 & 挂载 ADC"| Vertex
    Vertex -->|"3. 调度计算"| Model
    Model -->|"4. 抵扣账单 (全额覆盖)"| Billing
    Model -->|"5. 流式输出 + URL-Safe thoughtSignature"| Vertex
    Vertex -->|"6. 返回 SSE 流"| Bridge
    Bridge -->|"7. 格式清洗 (转为 RFC 4648 标准 Base64)"| CLI
    CLI -->|"8. 执行本地命令/读取视频并进入下一轮交互"| CLI
```

---

## ⚡ 极速安装与部署

### 1. 前置准备
1. 安装并登录 Google Cloud SDK (`gcloud`)：
   ```bash
   brew install --cask google-cloud-sdk
   gcloud auth login
   gcloud auth application-default login
   ```
2. 确保已在 Google Cloud 控制台激活了包含 300$ 体验额度的项目，并启用了 **Vertex AI API**。

### 2. 一键自动安装
克隆本项目并运行一键安装脚本：
```bash
git clone https://github.com/LilZeeCN/agy-argon-bridge.git
cd agy-argon-bridge
./install.sh
```

`install.sh` 脚本将全自动完成：
- Python 依赖检测与安装 (`fastapi`, `uvicorn`, `google-genai`)
- 检测并引导 gcloud ADC 凭据挂载
- 安装核心桥接脚本到 `~/.local/share/agy-argon`
- 注册 macOS `launchd` 开机自启守护服务（监听 `127.0.0.1:8050`）
- 在 `~/.zshrc` 中注入快捷 alias `alias agy="agy-argon"`

### 3. 生效与验证
重载配置文件或重新打开终端：
```bash
source ~/.zshrc
```
输入 `agy`，看到终端提示即代表已成功接入：
```text
✨ 已接入 Vertex AI 上的 Gemini 4 Argon 模型（全额计入 300$ 赠金）
```

---

## 🔬 实测验证与证据链

本项目经过极其严苛的工程化验证，以下是实测全链路证据：

### 1. 300$ 体验额度实测到账
<p align="center">
  <img src="assets/01_gcp_300_credits.png" alt="300$ Credits" width="750"/>
</p>

### 2. Google Cloud Agent Studio 中 Argon 状态确认
<p align="center">
  <img src="assets/02_agent_studio_argon.png" alt="Agent Studio" width="750"/>
</p>

### 3. 本地全量 Agent 工具调用实测（8.14MB 原生视频解析）
我们测试直接在 `agy` 中传入一个高达 **8.14MB** 的原生本地视频 `zee-rp-github-inline.mp4`：
```bash
agy -p "/Users/lilzee/axure-exp/zee-rp-video-20261013/out/zee-rp-github-inline.mp4 你能看到这个视频吗？"
```
**实测输出结果**：
- 模型自动触发本地 `view_file` 工具读取二进制视频；
- Go 客户端成功反序列化清洗后的 `thoughtSignature`，零崩溃；
- 完整提取出编码参数（`H.264 1920x1080 30fps`, `AAC 48kHz`）；
- 一字不差解析出全片 5 幕（ACT 01 ~ ACT 05）的所有分镜头台词、粒子动态、文字排版与演示命令！

### 4. Vertex AI 元数据与 GCP Logs Explorer 取证
在 Google Cloud 控制台的日志浏览器中，可以清晰核验模型名称完全为 `gemini-4-argon`，所有调用耗用均从 Vertex AI 300$ 赠金中扣减：
<p align="center">
  <img src="assets/06_vertex_ai_metadata_argon_proof.png" alt="Argon Proof" width="750"/>
</p>

---

## 🔄 企业级多项目 / 多凭据安全轮转方案 (Credential Rotation)

当特定开发项目或团队账号的额度耗尽、需要平滑切换到新的工作空间时，无需改动任何业务代码，通过标准的 ADC 凭据切换即可实现无缝轮转：

1. **配置新项目**：在新账号/新项目中启用 Vertex AI API，并记录新的 Project ID。
2. **刷新本地凭证**：在终端执行官方授权更新：
   ```bash
   gcloud auth application-default login
   gcloud config set project <NEW_PROJECT_ID>
   ```
3. **即刻重载**：重启后台守护服务：
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.antigravity.argon-bridge.plist
   launchctl load ~/Library/LaunchAgents/com.antigravity.argon-bridge.plist
   ```
桥接服务具备自动项目探知机制，可毫秒级感知新凭据与项目变更。

---

## ⚖️ 免责声明与风险合规防范 (Disclaimer & Compliance)

> [!IMPORTANT]
> 请在部署使用本项目前仔细阅读以下合规指引与风险防范说明：

1. **官方协议与合规性**：
   - 本项目属于本地开发辅助中间件，完全基于 Google Cloud 官方发布的公开 SDK（`google-genai`）及标准 Application Default Credentials (ADC) 鉴权体系开发。
   - 本项目不包含任何逆向破解、通信嗅探、越权访问或破坏 Google Cloud 服务防护措施的技术实现。
2. **计费安全与预算预警设置（防非预期扣费）**：
   - Google Cloud 的 300$ Free Trial 额度有效期为 90 天。为保障财务安全，**强烈建议所有使用者在 Google Cloud 控制台设置预算告警**：
     - 进入 GCP 控制台 -> `Billing` (结算) -> `Budgets & alerts` (预算与提醒)。
     - 创建一个额度为 `$250` 的告警规则，勾选邮件通知。
     - 一旦体验额度接近临界值，及时收到告警并停止调用，避免产生信用卡真实扣费。
3. **账户风控防范**：
   - 切换项目或多凭证轮转时，请务必使用正规、合法的个人或企业 Google 账号。请勿使用批量虚拟卡、假名或违规自动化脚本注册多账号，以免触发 Google Payments 反欺诈与风控策略。
4. **服务范围与免责**：
   - 本项目为开源实验项目，按“现状”（AS-IS）提供。使用者需自行承担调用 Vertex AI 产生的任何额度消耗或费用责任。开发者不对因使用本项目导致的任何账号限制、数据损失或商业后果承担法律责任。

---

## 🛠️ 故障排查 (FAQ)

### Q: 为什么我在 agy 运行提示 "cannot unmarshal string into Go struct"?
A: 请确保使用的是本项目最新版本的 `gemini_argon_bridge.py`。早期版本未将 `thoughtSignature` 从 URL-Safe Base64 转换为标准 Base64，更新后执行 `launchctl reload` 即可恢复正常。

### Q: 提示 8050 端口被占用怎么办？
A: 可以通过环境变量自定义端口：
```bash
export ARGON_BRIDGE_PORT=8055
agy
```

### Q: 如何彻底卸载？
A: 运行仓库根目录下的卸载脚本即可一键清理：
```bash
./uninstall.sh
```

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 授权，欢迎自由 Star、Fork、提交 PR 与持续交流！
