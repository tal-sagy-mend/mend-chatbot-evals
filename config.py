import os
from dotenv import load_dotenv

load_dotenv()

# Langfuse
LANGFUSE_PUBLIC_KEY = os.environ["LANGFUSE_PUBLIC_KEY"]
LANGFUSE_SECRET_KEY = os.environ["LANGFUSE_SECRET_KEY"]
LANGFUSE_HOST = os.environ["LANGFUSE_HOST"]
LANGFUSE_DATASET_NAME = "chatbot-golden-set-v1"

# Mend API
MEND_BASE_URL = os.environ.get("MEND_BASE_URL", "https://api-dev.whitesourcesoftware.com")
MEND_EMAIL = os.environ["MEND_EMAIL"]
MEND_USER_KEY = os.environ["MEND_USER_KEY"]
MEND_ORG_UUID = os.environ["MEND_ORG_UUID"]
MEND_PROXY_URL = os.environ.get("MEND_PROXY_URL", "http://127.0.0.1:9988")

# user_id as set by the bot SDK: email:org_uuid
LANGFUSE_USER_ID = f"{MEND_EMAIL}_{MEND_ORG_UUID}"

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
JUDGE_MODEL = "claude-sonnet-4-6"

# Runner config
PRODUCTION_LOOKBACK_HOURS = 24
