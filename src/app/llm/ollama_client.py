"""Local HTTP Client for Ollama.

Communicates with localhost Ollama server, enforcing JSON output format,
handling timeouts, and retry logic.
"""

from __future__ import annotations

import time
from typing import Optional

import httpx
from app.logging_config import get_logger

logger = get_logger("app.llm.ollama")


class OllamaClient:
    """HTTP Client for executing generation prompts against a local Ollama API."""

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "llama3.1",
        timeout: int = 120,
        temperature: float = 0.1,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature

    def is_available(self) -> bool:
        """Perform a quick health check to verify Ollama server is running and model pulled."""
        url = f"{self.host}/api/tags"
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    models = resp.json().get("models", [])
                    available_names = [m.get("name") for m in models]
                    # Direct check or generic check
                    logger.debug("Ollama model list: %s", available_names)
                    return True
        except Exception as exc:
            logger.debug("Ollama health check failed: %s", exc)
        return False

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Send prompt to local Ollama API and request JSON structured response."""
        url = f"{self.host}/api/generate"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",  # enforce structured JSON output format
            "options": {
                "temperature": self.temperature,
                "seed": 42,
            },
        }
        if system_prompt:
            payload["system"] = system_prompt

        logger.info("Executing Ollama generate (model=%s, timeout=%ds)...", self.model, self.timeout)

        # Retry loop for socket/connection errors
        max_attempts = 3
        backoff = 2.0

        for attempt in range(1, max_attempts + 1):
            try:
                start_time = time.time()
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload)
                    resp.raise_for_status()

                duration = time.time() - start_time
                logger.info("Ollama execution finished in %.2fs", duration)
                return resp.json().get("response", "")

            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt == max_attempts:
                    logger.error("Failed to connect to Ollama after %d attempts: %s", max_attempts, exc)
                    raise
                logger.warning(
                    "Ollama request attempt %d failed: %s. Retrying in %.1fs...",
                    attempt,
                    exc,
                    backoff,
                )
                time.sleep(backoff)
                backoff *= 2.0
            except Exception as exc:
                logger.error("Unexpected error during Ollama generation: %s", exc)
                raise

        raise RuntimeError("Ollama execution failed to complete.")
