"""AI 串接：用環境變數切換服務商，不需要改程式。

AI_PROVIDER=anthropic（預設）  使用 Claude API
AI_PROVIDER=openai             使用 OpenAI，或任何 OpenAI 相容的服務（設定 AI_BASE_URL）
AI_API_KEY                     服務商的 API 金鑰
AI_MODEL                       選填，模型名稱
AI_BASE_URL                    選填，OpenAI 相容服務的網址
"""
import json
import os
import re

import requests

PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").lower()
API_KEY = os.environ.get("AI_API_KEY", "")
BASE_URL = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
DEFAULT_MODEL = {"anthropic": "claude-sonnet-4-6"}.get(PROVIDER, "")
MODEL = os.environ.get("AI_MODEL") or DEFAULT_MODEL


class AIError(Exception):
    pass


def configured():
    return PROVIDER == "mock" or bool(API_KEY and MODEL)


def complete(prompt, max_tokens=4000):
    if PROVIDER == "mock":
        return _mock(prompt)
    if not configured():
        raise AIError("尚未設定 AI_API_KEY 或 AI_MODEL")
    try:
        if PROVIDER == "anthropic":
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=180, headers={
                "x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
            _check(r)
            return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        r = requests.post(f"{BASE_URL}/chat/completions", timeout=180, headers={
            "Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
        _check(r)
        return r.json()["choices"][0]["message"]["content"] or ""
    except requests.RequestException as e:
        raise AIError(f"無法連到 AI 服務：{e}") from e


def complete_json(prompt):
    text = complete(prompt + "\n\n只回傳 JSON，不要有任何其他文字或 Markdown 標記。")
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=0)
    try:
        return json.loads(text[start:])
    except ValueError as e:
        raise AIError("AI 回傳的格式無法解析，請再試一次") from e


def _check(r):
    if r.status_code == 401:
        raise AIError("AI 金鑰無效")
    if r.status_code == 429:
        raise AIError("AI 用量達到上限，請稍後再試")
    if r.status_code >= 400:
        raise AIError(f"AI 服務回應錯誤（{r.status_code}）：{r.text[:200]}")


def _mock(prompt):
    """測試用：不呼叫真實 AI。"""
    if "設計 3 到 5 個任務" in prompt:
        return json.dumps([
            {"prompt": "客人想要一杯大杯珍奶、少冰半糖，請幫他點好並結帳",
             "cond": {"type": "visible", "text": "訂單已送出"},
             "path": ["珍珠奶茶", "大杯", "少冰", "半糖", "加入購物車", "確認結帳"]},
            {"prompt": "看看購物車裡有什麼", "cond": {"type": "click", "text": "查看購物車"}, "path": ["查看購物車"]}],
            ensure_ascii=False)
    return "總結：多數受測者能完成點餐，但甜度與冰塊選項不夠明顯。（測試模式）"
