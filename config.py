"""Configuration loader - reads API keys and settings from .env file. (auto-read test)"""
import os
import json
from dotenv import load_dotenv

load_dotenv()


def _env(key, default=""):
    return os.getenv(key, default)


def _env_json(key, default=None):
    """从环境变量读取JSON格式的值（如数组/对象），解析失败时返回default。用于SUBAGENT_PROVIDERS等复杂配置。"""
    val = os.getenv(key, "")
    if not val:
        return default if default is not None else []
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []


CONFIG = {
    "API_KEY": _env("API_KEY"),
    "MODEL_NAME": _env("MODEL_NAME", "gemini-3.1-pro-preview-cli"),
    "SYSTEM_PROMPT": "",
    "THINKING_CONFIG": {"thinking_level": "high", "include_thoughts": True},
    "API_URL": _env("API_URL", "https://api.example.com/v1/chat/completions"),
    "VIBE_API_KEY": _env("VIBE_API_KEY"),
    "VIBE_API_URL": _env("VIBE_API_URL", "https://vibe-api.com/v1/chat/completions"),
    "LAB_API_KEY": _env("LAB_API_KEY"),
    "LAB_API_URL": _env("LAB_API_URL", "https://www.dmxapi.cn/v1/chat/completions"),
    "AIPAI_API_KEY": _env("AIPAI_API_KEY"),
    "AIPAI_API_URL": _env("AIPAI_API_URL", "https://api.aipaibox.com/v1/chat/completions"),
    "EKAN8_API_KEY": _env("EKAN8_API_KEY"),
    "EKAN8_API_URL": _env("EKAN8_API_URL", "https://api.ekan8.com/v1/chat/completions"),
    "CG_API_KEY": _env("CG_API_KEY"),
    "CG_API_URL": _env("CG_API_URL", "https://www.findcg.com/v1/chat/completions"),
    "CG2_API_KEY": _env("CG2_API_KEY"),
    "CG3_API_KEY": _env("CG3_API_KEY"),
    "HJM_API_KEY": _env("HJM_API_KEY"),
    "HJM_API_URL": _env("HJM_API_URL", "https://api.gemai.cc/v1/chat/completions"),
    "NEXUS_API_KEY": _env("NEXUS_API_KEY"),
    "NEXUS_API_URL": _env("NEXUS_API_URL", "https://cc.hgy667.shop/v1/responses"),
    "SISUO_API_KEY": _env("SISUO_API_KEY"),
    "SISUO_API_URL": _env("SISUO_API_URL", "https://newapi.sisuo.de/v1/chat/completions"),
    "SISUO2_API_KEY": _env("SISUO2_API_KEY"),
    "LOCAL_API_URL": _env("LOCAL_API_URL", "http://127.0.0.1:38211/v1/chat"),
    "GOAI_API_KEY": _env("GOAI_API_KEY"),
    "GOAI_API_URL": _env("GOAI_API_URL", "https://www.go-ai.cc/v1/chat/completions"),
    "SUBAGENT_PROVIDERS": _env_json("SUBAGENT_PROVIDERS", [
        {"name": "sisuo", "api_key": _env("SISUO_API_KEY"), "url": "https://newapi.sisuo.de/v1/messages"},
    ])
}
