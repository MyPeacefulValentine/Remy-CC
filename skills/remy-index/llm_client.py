#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OpenAI-compatible LLM HTTP client for the remy-index pipeline.

Owns transport-level concerns extracted from run.py (A1.1): request
construction, retry with capped exponential backoff, circuit breaking on
fatal HTTP status codes, truncation detection, and error classification.
Importing this module performs no network I/O and creates no files.
"""

import json
import os
import random
import ssl
import sys
import time
import urllib.request
import urllib.error

_REMY_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remy-src"))
if _REMY_SRC not in sys.path:
    sys.path.insert(0, _REMY_SRC)
import remy_config

DEFAULT_RETRY_BACKOFF_CAP_SECONDS = 60.0


class FatalError(Exception):
    """Triggers circuit breaker and halts execution."""
    pass


class TruncatedResponseError(Exception):
    """Raised when API response is incomplete/truncated."""
    pass


class LlmClient:
    """Stateful HTTP client: circuit_open persists across calls on one instance."""

    def __init__(self, config=None):
        if config is None:
            config = remy_config.load_config(strict=True)
        self.api_key = config.get("REMY_LLM_API_KEY")
        self.model = config.get("REMY_LLM_MODEL")
        self.base_url = config.get("REMY_LLM_BASE_URL")
        self.max_tokens = config.get_int("REMY_LLM_MAX_TOKENS")
        self.retry_limit = config.get_int("REMY_LLM_RETRY_LIMIT")
        self.timeout = config.get_int("REMY_LLM_TIMEOUT")
        self.lang = "English"
        self.circuit_open = False
        self.api_calls = 0
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def call(self, prompt):
        if not self.api_key:
            return "Error: REMY_LLM_API_KEY not set."

        if self.circuit_open:
            return "Error: Circuit breaker open."

        url = self.base_url
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"You are a code analysis assistant. Respond in {self.lang}. Respond with valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}
        }

        self.api_calls += 1
        retries = 0
        while retries <= self.retry_limit:
            try:
                req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
                with urllib.request.urlopen(req, context=self.ssl_context, timeout=self.timeout) as response:
                    raw_data = response.read().decode('utf-8')
                    result = json.loads(raw_data)
                    try:
                        text_content = result['choices'][0]['message']['content'].strip()

                        if "```json" in text_content:
                            text_content = text_content.split("```json")[1].split("```")[0].strip()
                        elif "```" in text_content:
                            text_content = text_content.split("```")[1].split("```")[0].strip()

                        if not text_content.strip().endswith(('}', ']')):
                            raise TruncatedResponseError("Response truncated (incomplete JSON)")

                        try:
                            json.loads(text_content)
                            return text_content
                        except json.JSONDecodeError:
                            pass
                        return text_content
                    except (KeyError, IndexError):
                        print(f"API Debug - Response Structure: {json.dumps(result)[:500]}")
                        return "Error: Unexpected API response format."
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    self.circuit_open = True
                    raise FatalError(f"Fatal API Error {e.code}: {e.reason}")

                if e.code in (500, 502, 503, 504) and retries < self.retry_limit:
                    retries += 1
                    wait = min(DEFAULT_RETRY_BACKOFF_CAP_SECONDS, 2 ** retries) + (random.random() * 0.3)
                    time.sleep(wait)
                    continue
                return f"Error: HTTP {e.code} - {e.reason}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if retries < self.retry_limit:
                    retries += 1
                    wait = min(DEFAULT_RETRY_BACKOFF_CAP_SECONDS, 2 ** retries) + (random.random() * 0.3)
                    time.sleep(wait)
                    continue
                return f"Error: Network error ({str(e)})"
            except TruncatedResponseError:
                if retries < self.retry_limit:
                    print(f"Warning: Response truncated. Retrying ({retries+1}/{self.retry_limit})...")
                    retries += 1
                    continue
                raise
            except Exception as e:
                return f"Error: {str(e)}"
        return "Error: Maximum retries exceeded."
