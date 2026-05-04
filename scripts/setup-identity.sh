#!/usr/bin/env bash
# Create / update an AgentCore Identity API Key Credential Provider for Tavily.
# Usage:
#   ./scripts/setup-identity.sh
# Reads AWS_PROFILE / AWS_REGION from env (defaults: agentcore-poc / us-west-2).
#
# NOTE: this script may fail with AccessDeniedException if your org SCP blocks
# bedrock-agentcore:CreateApiKeyCredentialProvider (as ours does). When that
# happens, create the provider via the AWS Console UI instead — see README §6a.
# Provider name must be "tavily-provider" to match tools/web_search.py.
set -euo pipefail

PROFILE="${AWS_PROFILE:-agentcore-poc}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
PROVIDER_NAME="tavily-provider"

read -r -s -p "Tavily API key: " TAVILY_KEY
echo
if [[ -z "${TAVILY_KEY}" ]]; then
  echo "ERROR: empty key" >&2
  exit 1
fi

export AWS_PROFILE="${PROFILE}"
export AWS_REGION="${REGION}"
export PROVIDER_NAME TAVILY_KEY

python3 - <<'PY'
import os, time, sys
import boto3
from botocore.exceptions import ClientError

session = boto3.Session(
    profile_name=os.environ["AWS_PROFILE"],
    region_name=os.environ["AWS_REGION"],
)
client = session.client("bedrock-agentcore-control")
name = os.environ["PROVIDER_NAME"]
key = os.environ["TAVILY_KEY"]

def create():
    client.create_api_key_credential_provider(name=name, apiKey=key)

try:
    create()
    print(f"Created credential provider: {name}")
except ClientError as e:
    code = e.response.get("Error", {}).get("Code", "")
    if code in ("ConflictException", "ResourceAlreadyExistsException"):
        print(f"{name} already exists — recreating")
        client.delete_api_key_credential_provider(name=name)
        time.sleep(15)  # propagation
        create()
        print(f"Recreated credential provider: {name}")
    else:
        raise
PY

echo
echo "Done. Reference in code via @requires_api_key(provider_name=\"${PROVIDER_NAME}\")."
