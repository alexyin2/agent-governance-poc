#!/usr/bin/env bash
# Create / update the Tavily API key in AWS Secrets Manager.
# Usage:
#   ./scripts/setup-secrets.sh
# Reads AWS_PROFILE / AWS_REGION from env (defaults: agentcore-poc / us-west-2).
set -euo pipefail

PROFILE="${AWS_PROFILE:-agentcore-poc}"
REGION="${AWS_REGION:-us-west-2}"
SECRET_NAME="agent-governance-poc/tavily-api-key"

read -r -s -p "Tavily API key: " TAVILY_KEY
echo
if [[ -z "${TAVILY_KEY}" ]]; then
  echo "ERROR: empty key" >&2
  exit 1
fi

SECRET_VALUE="{\"TAVILY_API_KEY\":\"${TAVILY_KEY}\"}"

if aws secretsmanager describe-secret \
     --secret-id "${SECRET_NAME}" \
     --region "${REGION}" \
     --profile "${PROFILE}" \
     --output text > /dev/null 2>&1; then
  aws secretsmanager put-secret-value \
    --secret-id "${SECRET_NAME}" \
    --secret-string "${SECRET_VALUE}" \
    --region "${REGION}" \
    --profile "${PROFILE}" \
    --output text > /dev/null
  echo "Updated secret: ${SECRET_NAME}"
else
  aws secretsmanager create-secret \
    --name "${SECRET_NAME}" \
    --secret-string "${SECRET_VALUE}" \
    --region "${REGION}" \
    --profile "${PROFILE}" \
    --output text > /dev/null
  echo "Created secret: ${SECRET_NAME}"
fi

echo "Secret ARN region: ${REGION}"
echo "Done."
