#!/usr/bin/env bash
# AWS session helper for the AgentCore PoC.
#
# Usage (MUST be sourced — `bash` would lose the exported env vars):
#   source scripts/aws_session.sh init           # one-time profile setup
#   source scripts/aws_session.sh mfa <6-digit>  # exchange MFA code for 8h session
#   source scripts/aws_session.sh status         # show what's currently active
#
# Edit the three constants below to match your account, then commit a
# .gitignored copy or set them via env vars.

: "${AWS_POC_PROFILE:=agentcore-poc}"
: "${AWS_POC_REGION:=us-west-2}"
: "${AWS_POC_MFA_SERIAL:=arn:aws:iam::538043300939:mfa/Iphone}"

_aws_session_require_sourced() {
    # ${BASH_SOURCE[0]} == $0 means executed, not sourced.
    if [ -n "${BASH_VERSION:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
        echo "ERROR: this script must be sourced, not executed."
        echo "  source scripts/aws_session.sh $*"
        exit 1
    fi
}

_aws_session_check_jq() {
    if ! command -v jq >/dev/null 2>&1; then
        echo "ERROR: jq not found. install with: brew install jq"
        return 1
    fi
}

_aws_session_init() {
    echo "Configuring AWS profile: $AWS_POC_PROFILE (region $AWS_POC_REGION)"
    echo "You will be prompted for the long-lived IAM access key + secret."
    aws configure --profile "$AWS_POC_PROFILE"
    aws configure set region "$AWS_POC_REGION" --profile "$AWS_POC_PROFILE"
    echo
    echo "Verifying..."
    if AWS_PROFILE="$AWS_POC_PROFILE" aws sts get-caller-identity; then
        echo "✓ profile '$AWS_POC_PROFILE' configured."
        echo "Next: source scripts/aws_session.sh mfa <6-digit-code>"
    else
        echo "✗ verification failed — check the access key + secret."
        return 1
    fi
}

_aws_session_mfa() {
    local code=$1
    if [ -z "$code" ]; then
        echo "usage: source scripts/aws_session.sh mfa <6-digit-code>"
        return 1
    fi
    _aws_session_check_jq || return 1

    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
    local creds
    creds=$(aws sts get-session-token \
        --profile "$AWS_POC_PROFILE" \
        --serial-number "$AWS_POC_MFA_SERIAL" \
        --token-code "$code" \
        --duration-seconds 28800 \
        --output json 2>&1)
    if [ $? -ne 0 ]; then
        echo "✗ get-session-token failed:"
        echo "$creds"
        return 1
    fi

    export AWS_ACCESS_KEY_ID=$(echo "$creds" | jq -r .Credentials.AccessKeyId)
    export AWS_SECRET_ACCESS_KEY=$(echo "$creds" | jq -r .Credentials.SecretAccessKey)
    export AWS_SESSION_TOKEN=$(echo "$creds" | jq -r .Credentials.SessionToken)
    export AWS_DEFAULT_REGION="$AWS_POC_REGION"
    unset AWS_PROFILE   # session-token wins over profile
    local exp
    exp=$(echo "$creds" | jq -r .Credentials.Expiration)
    echo "✓ MFA session active until $exp"
}

_aws_session_status() {
    if [ -z "${AWS_SESSION_TOKEN:-}" ]; then
        echo "no MFA session in this shell."
        echo "run: source scripts/aws_session.sh mfa <6-digit-code>"
        return 0
    fi
    aws sts get-caller-identity 2>&1 || echo "(session may have expired)"
}

_aws_session_main() {
    case "${1:-}" in
        init)   _aws_session_init ;;
        mfa)    _aws_session_mfa "$2" ;;
        status) _aws_session_status ;;
        *)
            echo "usage:"
            echo "  source scripts/aws_session.sh init"
            echo "  source scripts/aws_session.sh mfa <6-digit-code>"
            echo "  source scripts/aws_session.sh status"
            ;;
    esac
}

_aws_session_require_sourced "$@"
_aws_session_main "$@"
