# Bedrock Knowledge Base 建立步驟（PoC）

## 前置
- AWS account 已開通 Bedrock model access
  - Console > Bedrock > **Model access** > 啟用 Claude Opus 4.6 + Titan Embeddings V2 (或 Cohere Embed)
- 一個 S3 bucket 用來放 KB 來源文件（例如 `agent-poc-kb-<your-id>`）

## 建 KB（Console 流程，約 5–10 分鐘）

1. 上傳 KB 文件到 S3：
   ```bash
   aws s3 cp ./policies/ s3://agent-poc-kb-<id>/policies/ --recursive
   ```
2. Console > **Bedrock** > **Knowledge bases** > **Create knowledge base**
3. Step 1 — KB details：填名稱、IAM role 選 *Create new service role*
4. Step 2 — Data source：
   - Source: S3
   - URI: `s3://agent-poc-kb-<id>/policies/`
5. Step 3 — Embeddings：選 **Titan Text Embeddings V2** (1024 dim)
6. Step 4 — Vector store：選 **Quick create OpenSearch Serverless**
7. Review & create → 等 vector store 建立（約 3–5 分鐘）
8. KB 建好後點進去 → **Sync** data source（每次更新文件都要重 sync）
9. 複製 **Knowledge base ID**，貼到 `.env` 的 `KB_ID`

## 驗證
```bash
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id <KB_ID> \
  --retrieval-query '{"text":"範例查詢"}' \
  --region us-east-1
```
應該回傳幾筆 `retrievalResults`。

## 成本提醒
OpenSearch Serverless 最低也會持續計費（~$0.24/hr，2 OCU）。PoC 結束別忘了刪。
