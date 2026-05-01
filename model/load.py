import os
from dotenv import load_dotenv

load_dotenv()


def load_model():
    """Return the Bedrock model ID Strands should use.

    Strands accepts the Bedrock model/inference-profile ID as a string.
    Looked up from BEDROCK_MODEL_ID in .env — no fallback, because the right
    inference-profile ID depends on region and must be confirmed in the Bedrock
    console (Console > Bedrock > Inference profiles).
    """
    model_id = os.getenv("BEDROCK_MODEL_ID")
    if not model_id:
        raise RuntimeError(
            "BEDROCK_MODEL_ID is not set. Copy .env.example to .env and set the "
            "Bedrock inference-profile ID for Claude Opus 4.6 in your region."
        )
    return model_id
