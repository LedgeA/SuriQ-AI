"""Select the configured inference provider."""
import os
from math import isfinite
from threading import TIMEOUT_MAX

from service import gemini, meta, ollama

HOSTED = {"gemini": gemini, "meta": meta}


def provider():
    selected = os.getenv("SURIQ_AI_PROVIDER", "").strip().lower()
    if selected:
        return selected if selected in HOSTED or selected == "ollama" else "off"
    return "ollama" if ollama.enabled() else "off"


def hosted():
    return provider() in HOSTED


def public_status():
    selected = provider()
    return {"provider": selected, "configured": bool(HOSTED[selected].api_key()) if selected in HOSTED else selected == "ollama"}


def backend():
    selected = provider()
    if selected in HOSTED:
        return HOSTED[selected]
    if selected == "ollama":
        return ollama
    return None


def prepare(backend, model):
    message = "Set the AI timeout to a positive number of seconds. Native extraction is still available."
    try:
        timeout = backend.timeout_seconds()
    except ValueError as exc:
        raise ollama.InferenceError(message) from exc
    if not isfinite(timeout) or not 0 < timeout < TIMEOUT_MAX:
        raise ollama.InferenceError(message)
    if backend is ollama:
        return ollama.installed_model(model), timeout
    backend.require_key()
    return None, timeout
