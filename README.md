# Agent Governance PoC — Document Review Agent on AWS AgentCore

PoC：上傳 PDF / Excel → Agent 對照 Bedrock KB + Web 搜尋 → 產出 JSON 建議 + 寫回原檔（PDF 註解 / Excel comment）。

## 架構

```
input file (PDF/XLSX)
        │
        ▼
┌────────────────────────┐
│ AgentCore Runtime      │
│  Strands Agent         │
│   ├ read_input_file    │  (PyMuPDF / openpyxl)
│   ├ search_kb          │  ─► Bedrock Knowledge Base
│   ├ web_search         │  ─► Tavily
│   └ write_revised_file │  ─► outputs/ (+ optional S3)
└────────────────────────┘
        │
        ▼
JSON suggestions + 修訂版檔案
```

模型：Claude Opus 4.6 (Bedrock)。

## Quick Start

### 1. Python venv
```bash
cd /Users/cfh00910171/Desktop/agent_governance_poc
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. AWS credentials

第一次設定（建好 IAM user / access key 之後）：
```bash
source scripts/aws_session.sh init
```
這會跑 `aws configure --profile agentcore-poc`，填 access key + secret + region。

每天開工 / 每 8 小時換一次 MFA session：
```bash
source scripts/aws_session.sh mfa <6-digit-code>    # 從 MFA app 的 6 位數
```
> **必須用 `source`**，不能 `bash`。`mfa` 模式會 export `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_SESSION_TOKEN` 三個臨時憑證到當前 shell；用 `bash` 起 subshell 跑完就消失。

確認當前狀態：
```bash
source scripts/aws_session.sh status
```

> 設定值（profile name / region / MFA serial ARN）寫在 [`scripts/aws_session.sh`](scripts/aws_session.sh) 上方常數區。換人用就改那三行。

需要的 IAM 權限：
- `bedrock:InvokeModel*`
- `bedrock-agent-runtime:Retrieve*`
- `s3:GetObject`, `s3:PutObject`
- `bedrock-agentcore:*`
- `logs:*`

並在 Bedrock console > **Model access** 開通 Claude Opus 4.6 與 Titan Embeddings V2。

> 如果你的公司 AWS 設了 SCP 要求 MFA，直接 `aws configure` 後的 long-lived key 會被擋。請走上面的 `mfa` 流程。

### 3. 建立 Knowledge Base
參考 [`scripts/setup_kb.md`](scripts/setup_kb.md)。

### 4. 環境變數
```bash
cp .env.example .env
# 填入 KB_ID, TAVILY_API_KEY, BEDROCK_MODEL_ID 等
```
Tavily key：https://tavily.com (免費 1000 次/月)

### 5. 本地驗證
```bash
agentcore configure          # 首次：選 Strands、Bedrock、defaults
agentcore dev                # 啟動 local runtime (port 8080)

# 另一個 terminal
python scripts/invoke_local.py samples/input_sample.pdf
```
看 `outputs/` 是否有 `*_revised.pdf`。

### 6. 部署到 AgentCore Runtime
```bash
agentcore deploy
agentcore invoke '{"file_uri":"s3://<bucket>/input.pdf","file_type":"pdf"}'
```
CloudWatch logs 應顯示工具呼叫鏈。

## 專案結構
- `app/main.py` — Strands agent + `@app.entrypoint`
- `tools/` — file_reader / kb_search / web_search / file_writer
- `model/load.py` — Bedrock model ID
- `scripts/aws_session.sh` — `init` / `mfa` / `status`，sourceable，處理 MFA 換 session token
- `scripts/setup_kb.md` — KB 建立步驟
- `scripts/invoke_local.py` — 本地 smoke test

## Streaming Protocol (NDJSON)

`@app.entrypoint` 是 async generator，每行 yield 一個 JSON object（NDJSON）。Client 端逐行 parse、依 `type` 分支處理。

| `type` | 欄位 | 何時出現 |
|---|---|---|
| `start` | `file_uri`, `file_type`, `task` | 開頭一次 |
| `text` | `delta` (str) | LLM 文字 token 串流 |
| `tool_start` | `name`, `input` | 每個工具被呼叫時一次（已 dedup by `toolUseId`） |
| `result` | `data` (object) | 結束時，從最後 assistant message 的 ` ```json ` fence 解出的 JSON |
| `error` | `message` | 任何階段出錯（payload 缺欄位、agent 例外、parse 失敗） |

範例輸出：
```
{"type":"start","file_uri":"samples/x.pdf","file_type":"pdf","task":"review"}
{"type":"text","delta":"先讀取"}
{"type":"text","delta":"檔案..."}
{"type":"tool_start","name":"read_input_file","input":{"file_uri":"...","file_type":"pdf"}}
{"type":"tool_start","name":"search_knowledge_base","input":{"query":"..."}}
{"type":"tool_start","name":"write_revised_file","input":{...}}
{"type":"result","data":{"status":"ok","suggestions":[...],"revised_file_uri":"outputs/x_revised.pdf"}}
```

設計取捨：
- 故意**不**回傳 tool 結果（KB 命中筆數、Tavily 結果），減少協議複雜度與 token 噪音
- 最終結構化結果靠 system prompt 約束 LLM 用 ` ```json ` fence 包，再用 regex 抓最後一個 fence
- 若 LLM 沒輸出合法 fence，會回 `{"type":"error","message":"failed to parse final \`\`\`json block"}`

## Out of Scope (PoC)
未涵蓋：使用者認證 / multi-turn memory / CDK IaC / UI / Guardrails。
