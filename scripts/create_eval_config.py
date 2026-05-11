"""建立 Online Evaluation Configuration

*** 這個檔案還沒有真的測試過 ****

用法：
    source scripts/aws_session.sh mfa <code>   # 先取得 MFA session
    python scripts/create_eval_config.py

執行後會印出 onlineEvaluationConfigId，後續可用來查詢評估結果。

Evaluator 說明（針對文件審查 agent）：
  - Builtin.GoalSuccessRate       (session-scoped) 整輪審查是否達成目標
  - Builtin.Correctness           (trace-scoped)   findings 是否正確
  - Builtin.Helpfulness           (trace-scoped)   建議是否有幫助
  - Builtin.InstructionFollowing  (trace-scoped)   是否遵循使用者指令
  - Builtin.ToolSelectionAccuracy (span-scoped)    工具選用是否適當
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

AGENT_ID = "agent_governance-2s6xHgGuvg"   # 從 AgentCore console / .bedrock_agentcore.yaml 確認
REGION   = os.getenv("AWS_REGION", "us-west-2")

# 想評估的指標（可增減）
EVALUATORS = [
    "Builtin.GoalSuccessRate",       # session-scoped：整個 session 目標達成率
    "Builtin.Correctness",           # trace-scoped：回答正確性
    "Builtin.Helpfulness",           # trace-scoped：建議是否有幫助
    "Builtin.InstructionFollowing",  # trace-scoped：指令遵循程度
    "Builtin.ToolSelectionAccuracy", # span-scoped：工具選用準確率
]

SAMPLING_RATE = 100  # 100% 採樣（PoC 階段全部評估；上線後可調低，如 20）

CONFIG_NAME        = "doc-review-agent-eval"
CONFIG_DESCRIPTION = "文件審查 agent 線上評估：針對 PDF/Excel 審查品質、工具選用、指令遵循"


def main():
    try:
        from bedrock_agentcore_starter_toolkit import Evaluation
    except ImportError:
        print("❌ 找不到 bedrock_agentcore_starter_toolkit，請確認已 pip install -r requirements.txt")
        sys.exit(1)

    print(f"Region  : {REGION}")
    print(f"Agent ID: {AGENT_ID}")
    print(f"Evaluators: {EVALUATORS}")
    print(f"Sampling rate: {SAMPLING_RATE}%")
    print()

    eval_client = Evaluation(region=REGION)

    print("建立 Online Evaluation Configuration...")
    response = eval_client.create_online_config(
        agent_id=AGENT_ID,
        config_name=CONFIG_NAME,
        sampling_rate=SAMPLING_RATE,
        evaluator_list=EVALUATORS,
        config_description=CONFIG_DESCRIPTION,
        auto_create_execution_role=True,
    )

    config_id = response.get("onlineEvaluationConfigId")
    print()
    print("✅ 建立成功！")
    print(f"   onlineEvaluationConfigId: {config_id}")
    print()

    # 確認設定內容
    print("確認設定內容...")
    detail = eval_client.get_online_config(config_id=config_id)
    print(json.dumps(detail, indent=2, default=str))

    print()
    print("📌 下一步：")
    print("   1. 把 config_id 存起來，填入 eval/config.py")
    print("   2. agentcore invoke 跑幾個 session")
    print("   3. 幾分鐘後在 CloudWatch / AgentCore console 看評分")
    print(f"   Output log group: /aws/bedrock-agentcore/evaluations/results/{config_id}")


if __name__ == "__main__":
    main()
