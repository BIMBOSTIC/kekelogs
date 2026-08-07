import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
DATABASE_URL: str = os.environ["DATABASE_URL"]
DATABASE_SSL: bool = os.environ.get("DATABASE_SSL", "false").lower() == "true"
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
