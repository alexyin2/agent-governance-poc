# Agent Governance PoC — Document Review Agent on AWS AgentCore

PoC：使用者下一段自然語言 `instruction`（**必填**），可選擇性附上 PDF / Excel。Agent 多模態看懂內容（文字 + 版面 + 圖表），依 instruction 自主決定要不要查 Bedrock KB、要不要 web 搜尋、要不要把意見寫回原檔（PDF 註解 / Excel comment）—— 沒有強制流程，純諮詢、摘要、政策審查、跨檔比對都走同一個入口。

## 架構

```
payload: { instruction (required), files? (PDF/XLSX), actor_id?, session_id? }
        │  files 預載為 Bedrock document content blocks，Claude 直接看到視覺
        ▼
┌──────────────────────────────┐
│ AgentCore Runtime            │
│  Strands Agent (Sonnet 4.6)  │   ← 依 instruction 自主決定要呼叫哪些工具
│   ├ load_file                │   多模態載入跨輪 history 提到的檔案
│   ├ inspect_pdf_page         │   PyMuPDF — 單頁 text blocks + bbox
│   ├ inspect_xlsx_sheet       │   openpyxl — sheet 結構 + A1 column letter
│   ├ search_knowledge_base    │   Bedrock KB
│   ├ web_search               │   Tavily（KB 沒有時才用）
│   ├ annotate_pdf             │   寫回 PDF sticky + highlight
│   └ annotate_xlsx            │   寫回 Excel cell comment（merged cell 自動 redirect）
└──────────────────────────────┘
        │  NDJSON 串流（start / text / tool_start / result / error）
        ▼
最終 result：answer + outputs（修訂版檔案 URI；純諮詢時為空陣列）
```

關鍵設計：
- **Instruction-driven，非線性**：`instruction` 是**必填**且是唯一的行為驅動源。所有工具一次全部 register 給 agent，沒有 if/else 分流；agent 依 instruction 語意自己決定要不要查 KB、要不要寫回、要不要 inspect。同一個入口可處理摘要、政策審查、跨檔比對、純諮詢等情境。
- **檔案是選填素材**：`files` 可省略（純諮詢、跨輪追問用）；instruction 永遠不能省。舊欄位 `file_uri` / `file_type` / `task` / `mode` 已不支援，會立刻 reject。
- **多模態預載**：payload 的 `files` 全部 pre-load 為 Bedrock `document` content block，agent 一開始就「看到」整份 PDF/Excel（含圖表、版面）。`s3://` 來源會由 agent 進程下載成 bytes 後 inline（Bedrock Converse 對 Anthropic Claude 不支援 `s3Location` 作為 DocumentSource，只有 Nova 系列支援）。
- **PDF 三種定位寫入**：`annotate_pdf` 的 suggestion 接受 `bbox`（最精準）/ `anchor_text`（≥8 字、含上下文識別符）/ `region`（視覺元件），優先序 bbox > anchor_text > region。
- **PDF / Excel 拆分**：`annotate_pdf` 與 `annotate_xlsx` 是兩個工具，各自只 own 一種 schema，避免跨格式欄位混淆。
- **內文欄位強制 `text`**：annotate suggestion 的內文 key 必須是 `text`（不是 `comment` / `body` / `content` / `note`），prompt 與 docstring 雙重約束。

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

並在 Bedrock console > **Model access** 開通 Claude Sonnet 4.6 與 Titan Embeddings V2。

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

**6a. 機密 → AgentCore Identity**

`TAVILY_API_KEY` 存進 AgentCore Identity 的 API Key Credential Provider，agent 內由 `@requires_api_key(provider_name="tavily-provider")` 在 cold start 取出（[tools/web_search.py](tools/web_search.py)）。底層是 Secrets Manager，但走 agent-aware 介面（cross-agent 隔離、審計）。

> ⚠️ **CLI / SDK 路徑被 SCP 擋住**：本帳號的組織 SCP 拒絕 `bedrock-agentcore:CreateApiKeyCredentialProvider`（任何 region）。
> `./scripts/setup-identity.sh` 跟 `aws bedrock-agentcore-control create-api-key-credential-provider` 兩條都會撞 `AccessDeniedException ... explicit deny in a service control policy`。
>
> **可行做法 — Console 建立**（已驗證）：
> 1. AWS Console → **Bedrock AgentCore** → **Identity** → **Create credential provider**
> 2. Type 選 `API Key`
> 3. Name 填 **`tavily-provider`**（**必須**跟 code 對齊）
> 4. API secret 貼 Tavily key
> 5. Create
>
> Console 走的內部 path 不在 SCP 黑名單，可以直接過。Provider ARN 大約長這樣：
> `arn:aws:bedrock-agentcore:us-west-2:<account>:token-vault/default/apikeycredentialprovider/tavily-provider`

Runtime execution role 需要的 Identity 權限（`agentcore configure --auto-create-role` 會自動帶；手動 role 要自己加）：
```
bedrock-agentcore:GetWorkloadAccessToken
bedrock-agentcore:GetResourceApiKey
```

**6b. 非機密設定 → `agentcore launch --env`**

```bash
# 把 .env 的值灌進當前 shell 變數（一次性）
set -a; source .env; set +a

agentcore configure --entrypoint app/main.py --auto-create-role --region us-west-2  # 首次
agentcore launch \
  --env BEDROCK_MODEL_ID="$BEDROCK_MODEL_ID" \
  --env KB_ID="$KB_ID" \
  --env S3_BUCKET="$S3_BUCKET"
# AWS_REGION 由 Runtime 自動注入；TAVILY_API_KEY 走 Identity 不傳

agentcore invoke '{
  "actor_id":"alex",
  "instruction":"請依公司政策審查這份 CAB 申請",
  "files":[{"uri":"s3://<bucket>/inputs/cab.pdf","type":"pdf"}]
}'

# 多檔同時審查（agent 自動做 cross-file 一致性檢查）：
agentcore invoke '{
  "actor_id":"alex",
  "instruction":"審查 CAB 並對照填寫的檢核表",
  "files": [
    {"uri":"s3://<bucket>/inputs/cab.pdf",       "type":"pdf"},
    {"uri":"s3://<bucket>/inputs/checklist.xlsx","type":"xlsx"}
  ]
}'
```

**Payload schema**：
- `actor_id` (optional, str) — 啟用 memory（偏好注入、recent history）。缺則 stateless。
- `instruction` (**required**, str) — 自然語言指令，agent 依此決定要做什麼。
- `files` (optional, list) — `[{uri, type?}]`，`type` 可省略（從副檔名自動推斷成 `"pdf"` 或 `"xlsx"`）。可空，純諮詢時不附即可。
- `session_id` (optional, str) — 帶上同一個 id 表示接續 session（會載入 `<recent_history>`）。

> 舊欄位 `file_uri` / `file_type` / `task` / `mode` 已不支援，會立刻 reject。

> 萬一 Console 建 provider 也被擋（公司未來改 SCP 範圍可能）：暫時加 `--env TAVILY_API_KEY="$TAVILY_API_KEY"` 走 env-var 後備路徑。`prewarm_key()` 邏輯是「env var 優先 → 沒有才打 Identity」，env 被清掉就會自然走 Identity，code 不用改。

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
└── outputs/<filename>_revised_<UTC-timestamp>.{pdf,xlsx}  ← agent 寫回此處，URI 放在 result.outputs[].output_uri
```

呼叫範例：
```bash
aws s3 cp samples/aies_cab1.pdf  s3://$S3_BUCKET/inputs/
aws s3 cp samples/aies_cab1.xlsx s3://$S3_BUCKET/inputs/

agentcore invoke '{
  "actor_id":"alex",
  "instruction":"審查 CAB 並對照填寫的檢核表",
  "files":[
    {"uri":"s3://'$S3_BUCKET'/inputs/aies_cab1.pdf","type":"pdf"},
    {"uri":"s3://'$S3_BUCKET'/inputs/aies_cab1.xlsx","type":"xlsx"}
  ]
}'
# result.outputs[*].output_uri => s3://$S3_BUCKET/outputs/aies_cab1_revised_20260504T044812Z.pdf 等
```

> Bedrock Converse 對 Anthropic Claude 不支援 `s3Location` 作為 DocumentSource，所以 agent 進程會用 Runtime execution role 的 `s3:GetObject` 權限把檔案下載成 bytes 後 inline 進 content block。`annotate_pdf` / `annotate_xlsx` 也要把檔案抓回來修改後 PutObject 回 `outputs/`。

本地開發 `S3_BUCKET` 留空即可 — `tools/file_writer.py` 的 `_maybe_upload` 偵測沒設就只寫 `outputs/` 本地目錄。

> PoC 階段直接回傳 `s3://` URI，假設呼叫端有讀 bucket 的權限。若未來 client 沒有 AWS 身份，再改成回 presigned URL。

## 使用情境範例

`instruction` 是必填、files 是選填；下列只是 agent 可能自行選擇的工具組合，**並非預先寫死的 mode**：

| 情境 | payload 重點 | agent 可能採取的動作 |
|---|---|---|
| 純諮詢 | instruction 問題，無 files | 直接答 → `outputs:[]`、`cross_findings:"不適用"` |
| 純摘要 | 「總結這份 CAB 的範疇與目的」+ pdf | 看預載內容 → 答 → `outputs:[]` |
| 政策審查 | 「依公司政策審查這份 CAB 並加註記」+ pdf | `search_knowledge_base` → `annotate_pdf` |
| 跨輪追問 | （Turn 2，無 files）「剛剛那份 PDF 第 3 頁細節給意見」+ session_id | `load_file`（從 `<recent_history>` 取 URI）→ 答 |
| 逐題審查 | 「對檢核表每題填答給 pass/fail 與依據」+ [pdf, xlsx] | `inspect_xlsx_sheet` → `annotate_xlsx` 每 row 一個 comment |
| 跨檔比對 | 「審查 CAB 並對照填寫的檢核表」+ [pdf, xlsx] | 看雙檔 → 視需求查 KB → 註記 + `cross_findings` 摘要不一致處 |

## 專案結構
- `app/main.py` — Strands agent + `@app.entrypoint`，組 multimodal ContentBlocks + 工具註冊
- `app/prompts/system.md` — 外部化的 system prompt（XML-tag 分區）
- `tools/file_loader.py` — `load_file`：多模態載入 PDF/Excel 為 document block
- `tools/pdf_inspect.py` — `inspect_pdf_page`：單頁 text blocks + bbox
- `tools/xlsx_inspect.py` — `inspect_xlsx_sheet`：sheet 結構 + A1 column letter（供 agent 確認意見欄字母）
- `tools/kb_search.py` / `tools/web_search.py` — KB / Tavily 搜尋
- `tools/file_writer.py` — `annotate_pdf` / `annotate_xlsx`：拆分後各自一種 schema（PDF 三段定位、Excel cell comment）
- `memory/` — preferences / hooks / short_term recall
- `model/load.py` — Bedrock model ID
- `scripts/aws_session.sh` — `init` / `mfa` / `status`
- `scripts/invoke_local.py` — 本地 smoke test

## Streaming Protocol (NDJSON)

`@app.entrypoint` 是 async generator，每行 yield 一個 JSON object（NDJSON）。Client 端逐行 parse、依 `type` 分支處理。

| `type` | 欄位 | 何時出現 |
|---|---|---|
| `start` | `actor_id`, `session_id`, `files`, `instruction`, `is_continuation`, `memory_enabled` | 開頭一次 |
| `text` | `delta` (str) | LLM 文字 token 串流 |
| `tool_start` | `name`, `input` | 每個工具被呼叫時一次（已 dedup by `toolUseId`） |
| `result` | `data` (object) | 結束時，從最後 assistant message 的 ` ```json ` fence 解出的 JSON |
| `error` | `message` | 任何階段出錯（payload 缺欄位、agent 例外、parse 失敗） |

範例輸出：
```
{"type":"start","actor_id":"alex","session_id":"rv-...","files":[...],"instruction":"審查...","is_continuation":false,"memory_enabled":true}
{"type":"text","delta":"先檢查"}
{"type":"text","delta":"附件..."}
{"type":"tool_start","name":"search_knowledge_base","input":{"query":"..."}}
{"type":"tool_start","name":"annotate_pdf","input":{...}}
{"type":"result","data":{
  "status":"ok","session_id":"rv-...","answer":"審查完成...",
  "outputs":[{"input_uri":"s3://.../cab.pdf","output_uri":"s3://.../cab_revised_xxx.pdf","suggestions":[...]}],
  "cross_findings":"無跨檔問題"
}}
```

設計取捨：
- 故意**不**回傳 tool 結果（KB 命中筆數、Tavily 結果），減少協議複雜度與 token 噪音
- 最終結構化結果靠 system prompt 約束 LLM 用 ` ```json ` fence 包，再用 regex 抓最後一個 fence
- 若 LLM 沒輸出合法 fence，會回 `{"type":"error","message":"failed to parse final \`\`\`json block"}`

## Out of Scope (PoC)
未涵蓋：使用者認證 / CDK IaC / UI / Guardrails / 全新檔案產出（compose report）/ scanned PDF OCR。
