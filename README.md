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

雲端不能讀本地 `.env`（已在 `.dockerignore`）。機密與設定分兩條路注入：

**6a. 機密 → AgentCore Identity（理想做法，但目前被 SCP 擋）**

正式做法是把 `TAVILY_API_KEY` 存進 AgentCore Identity 的 API Key Credential Provider，agent 內由 `@requires_api_key` 在 cold start 取出。底層仍是 Secrets Manager，但走 agent-aware 介面（cross-agent 隔離、審計）。

```bash
./scripts/setup-identity.sh        # 互動輸入 Tavily key，建 tavily-provider
```

Runtime execution role 需要：
```
bedrock-agentcore:GetWorkloadAccessToken
bedrock-agentcore:GetResourceApiKey
```
（`agentcore configure --auto-create-role` 會自動帶；手動 role 要自己加。）

> ⚠️ **目前障礙**：本專案 AWS 帳號的組織 SCP 拒絕 `bedrock-agentcore:CreateApiKeyCredentialProvider`，無論 region。`./scripts/setup-identity.sh` 會跑出 `AccessDeniedException ... explicit deny in a service control policy`。
>
> **長期解**：請組織 admin 用具備該權限的身份代為執行：
> ```bash
> aws bedrock-agentcore-control create-api-key-credential-provider \
>   --name tavily-provider \
>   --api-key <Tavily key> \
>   --region us-west-2
> ```
> Provider 建好後，本帳號 IAM user 只需要 `bedrock-agentcore:GetResourceApiKey` 讀取權限（execution role 已帶）就能用。
>
> **短期解**（目前 PoC 採用）：把 Tavily key 改用 `--env` 注入，繞過 Identity。見 6b。

**6b. 非機密設定 → `agentcore launch --env`**

當前 PoC 使用的指令（含 6a 的 fallback：`TAVILY_API_KEY` 也走 `--env`）：

```bash
# 把 .env 的值灌進當前 shell 變數（一次性）
set -a; source .env; set +a

agentcore configure --entrypoint app/main.py --auto-create-role --region us-west-2  # 首次
agentcore launch \
  --env BEDROCK_MODEL_ID="$BEDROCK_MODEL_ID" \
  --env KB_ID="$KB_ID" \
  --env S3_BUCKET="$S3_BUCKET" \
  --env TAVILY_API_KEY="$TAVILY_API_KEY"     # ← Identity 通了之後拿掉這行
# AWS_REGION 由 Runtime 自動注入

agentcore invoke '{"file_uri":"s3://<bucket>/inputs/input.pdf","file_type":"pdf"}'
```

> Identity 通了之後切回正規路徑：刪掉 `--env TAVILY_API_KEY`，code 不用改 — `tools/web_search.py` 的 `prewarm_key()` 邏輯是「env var 優先 → 沒有才打 Identity」，env 被清掉就會自然走 Identity。

Runtime execution role 需要的 IAM policy（除了 `agentcore configure --auto-create-role` 預設帶的之外，還要手動加 inline policy）：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"ReadInputs","Effect":"Allow","Action":"s3:GetObject",
     "Resource":"arn:aws:s3:::<your-bucket>/inputs/*"},
    {"Sid":"WriteOutputs","Effect":"Allow","Action":"s3:PutObject",
     "Resource":"arn:aws:s3:::<your-bucket>/outputs/*"},
    {"Sid":"RetrieveKB","Effect":"Allow",
     "Action":["bedrock:Retrieve","bedrock:RetrieveAndGenerate"],
     "Resource":"arn:aws:bedrock:us-west-2:<account>:knowledge-base/<KB_ID>"}
  ]
}
```

CloudWatch logs 應顯示工具呼叫鏈。

> 本地開發仍用 `.env` — `tools/web_search.py` 先讀 `TAVILY_API_KEY` env var，找不到才走 AgentCore Identity，所以本地不會打到雲端 API。
> Cold start 時 [`app/main.py`](app/main.py) 的 `invoke()` 會 await 一次 `prewarm_key()`，把 key 灌進 `tools/web_search.py` 的 module cache，後續 sync `web_search` tool 直接讀取。

## S3 Layout

雲端部署時固定用 **單一 bucket + 兩個 prefix**：

```
s3://$S3_BUCKET/
├── inputs/<filename>.{pdf,xlsx}        ← 呼叫端先 putObject 到這裡
└── outputs/<filename>_revised_<UTC-timestamp>.{pdf,xlsx}  ← agent 寫回此處，URI 放在 result.revised_file_uri
```

呼叫範例：
```bash
aws s3 cp samples/input_sample.pdf s3://$S3_BUCKET/inputs/
agentcore invoke '{"file_uri":"s3://'$S3_BUCKET'/inputs/input_sample.pdf","file_type":"pdf"}'
# result.revised_file_uri => s3://$S3_BUCKET/outputs/input_sample_revised_20260504T044812Z.pdf
```

本地開發 `S3_BUCKET` 留空即可 — `tools/file_writer.py` 的 `_maybe_upload` 偵測沒設就只寫 `outputs/` 本地目錄。

> PoC 階段直接回傳 `s3://` URI，假設呼叫端有讀 bucket 的權限。若未來 client 沒有 AWS 身份，再改成回 presigned URL。

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
