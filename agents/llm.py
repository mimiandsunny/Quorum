"""LLM client abstraction. Local (Ollama/llama.cpp) and cloud (OpenAI)."""

import contextvars
import json
import logging
import threading
import time
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


# ─── Telemetry context (wave 2 prep) ─────────────────────
# Callers wrap their work in `with llm_call_context(ticker=..., stage=...)`
# so each LLM call records ticker/stage in the llm_calls table. Unset →
# telemetry rows have NULL for those columns (still useful for global stats).

_ticker_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_ticker", default=None,
)
_stage_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_stage", default=None,
)
_run_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_run_id", default=None,
)


class llm_call_context:
    """Tag every LLM call inside the with-block with telemetry metadata."""

    def __init__(self, ticker: str | None = None, stage: str | None = None,
                 run_id: str | None = None):
        self._ticker = ticker
        self._stage = stage
        self._run_id = run_id
        self._reset: list[tuple[contextvars.ContextVar, object]] = []

    def __enter__(self) -> "llm_call_context":
        if self._ticker is not None:
            self._reset.append((_ticker_ctx, _ticker_ctx.set(self._ticker)))
        if self._stage is not None:
            self._reset.append((_stage_ctx, _stage_ctx.set(self._stage)))
        if self._run_id is not None:
            self._reset.append((_run_id_ctx, _run_id_ctx.set(self._run_id)))
        return self

    def __exit__(self, *exc) -> bool:
        for var, token in reversed(self._reset):
            var.reset(token)
        return False


def _emit_call_telemetry(
    *,
    provider: str,
    model: str,
    attempt: int,
    prompt_len: int,
    output_len: int,
    elapsed_seconds: float,
    response_meta: dict,
    parse_ok: bool,
    error: str | None = None,
    fallback_used: bool = False,
    ticker: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> None:
    """Write one llm_calls row. Fail-open — never raises.

    Explicit ticker/stage/run_id kwargs override the contextvars (so callers
    that pass them explicitly always win over any ambient context).
    """
    try:
        from data.models import LLMCallTelemetry
        from data.storage import record_llm_call
        record_llm_call(LLMCallTelemetry(
            run_id=run_id if run_id is not None else _run_id_ctx.get(),
            ticker=ticker if ticker is not None else _ticker_ctx.get(),
            stage=stage if stage is not None else _stage_ctx.get(),
            provider=provider,
            model=model,
            attempt=attempt,
            prompt_len=prompt_len,
            output_len=output_len,
            elapsed_seconds=elapsed_seconds,
            done_reason=_extract_done_reason(response_meta),
            eval_count=_extract_eval_count(response_meta),
            parse_ok=parse_ok,
            fallback_used=fallback_used,
            error=error,
        ))
    except Exception as exc:
        logger.debug(f"telemetry emit skipped: {exc}")


def _extract_done_reason(meta: dict) -> str | None:
    if not isinstance(meta, dict):
        return None
    val = meta.get("done_reason") or meta.get("finish_reason")
    return str(val) if val is not None else None


def _extract_eval_count(meta: dict) -> int | None:
    if not isinstance(meta, dict):
        return None
    if "eval_count" in meta:
        try:
            return int(meta["eval_count"])
        except (TypeError, ValueError):
            return None
    usage = meta.get("usage")
    if isinstance(usage, dict) and "completion_tokens" in usage:
        try:
            return int(usage["completion_tokens"])
        except (TypeError, ValueError):
            return None
    return None

# ─── Clients (lazy-initialized) ──────────────────────────

_openai_client = None
_ollama_client = None
_llama_cpp_client = None
_llama_cpp_lock = threading.RLock()


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.cloud_llm_timeout_seconds,
        )
    return _openai_client


def _get_ollama_client():
    global _ollama_client
    if _ollama_client is None:
        import ollama
        _ollama_client = ollama.Client(
            host=settings.ollama_base_url,
            timeout=settings.local_llm_timeout_seconds,
        )
    return _ollama_client


def _get_llama_cpp_client():
    global _llama_cpp_client
    with _llama_cpp_lock:
        if _llama_cpp_client is not None:
            return _llama_cpp_client

        model_path = settings.llama_cpp_model_path.strip()
        if not model_path:
            raise RuntimeError(
                "LOCAL_PROVIDER=llama_cpp requires LLAMA_CPP_MODEL_PATH to point to a GGUF model."
            )

        expanded_path = Path(model_path).expanduser()
        if not expanded_path.exists():
            raise RuntimeError(f"LLAMA_CPP_MODEL_PATH does not exist: {expanded_path}")

        try:
            from llama_cpp import Llama
            import llama_cpp
        except ImportError as e:
            raise RuntimeError(
                "LOCAL_PROVIDER=llama_cpp requires llama-cpp-python. "
                "Install it with the optional llama.cpp requirements file."
            ) from e

        if settings.llama_cpp_n_gpu_layers != 0 and not _llama_cpp_has_cuda_backend(llama_cpp):
            raise RuntimeError(
                "llama-cpp-python appears to be installed without CUDA support, "
                "but LLAMA_CPP_N_GPU_LAYERS requests GPU offload. Reinstall it with "
                'CMAKE_ARGS="-DGGML_CUDA=on" FORCE_CMAKE=1 and --no-binary llama-cpp-python, '
                "or set LLAMA_CPP_N_GPU_LAYERS=0 for a CPU-only test."
            )

        kwargs = {
            "model_path": str(expanded_path),
            "n_ctx": settings.llama_cpp_n_ctx,
            "n_gpu_layers": settings.llama_cpp_n_gpu_layers,
            "verbose": settings.llama_cpp_verbose,
        }
        if settings.llama_cpp_n_threads > 0:
            kwargs["n_threads"] = settings.llama_cpp_n_threads
        if settings.llama_cpp_chat_format.strip():
            kwargs["chat_format"] = settings.llama_cpp_chat_format.strip()

        _llama_cpp_client = Llama(**kwargs)
        return _llama_cpp_client


def _llama_cpp_has_cuda_backend(llama_cpp_module: object) -> bool:
    """Return True when the installed llama.cpp bindings expose a CUDA backend."""
    try:
        raw_info = llama_cpp_module.llama_cpp.llama_print_system_info()
        system_info = raw_info.decode() if isinstance(raw_info, bytes) else str(raw_info)
    except Exception:
        system_info = ""

    cuda_markers = ("CUDA = 1", "GGML_CUDA", "CUBLAS", "CUDA")
    if any(marker in system_info for marker in cuda_markers):
        return True

    try:
        package_dir = Path(llama_cpp_module.__file__).resolve().parent
    except Exception:
        return False

    return any(
        path.name.startswith("libggml-cuda") or path.name.startswith("libggml-cublas")
        for lib_dir in (package_dir / "lib", package_dir.parent / "lib")
        if lib_dir.exists()
        for path in lib_dir.iterdir()
    )


# ─── Core call functions ─────────────────────────────────

def call_local(
    prompt: str,
    output_model: type[T],
    system: str = "",
    max_retries: int = 2,
    *,
    ticker: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> T:
    """Call the configured local model provider and parse into a Pydantic model."""
    if settings.local_provider == "ollama":
        return _call_ollama(prompt, output_model, system=system, max_retries=max_retries,
                            ticker=ticker, stage=stage, run_id=run_id)
    if settings.local_provider == "llama_cpp":
        return _call_llama_cpp(prompt, output_model, system=system, max_retries=max_retries,
                               ticker=ticker, stage=stage, run_id=run_id)
    raise RuntimeError(f"Unsupported local provider: {settings.local_provider}")


def _build_local_messages(
    prompt: str,
    output_model: type[T],
    system: str,
    required_keys: str,
    attempt: int,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({
        "role": "user",
        "content": (
            f"{prompt}\n\n"
            f"Return exactly one JSON object for {output_model.__name__}. "
            f"Required top-level keys: {required_keys}. "
            "Keep every string value under 240 characters. "
            "All numeric score fields must be within their documented range "
            "(e.g. sentiment scores must be between -1.0 and 1.0). "
            "Do not return the JSON schema, markdown, or commentary."
        ),
    })
    if attempt > 0:
        messages.append({
            "role": "user",
            "content": (
                f"The previous response was invalid JSON for {output_model.__name__}. "
                "Return a NEW complete JSON object. "
                "Do not continue the prior partial response. "
                f"Required top-level keys: {required_keys}. "
                "Keep every string value under 240 characters. "
                "Do not return markdown, commentary, or the JSON schema."
            ),
        })
    return messages


def _local_sampling_options(attempt: int) -> dict[str, object]:
    options: dict[str, object] = {}
    if settings.local_llm_num_predict > 0:
        options["num_predict"] = settings.local_llm_num_predict
    if attempt == 0:
        options["temperature"] = 0.0
    else:
        # Attack the "growthries-growthries-growthries..." failure mode:
        # repeat_penalty (>1.0) makes the sampler less likely to re-pick
        # tokens it just emitted. top_p narrows the candidate set so the
        # model can't meander into a low-probability loop. Temperature
        # adds variance so we don't re-run a deterministic failure.
        options["temperature"] = 0.4
        options["top_p"] = 0.9
        options["repeat_penalty"] = 1.3
    return options


def _call_ollama(
    prompt: str,
    output_model: type[T],
    system: str = "",
    max_retries: int = 2,
    *,
    ticker: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> T:
    """Call local Ollama model and parse into a Pydantic model."""
    client = _get_ollama_client()
    schema = output_model.model_json_schema()
    required_keys = ", ".join(schema.get("required", []))

    for attempt in range(max_retries + 1):
        content = ""
        response_meta: dict = {}
        elapsed = 0.0
        parsed_ok = False
        emit_error: str | None = None
        messages = _build_local_messages(prompt, output_model, system, required_keys, attempt)
        try:
            t0 = time.monotonic()
            logger.debug(
                f"Local LLM call (attempt {attempt + 1}): provider=ollama, "
                f"model={settings.local_model}, prompt_len={len(prompt)}"
            )
            options = _local_sampling_options(attempt)
            response = client.chat(
                model=settings.local_model,
                messages=messages,
                format=schema,
                options=options,
                think=settings.local_llm_think,
            )
            elapsed = time.monotonic() - t0
            response_meta = _ollama_response_meta(response)
            content = response["message"]["content"]
            logger.debug(
                f"Local LLM responded in {elapsed:.1f}s, output_len={len(content)}, "
                f"response_meta={response_meta}"
            )
            if not content.strip():
                raise RuntimeError(
                    f"Ollama returned empty content for model={settings.local_model}, "
                    f"prompt_len={len(prompt)}, options={options}, response_meta={response_meta}"
                )
            result = output_model.model_validate_json(content)
            parsed_ok = True
            return result
        except Exception as e:
            emit_error = repr(e)
            if attempt < max_retries:
                logger.warning(
                    f"Local LLM parse attempt {attempt + 1} failed: {e}; "
                    f"response_meta={response_meta}; output_len={len(content)}. Retrying..."
                )
            else:
                raise RuntimeError(
                    f"Local LLM (ollama) returned invalid {output_model.__name__}: {e}; "
                    f"response_meta={response_meta}; output_len={len(content)}; "
                    f"output_tail={content[-300:]!r}"
                ) from e
        finally:
            _emit_call_telemetry(
                provider="ollama",
                model=settings.local_model,
                attempt=attempt,
                prompt_len=len(prompt),
                output_len=len(content),
                elapsed_seconds=elapsed,
                response_meta=response_meta,
                parse_ok=parsed_ok,
                error=emit_error,
                ticker=ticker,
                stage=stage,
                run_id=run_id,
            )


def _call_llama_cpp(
    prompt: str,
    output_model: type[T],
    system: str = "",
    max_retries: int = 2,
    *,
    ticker: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> T:
    """Call local llama.cpp model and parse into a Pydantic model."""
    client = _get_llama_cpp_client()
    schema = output_model.model_json_schema()
    required_keys = ", ".join(schema.get("required", []))

    for attempt in range(max_retries + 1):
        content = ""
        response_meta: dict = {}
        elapsed = 0.0
        parsed_ok = False
        emit_error: str | None = None
        messages = _build_local_messages(prompt, output_model, system, required_keys, attempt)
        try:
            t0 = time.monotonic()
            logger.debug(
                f"Local LLM call (attempt {attempt + 1}): provider=llama_cpp, "
                f"model_path={settings.llama_cpp_model_path}, prompt_len={len(prompt)}"
            )
            options = _local_sampling_options(attempt)
            completion_kwargs = {
                "messages": messages,
                "temperature": options["temperature"],
                "response_format": {"type": "json_object", "schema": schema},
            }
            if "top_p" in options:
                completion_kwargs["top_p"] = options["top_p"]
            if "repeat_penalty" in options:
                completion_kwargs["repeat_penalty"] = options["repeat_penalty"]
            if settings.local_llm_num_predict > 0:
                completion_kwargs["max_tokens"] = settings.local_llm_num_predict

            response = _create_llama_cpp_chat_completion(client, completion_kwargs)
            elapsed = time.monotonic() - t0
            response_meta = _llama_cpp_response_meta(response)
            content = _llama_cpp_response_content(response)
            logger.debug(
                f"Local LLM responded in {elapsed:.1f}s, output_len={len(content)}, "
                f"response_meta={response_meta}"
            )
            if not content.strip():
                raise RuntimeError(
                    f"llama.cpp returned empty content for model_path={settings.llama_cpp_model_path}, "
                    f"prompt_len={len(prompt)}, response_meta={response_meta}"
                )
            result = output_model.model_validate_json(content)
            parsed_ok = True
            return result
        except Exception as e:
            emit_error = repr(e)
            if attempt < max_retries:
                logger.warning(
                    f"Local LLM parse attempt {attempt + 1} failed: {e}; "
                    f"response_meta={response_meta}; output_len={len(content)}. Retrying..."
                )
            else:
                raise RuntimeError(
                    f"Local LLM (llama_cpp) returned invalid {output_model.__name__}: {e}; "
                    f"response_meta={response_meta}; output_len={len(content)}; "
                    f"output_tail={content[-300:]!r}"
                ) from e
        finally:
            _emit_call_telemetry(
                provider="llama_cpp",
                model=settings.llama_cpp_model_path or "(unset)",
                attempt=attempt,
                prompt_len=len(prompt),
                output_len=len(content),
                elapsed_seconds=elapsed,
                response_meta=response_meta,
                parse_ok=parsed_ok,
                error=emit_error,
                ticker=ticker,
                stage=stage,
                run_id=run_id,
            )


def _create_llama_cpp_chat_completion(client: object, completion_kwargs: dict[str, object]) -> object:
    # llama-cpp-python keeps mutable model/KV/cache state inside the client.
    # FastAPI background tasks and ticker analysis can overlap, so serialize
    # access to avoid reentrant CUDA allocator crashes in ggml.
    with _llama_cpp_lock:
        try:
            return client.create_chat_completion(**completion_kwargs)
        except TypeError as e:
            if "response_format" not in str(e):
                raise
            fallback_kwargs = dict(completion_kwargs)
            fallback_kwargs.pop("response_format", None)
            logger.warning(
                "Installed llama-cpp-python does not accept response_format; "
                "falling back to prompt-only JSON guidance."
            )
            return client.create_chat_completion(**fallback_kwargs)


def _llama_cpp_response_content(response: object) -> str:
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _llama_cpp_response_meta(response: object) -> dict[str, object]:
    """Extract stable llama.cpp timing/termination fields for logging."""
    if not isinstance(response, dict):
        return {}
    meta: dict[str, object] = {}
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        if "finish_reason" in choices[0]:
            meta["finish_reason"] = choices[0]["finish_reason"]
    if "usage" in response:
        meta["usage"] = response["usage"]
    return meta


def _ollama_response_meta(response: object) -> dict[str, object]:
    """Extract stable Ollama timing/termination fields for logging."""
    if not isinstance(response, dict):
        return {}
    return {
        k: response.get(k)
        for k in ("done_reason", "eval_count", "prompt_eval_count", "total_duration")
        if k in response
    }


def call_analyst(
    prompt: str,
    output_model: type[T],
    system: str = "",
    max_retries: int | None = None,
    *,
    ticker: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> T:
    """Call the configured LLM backend for analyst-style structured output."""
    retries = settings.analyst_max_retries if max_retries is None else max_retries
    if settings.analyst_mode == "cloud":
        return call_cloud(prompt, output_model, system=system, max_retries=retries,
                          ticker=ticker, stage=stage, run_id=run_id)
    if settings.analyst_mode == "local":
        return call_local(prompt, output_model, system=system, max_retries=retries,
                          ticker=ticker, stage=stage, run_id=run_id)
    raise RuntimeError("call_analyst called while ANALYST_MODE=deterministic")


def call_cloud(
    prompt: str,
    output_model: type[T],
    system: str = "",
    max_retries: int = 2,
    *,
    ticker: str | None = None,
    stage: str | None = None,
    run_id: str | None = None,
) -> T:
    """Call cloud OpenAI model and parse into a Pydantic model."""
    client = _get_openai_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    for attempt in range(max_retries + 1):
        content = ""
        elapsed = 0.0
        response_meta: dict = {}
        parsed_ok = False
        emit_error: str | None = None
        try:
            t0 = time.monotonic()
            logger.debug(f"Cloud LLM call (attempt {attempt + 1}): model={settings.cloud_model}, prompt_len={len(prompt)}")
            response = client.chat.completions.create(
                model=settings.cloud_model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            elapsed = time.monotonic() - t0
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            finish_reason = response.choices[0].finish_reason if response.choices else None
            response_meta = {"finish_reason": finish_reason}
            if usage is not None:
                response_meta["usage"] = {
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                }
            logger.debug(f"Cloud LLM responded in {elapsed:.1f}s, output_len={len(content)}")
            result = output_model.model_validate_json(content)
            parsed_ok = True
            return result
        except Exception as e:
            emit_error = repr(e)
            if attempt < max_retries:
                logger.warning(
                    f"Cloud LLM parse attempt {attempt + 1} failed: {e}. Retrying..."
                )
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"Your response was not valid JSON matching the schema. Error: {e}\n"
                    f"Please respond with ONLY valid JSON matching this schema:\n"
                    f"{json.dumps(output_model.model_json_schema(), indent=2)}",
                })
            else:
                raise
        finally:
            _emit_call_telemetry(
                provider="openai",
                model=settings.cloud_model,
                attempt=attempt,
                prompt_len=len(prompt),
                output_len=len(content),
                elapsed_seconds=elapsed,
                response_meta=response_meta,
                parse_ok=parsed_ok,
                error=emit_error,
                ticker=ticker,
                stage=stage,
                run_id=run_id,
            )
