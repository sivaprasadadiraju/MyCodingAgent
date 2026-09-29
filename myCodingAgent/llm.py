"""
myCodingAgent/llm.py - Self-contained LLM client for myCodingAgent.

This is a bundled copy of the OxAlpha chat API client so the
myCodingAgent package works FULLY INDEPENDENTLY (no imports from the
parent workspace). Any other project can copy this whole folder and it
will still function.

Requires: python -m pip install requests
"""

import json
import os
import re
import time

import requests

MODEL_NAME = "z-ai/glm-5.3-flash"
BASE_URL = "https://oxalpha.com"
API_URL = BASE_URL + "/api/chat"

# NOTE: the server enforces a daily message checkpoint (~20 messages) and a
# Cloudflare Turnstile bot-check. Calling too fast/frequently triggers 428
# turnstile_required, which CANNOT be solved by retries or csrf rotation.
# So we keep the per-call delay generous and never retry turnstile errors.
MIN_DELAY_BETWEEN_CALLS = 4.0
MAX_ATTEMPTS = 4


class VerificationRequiredError(RuntimeError):
    """Raised when the service requires a human to complete verification."""

NOISE_PATTERNS = [
    "",
    "",
    "",
    "event ping",
]

session = requests.Session()
session.headers.update({
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "accept-language": "en-US,en;q=0.9",
})

CURRENT_CREDS = None
_last_call_time = 0.0


# ============================================================
# Token / auth handling
# ============================================================
def refresh_tokens():
    """Fetch /chat page, extract csrf-token meta and auth cookies."""
    global CURRENT_CREDS
    time.sleep(1)
    session.cookies.clear()
    resp = session.get(BASE_URL + "/chat", timeout=30)
    resp.raise_for_status()

    m = (re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"',
                   resp.text, re.IGNORECASE)
         or re.search(r'<meta\s+content="([^"]+)"\s+name="csrf-token"',
                      resp.text, re.IGNORECASE))
    if not m:
        raise RuntimeError("Could not find csrf-token in page HTML.")

    xsrf = session.cookies.get("XSRF-TOKEN")
    sess = session.cookies.get("ox_alpha_session")
    if not xsrf or not sess:
        raise RuntimeError("Server did not set required cookies.")

    CURRENT_CREDS = {"csrf": m.group(1), "xsrf_cookie": xsrf,
                     "session": sess}
    return CURRENT_CREDS


def ensure_credentials():
    global CURRENT_CREDS
    if CURRENT_CREDS is None:
        refresh_tokens()
    return CURRENT_CREDS


def sync_cookies_from_response():
    """Pick up rotated XSRF-TOKEN / session cookies after each response."""
    global CURRENT_CREDS
    if CURRENT_CREDS is None:
        return False
    changed = False
    new_xsrf = session.cookies.get("XSRF-TOKEN")
    new_sess = session.cookies.get("ox_alpha_session")
    if new_xsrf and new_xsrf != CURRENT_CREDS["csrf"]:
        CURRENT_CREDS["csrf"] = new_xsrf
        changed = True
    if new_xsrf:
        CURRENT_CREDS["xsrf_cookie"] = new_xsrf
    if new_sess:
        CURRENT_CREDS["session"] = new_sess
    return changed


def is_turnstile(response):
    """Detect the Cloudflare Turnstile checkpoint (HTTP 428 / code
    turnstile_required). Retrying can never fix this, so callers must stop."""
    if response.status_code != 428:
        return False
    try:
        data = response.json()
    except Exception:
        return False
    return (data.get("code") == "turnstile_required"
            or "turnstile" in str(data.get("error", "")).lower())


def turnstile_message(response):
    try:
        data = response.json()
    except Exception:
        data = {}
    return (
        "The current OxAlpha model service paused this request for a human-"
        "verification or usage checkpoint "
        f"({data.get('messages_today', '?')}/{data.get('checkpoint', '?')}). "
        "myCodingAgent cannot complete or bypass this check.\n\n"
        "To continue without relying on OxAlpha, configure an OpenAI-compatible "
        "model endpoint with MYCODINGAGENT_LLM_BASE_URL and "
        "MYCODINGAGENT_LLM_MODEL. Set MYCODINGAGENT_LLM_API_KEY in your "
        "environment if that provider requires a key (local Ollama can run "
        "without one), then restart VS Code and retry. You can also complete "
        "the check at https://oxalpha.com/chat and retry later."
    )


# ============================================================
# Stream cleaning
# ============================================================
def is_noise(text):
    if not text:
        return True
    for pattern in NOISE_PATTERNS:
        if pattern and pattern in text:
            return True
    if re.fullmatch(r"[\s]+", text):
        return True
    return False


def clean_response(text):
    for pattern in NOISE_PATTERNS:
        text = text.replace(pattern, "")
    text = re.sub(r"(?:^\s*:\s*$)", "", text, flags=re.MULTILINE)
    return text.strip()


# ============================================================
# Message helpers
# ============================================================
def enforce_alternating(history):
    """The OxAlpha API requires strict user/assistant alternation."""
    fixed = []
    for msg in history:
        if msg["role"] == "system":
            msg = {"role": "user",
                   "content": "[Instructions]\n" + msg.get("content", "")}
        if fixed and fixed[-1]["role"] == msg["role"]:
            fixed[-1] = {"role": fixed[-1]["role"],
                         "content": fixed[-1]["content"] + "\n\n" + msg["content"]}
        else:
            fixed.append(dict(msg))
    while fixed and fixed[0]["role"] != "user":
        fixed.pop(0)
    return fixed


def configured_openai_endpoint():
    """Return explicitly configured OpenAI-compatible endpoint, if any."""
    base_url = os.environ.get("MYCODINGAGENT_LLM_BASE_URL", "").strip()
    model = os.environ.get("MYCODINGAGENT_LLM_MODEL", "").strip()
    api_key = os.environ.get("MYCODINGAGENT_LLM_API_KEY", "").strip()
    if not base_url and not model and not api_key:
        return None
    if not base_url or not model:
        raise RuntimeError(
            "Alternate LLM configuration is incomplete. Set both "
            "MYCODINGAGENT_LLM_BASE_URL and MYCODINGAGENT_LLM_MODEL. "
            "Set MYCODINGAGENT_LLM_API_KEY only if your provider requires it."
        )
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    return endpoint, model, api_key


def ask_openai_compatible(messages, endpoint, model, api_key, timeout):
    """Call an explicitly configured OpenAI-compatible chat-completions API."""
    normalized = enforce_alternating(messages)
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = "Bearer " + api_key
    response = session.post(
        endpoint,
        json={"model": model, "messages": normalized, "stream": False},
        headers=headers,
        timeout=timeout,
    )
    if response.status_code == 428:
        response.close()
        raise RuntimeError(
            "The configured LLM provider returned HTTP 428 and requires "
            "action on its own service. No verification was attempted."
        )
    response.raise_for_status()
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Configured LLM returned invalid JSON.") from exc
    finally:
        response.close()
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Configured LLM response is missing choices[0].message.content."
        ) from exc
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Configured LLM returned an empty response.")
    return text.strip()


# ============================================================
# Main entry point
# ============================================================
def ask_llm(messages, verbose=False, timeout=60):
    """Send a chat completion request to the OxAlpha API and return the text.

    messages: list of {"role": "system"/"user"/"assistant", "content": str}
    (system roles are converted automatically; alternation enforced)
    verbose: when True, stream tokens to stdout.
    """
    global _last_call_time
    alternate = configured_openai_endpoint()
    if alternate:
        endpoint, model, api_key = alternate
        return ask_openai_compatible(messages, endpoint, model, api_key, timeout)

    messages = enforce_alternating(messages)

    total_len = sum(len(m["content"]) for m in messages)
    if total_len > 60_000:
        messages = enforce_alternating(messages[:1] + messages[-4:])

    payload = {"model": MODEL_NAME, "messages": messages}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        creds = ensure_credentials()

        elapsed = time.time() - _last_call_time
        if elapsed < MIN_DELAY_BETWEEN_CALLS:
            time.sleep(MIN_DELAY_BETWEEN_CALLS - elapsed)

        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BASE_URL,
            "referer": BASE_URL + "/chat",
            "x-csrf-token": creds["csrf"],
            "x-requested-with": "XMLHttpRequest",
        }
        r = session.post(API_URL, json=payload, headers=headers,
                         stream=True, timeout=timeout)

        _last_call_time = time.time()
        rotated = sync_cookies_from_response()

        # --- Turnstile checkpoint: fail FAST, never retry ---
        if is_turnstile(r):
            message = turnstile_message(r)
            r.close()
            raise VerificationRequiredError(message)

        # A different 428 response is not necessarily an auth failure. Do not
        # rotate credentials/retry blindly; report the service response.
        if r.status_code == 428:
            try:
                detail = r.text[:1000]
            finally:
                r.close()
            raise RuntimeError("Server returned HTTP 428 (not Turnstile): " + detail)

        if r.status_code in (419, 428, 403, 401):
            r.close()
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    "Auth failed after %d attempts (last status %d)."
                    % (MAX_ATTEMPTS, r.status_code))
            wait = min(2 * (2 ** (attempt - 1)), 30)
            if not rotated:
                try:
                    refresh_tokens()
                except Exception:
                    pass
            time.sleep(wait)
            continue

        if r.status_code in (400, 422):
            r.close()
            if attempt < MAX_ATTEMPTS:
                messages = enforce_alternating(messages[:1] + messages[-2:])
                payload["messages"] = messages
                continue
            raise RuntimeError("Server rejected request (%d)." % r.status_code)

        if r.status_code == 429:
            r.close()
            time.sleep(10 * attempt)
            continue

        r.raise_for_status()

        raw_lines = []
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if is_noise(data):
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    content = (chunk.get("choices", [{}])[0]
                               .get("delta", {}).get("content", "")
                               or chunk.get("content", ""))
                except json.JSONDecodeError:
                    content = data
                if is_noise(content):
                    continue
                raw_lines.append(content)
                if verbose:
                    print(content, end="", flush=True)
            else:
                if is_noise(line):
                    continue
                raw_lines.append(line)
                if verbose:
                    print(line, end="", flush=True)

        if verbose:
            print()
        sync_cookies_from_response()
        return clean_response("".join(raw_lines))

    raise RuntimeError("LLM request failed after retries.")


if __name__ == "__main__":
    reply = ask_llm([{"role": "user",
                      "content": "Reply with exactly OK"}], verbose=True)
    print("reply:", reply)
