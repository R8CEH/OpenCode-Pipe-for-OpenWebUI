"""
title: OpenCode
description: Run OpenCode's agent loop from inside OpenWebUI chats via subprocess.
             Supports any OpenRouter or RouterAI model. Each chat gets its own
             isolated project directory. Files created by the agent are uploaded
             as artifacts.
author: Denis Kutuzov (aka R8CEH)
author_url: https://github.com/R8CEH/OpenCode-Pipe-for-OpenWebUI
version: 0.2.0
license: MIT
requirements:
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
_DOWNLOAD_EXTENSIONS = {
    ".pdf", ".csv", ".tsv", ".txt", ".md", ".json", ".yaml", ".yml",
    ".html", ".xml", ".xlsx", ".docx", ".pptx", ".zip",
    ".py", ".js", ".ts", ".sh", ".rs", ".go", ".cpp", ".c", ".h",
}
_ARTIFACT_EXTENSIONS = _IMAGE_EXTENSIONS | _DOWNLOAD_EXTENSIONS
_MAX_ARTIFACT_BYTES = 50 * 1024 * 1024  # 50 MiB

_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".sh": "bash", ".rs": "rust", ".go": "go", ".c": "c", ".cpp": "cpp",
    ".h": "c", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".html": "html", ".css": "css", ".md": "markdown", ".sql": "sql",
    ".toml": "toml", ".xml": "xml",
}

_COMPANY_MAP = {
    "anthropic": "Anthropic", "openai": "OpenAI", "deepseek": "DeepSeek",
    "qwen": "Qwen", "google": "Google", "x-ai": "xAI", "z-ai": "Z.ai",
    "minimax": "MiniMax", "moonshotai": "Moonshot", "tencent": "Tencent",
    "xiaomi": "Xiaomi", "mistralai": "Mistral", "meta-llama": "Meta",
    "cohere": "Cohere", "nvidia": "NVIDIA", "microsoft": "Microsoft",
}

_TIER_MAP = {
    "flash": "Flash ⚡", "pro": "Pro", "max": "Max", "plus": "Plus",
    "mini": "Mini", "nano": "Nano", "turbo": "Turbo", "fast": "Fast ⚡",
    "preview": "Preview", "next": "Next", "latest": "Latest",
}

_TOOL_ICON = {
    "bash": "💻", "write": "✏️", "read": "📖", "edit": "✏️",
    "glob": "🔍", "grep": "🔍", "webfetch": "🌐", "websearch": "🌐",
    "todowrite": "📋", "task": "🤖",
}

_TOOL_LABEL = {
    "bash": "bash", "write": "write", "read": "read", "edit": "edit",
    "glob": "glob", "grep": "grep", "webfetch": "fetch", "websearch": "search",
    "todowrite": "todo", "task": "agent",
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chat state via OpenWebUI Chats API
# ---------------------------------------------------------------------------

async def _chats_call(method: str, *args):
    """Call a Chats method handling both sync and async variants."""
    from open_webui.models.chats import Chats
    result = getattr(Chats, method)(*args)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _load_chat_state(chat_id: str) -> tuple:
    """Load opencode session_id and workdir_name from chat meta."""
    try:
        chat = await _chats_call("get_chat_by_id", chat_id)
        if chat:
            meta = (chat.chat or {}).get("meta", {})
            return meta.get("opencode_session_id"), meta.get("opencode_workdir")
    except Exception as exc:
        log.warning("_load_chat_state failed: %s", exc)
    return None, None


async def _save_chat_state(chat_id: str, session_id: str, workdir_name: str):
    """Persist opencode session_id and workdir_name into chat meta."""
    try:
        chat = await _chats_call("get_chat_by_id", chat_id)
        if chat:
            chat_data = dict(chat.chat or {})
            meta = dict(chat_data.get("meta", {}))
            meta["opencode_session_id"] = session_id
            meta["opencode_workdir"] = workdir_name
            chat_data["meta"] = meta
            if "title" not in chat_data:
                chat_data["title"] = workdir_name
            await _chats_call("update_chat_by_id", chat_id, chat_data)
    except Exception as exc:
        log.warning("_save_chat_state failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers: model display name
# ---------------------------------------------------------------------------

def _model_display_name(model_id: str) -> str:
    """Convert openrouter/anthropic/claude-sonnet-4.6 → Anthropic: Claude Sonnet 4.6 (Code)"""
    parts = model_id.split("/")
    if len(parts) >= 3:
        company_slug, model_slug = parts[-2], parts[-1]
    elif len(parts) == 2:
        company_slug, model_slug = parts[0], parts[1]
    else:
        return f"{model_id} (Code)"

    company = _COMPANY_MAP.get(company_slug, company_slug.capitalize())
    tokens = model_slug.replace("_", "-").split("-")
    name_parts = []
    for tok in tokens:
        mapped = _TIER_MAP.get(tok.lower())
        if mapped:
            name_parts.append(mapped)
        elif re.match(r"^\d[\d\.]*$", tok):
            if name_parts:
                name_parts[-1] += " " + tok
            else:
                name_parts.append(tok)
        else:
            name_parts.append(tok.capitalize())

    return f"{company}: {' '.join(name_parts)} (Code)"


def _clean_model_id(raw: str) -> str:
    """Strip OpenWebUI pipe prefix from model id."""
    for prefix in ("opencode.", "function_opencode."):
        if raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


# ---------------------------------------------------------------------------
# Helpers: project name from prompt
# ---------------------------------------------------------------------------

async def _project_name_from_prompt(prompt: str, event_emitter: Optional[Callable]) -> str:
    """Extract a short project folder name from the first prompt."""
    version_match = re.search(
        r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9\.]+)+)\b", prompt
    )
    if version_match:
        name = version_match.group(1)
    else:
        explicit = re.search(
            r'(?:назов[её]м|название|named?|call(?:\s+it)?|project)\s+["\']?'
            r'([A-Za-z0-9][A-Za-z0-9_\-]{1,30})["\']?',
            prompt, re.IGNORECASE,
        )
        if explicit:
            name = explicit.group(1)
        else:
            stop = {
                "the", "and", "for", "with", "from", "that", "this",
                "let", "make", "create", "write", "simple", "just", "please",
                "можешь", "напиши", "сделай", "создай",
            }
            words = re.findall(r"[A-Za-z]{3,}", prompt)
            meaningful = [w.capitalize() for w in words if w.lower() not in stop][:3]
            name = "_".join(meaningful) or "Project"

    if event_emitter:
        try:
            await event_emitter(
                {"type": "chat:title", "data": {"title": name.replace("_", " ")}}
            )
        except Exception:
            pass
    return name


# ---------------------------------------------------------------------------
# Helpers: artifacts
# ---------------------------------------------------------------------------

def _iter_artifact_files(scan_dirs: List[Path]) -> List[Path]:
    result = []
    for d in scan_dirs:
        if not d.exists():
            continue
        for path in d.iterdir():
            if (path.is_file()
                    and path.suffix.lower() in _ARTIFACT_EXTENSIONS
                    and not path.name.startswith(".")):
                result.append(path)
    return result


def _snapshot_artifacts(scan_dirs: List[Path]) -> Dict[str, int]:
    snapshot: Dict[str, int] = {}
    for path in _iter_artifact_files(scan_dirs):
        try:
            snapshot[str(path)] = path.stat().st_mtime_ns
        except OSError:
            pass
    return snapshot


async def _upload_new_artifacts(
    scan_dirs: List[Path],
    before: Dict[str, int],
    user_id: Optional[str],
) -> List[str]:
    if not user_id:
        return ["\n\n_(Can't save artifacts: no user context.)_\n"]
    try:
        from open_webui.models.files import FileForm, Files
        from open_webui.storage.provider import Storage
    except Exception as exc:
        return [f"\n\n_(File store unavailable: {exc})_\n"]

    chunks: List[str] = []
    for path in sorted(_iter_artifact_files(scan_dirs)):
        try:
            mtime = path.stat().st_mtime_ns
            size = path.stat().st_size
        except OSError:
            continue
        if before.get(str(path)) == mtime:
            continue
        if size > _MAX_ARTIFACT_BYTES:
            chunks.append(
                f"\n\n_(Skipped {path.name}: "
                f"{size // 1024 // 1024} MiB exceeds limit.)_\n"
            )
            continue

        ext = path.suffix.lower()
        is_image = ext in _IMAGE_EXTENSIONS
        mime = mimetypes.guess_type(path.name)[0] or (
            "image/png" if is_image else "application/octet-stream"
        )
        file_id = str(uuid.uuid4())
        try:
            with path.open("rb") as handle:
                contents, storage_path = Storage.upload_file(
                    handle, f"{file_id}_{path.name}",
                    {"OpenWebUI-User-Id": user_id, "OpenWebUI-File-Id": file_id},
                )
        except Exception as exc:
            log.exception("Artifact upload failed: %s", path)
            chunks.append(f"\n\n_(Failed to save {path.name}: {exc})_\n")
            continue

        try:
            await Files.insert_new_file(
                user_id,
                FileForm(
                    id=file_id, filename=path.name, path=storage_path, data={},
                    meta={"name": path.name, "content_type": mime, "size": len(contents)},
                ),
            )
        except Exception as exc:
            log.warning("DB insert failed: %s -> %s", path.name, exc)
            chunks.append(f"\n\n_(Saved but not linkable: {path.name}: {exc})_\n")
            continue

        if is_image:
            chunks.append(f"\n\n![{path.name}](/api/v1/files/{file_id}/content)\n")
        else:
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_str = f"{size / 1024:.1f} KiB"
            else:
                size_str = f"{size / 1024 / 1024:.1f} MiB"
            chunks.append(
                f"\n\n📎 [{path.name}](/api/v1/files/{file_id}/content) · {size_str}\n"
            )
    return chunks


# ---------------------------------------------------------------------------
# Helpers: message extraction
# ---------------------------------------------------------------------------

def _extract_latest_user_prompt(body: Dict[str, Any]) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            texts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return " ".join(texts).strip()
    return ""


def _extract_system_prompt(body: Dict[str, Any]) -> Optional[str]:
    parts: List[str] = []
    for msg in body.get("messages") or []:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and piece.get("type") == "text":
                    parts.append(piece.get("text", ""))
    merged = "\n\n".join(p for p in parts if p and p.strip())
    return merged or None


# ---------------------------------------------------------------------------
# Helpers: tool display
# ---------------------------------------------------------------------------

def _tool_status_line(tool_name: str, state: Dict[str, Any]) -> str:
    name_lower = tool_name.lower()
    icon = _TOOL_ICON.get(name_lower, "🔧")
    label = _TOOL_LABEL.get(name_lower, tool_name)
    inp = state.get("input") or {}

    preview = ""
    for key, transform in [
        ("command", lambda v: v.split("\n")[0][:80]),
        ("filePath", lambda v: Path(v).name),
        ("description", lambda v: v[:80]),
        ("query", lambda v: v[:80]),
        ("url", lambda v: v[:80]),
        ("pattern", lambda v: v[:80]),
    ]:
        if key in inp:
            preview = transform(inp[key])
            break

    return f"{icon} {label}: {preview}" if preview else f"{icon} {label}"


def _tool_detail_block(tool_name: str, state: Dict[str, Any]) -> str:
    name_lower = tool_name.lower()
    icon = _TOOL_ICON.get(name_lower, "🔧")
    label = _TOOL_LABEL.get(name_lower, tool_name)
    inp = state.get("input") or {}
    out = state.get("output") or ""
    title_field = state.get("title", "")

    if title_field:
        summary = f"{icon} {title_field}"
    else:
        preview = ""
        for key in ("command", "filePath", "description", "query", "url", "pattern"):
            if key in inp:
                preview = str(inp[key]).split("\n")[0][:80]
                break
        summary = f"{icon} {label}" + (f": {preview}" if preview else "")

    if name_lower in ("write", "edit") and "content" in inp:
        file_path = inp.get("filePath", "")
        ext = Path(file_path).suffix.lower()
        lang = _EXT_TO_LANG.get(ext, "text")
        display_path = (
            "/".join(Path(file_path).parts[-2:])
            if len(Path(file_path).parts) >= 2
            else file_path
        )
        input_block = f"`{display_path}`\n\n```{lang}\n{inp['content']}\n```"
    elif "command" in inp:
        input_block = f"```bash\n{inp['command']}\n```"
    else:
        input_block = f"```json\n{json.dumps(inp, indent=2, ensure_ascii=False)}\n```"

    output_block = ""
    if isinstance(out, str) and out.strip() and out.strip() not in (
        "Wrote file successfully.", "OK"
    ):
        truncated = out[:2000] + ("…" if len(out) > 2000 else "")
        output_block = f"\n\n```\n{truncated}\n```"

    return (
        f"\n\n<details>\n<summary>{summary}</summary>\n\n"
        f"{input_block}{output_block}\n\n</details>\n\n"
    )


# ---------------------------------------------------------------------------
# Pipe class
# ---------------------------------------------------------------------------

class Pipe:
    class Valves(BaseModel):
        OPENROUTER_API_KEY: str = Field(
            default="",
            description="OpenRouter API key (sk-or-v1-...)",
        )
        MODELS: str = Field(
            default=(
                "openrouter/qwen/qwen3-coder-next,"
                "openrouter/qwen/qwen3.6-plus,"
                "openrouter/deepseek/deepseek-v4-flash"
            ),
            description=(
                "Comma-separated model IDs: openrouter/<company>/<model>. "
                "Each becomes a separate entry in OpenWebUI."
            ),
        )
        WORKDIR_ROOT: str = Field(
            default=str(Path.home() / "OpenCode"),
            description="Root directory for project workspaces.",
        )
        AGENTS_MD_TEMPLATE: str = Field(
            default="",
            description="Path to AGENTS.md template. Copied into every new workspace.",
        )
        MAX_TURNS: int = Field(
            default=0,
            description="Maximum agent turns (0 = unlimited).",
        )
        OPENCODE_BIN: str = Field(
            default="",
            description=(
                "Full path to opencode binary. Leave empty to auto-detect. "
                "Example: ~/.opencode/bin/opencode"
            ),
        )

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self) -> List[Dict[str, str]]:
        result = []
        for raw in self.valves.MODELS.split(","):
            model_id = raw.strip()
            if model_id:
                result.append({"id": model_id, "name": _model_display_name(model_id)})
        return result

    async def pipe(
        self,
        body: Dict[str, Any],
        __chat_id__: Optional[str] = None,
        __event_emitter__: Optional[Callable] = None,
        __user__: Optional[Dict[str, Any]] = None,
        __metadata__: Optional[Dict[str, Any]] = None,
        __files__: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[str, None]:

        # --- API key ---
        if self.valves.OPENROUTER_API_KEY:
            os.environ["OPENROUTER_API_KEY"] = self.valves.OPENROUTER_API_KEY

        # --- Determine model ---
        raw_model = _clean_model_id(body.get("model") or "")
        registered = [m["id"] for m in self.pipes()]
        model_id = registered[0] if registered else ""
        for candidate in registered:
            if raw_model.endswith(candidate) or raw_model.endswith(candidate.replace("/", ".")):
                model_id = candidate
                break

        # --- Extract prompt ---
        prompt = _extract_latest_user_prompt(body)
        if not prompt:
            yield "_No user message found._"
            return

        # --- Workdir ---
        chat_id = __chat_id__ or "default"
        session_id, workdir_name = await _load_chat_state(chat_id)

        if not workdir_name:
            workdir_name = await _project_name_from_prompt(prompt, __event_emitter__)
            if workdir_name == "Project":
                workdir_name = f"Project_{chat_id[:6]}"

        workdir = Path(self.valves.WORKDIR_ROOT) / workdir_name
        workdir.mkdir(parents=True, exist_ok=True)

        # --- AGENTS.md template ---
        if self.valves.AGENTS_MD_TEMPLATE:
            template = Path(self.valves.AGENTS_MD_TEMPLATE)
            agents_md = workdir / "AGENTS.md"
            if template.exists() and not agents_md.exists():
                shutil.copy2(template, agents_md)

        # --- Build command ---
        opencode_bin = (
            self.valves.OPENCODE_BIN
            or shutil.which("opencode")
            or str(Path.home() / ".opencode" / "bin" / "opencode")
        )

        cmd = [opencode_bin, "run", "--format", "json", "--dangerously-skip-permissions"]
        cmd += ["--model", model_id, "--dir", str(workdir)]

        if session_id:
            cmd += ["--session", session_id, "--continue"]

        if self.valves.MAX_TURNS > 0:
            cmd += ["--max-turns", str(self.valves.MAX_TURNS)]

        # inject cwd note + system prompt into the prompt itself
        system_prompt = _extract_system_prompt(body)
        cwd_note = (
            f"Working directory: {workdir}. "
            "Always write files here, never use /tmp or absolute paths."
        )
        full_prompt = (
            f"{cwd_note}\n\n{system_prompt}\n\n{prompt}"
            if system_prompt
            else f"{cwd_note}\n\n{prompt}"
        )
        cmd.append(full_prompt)

        # --- Emit helper ---
        async def emit_status(description: str, done: bool = False) -> None:
            if __event_emitter__:
                await __event_emitter__(
                    {"type": "status", "data": {"description": description, "done": done}}
                )

        # --- Run ---
        scan_dirs = [workdir]
        artifact_snapshot = _snapshot_artifacts(scan_dirs)
        await emit_status("Starting OpenCode…")

        total_tokens = 0
        new_session_id: Optional[str] = None
        heartbeat_task: Optional[asyncio.Task] = None
        active_tool_label: Optional[str] = None
        active_tool_start: float = 0.0

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(2)
                    if active_tool_label:
                        elapsed = int(time.monotonic() - active_tool_start)
                        await emit_status(f"⏳ {active_tool_label} · {elapsed}s…")
            except asyncio.CancelledError:
                pass

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ},
                cwd=str(workdir),
            )
            heartbeat_task = asyncio.create_task(_heartbeat())

            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type")

                if etype == "step_start":
                    sid = event.get("sessionID")
                    if sid and not new_session_id:
                        new_session_id = sid
                        await _save_chat_state(chat_id, sid, workdir_name)

                elif etype == "tool_use":
                    part = event.get("part") or {}
                    tool_name = part.get("tool", "tool")
                    state = part.get("state") or {}
                    status_val = state.get("status", "")

                    if status_val == "running":
                        active_tool_label = _tool_status_line(tool_name, state)
                        active_tool_start = time.monotonic()
                        await emit_status(f"⚙️ {active_tool_label}")
                    elif status_val == "completed":
                        active_tool_label = None
                        await emit_status(f"✅ {_tool_status_line(tool_name, state)}")
                        yield _tool_detail_block(tool_name, state)

                elif etype == "step_finish":
                    total_tokens += (
                        (event.get("part") or {}).get("tokens", {}).get("total", 0)
                    )

                elif etype == "text":
                    text = (event.get("part") or {}).get("text", "")
                    if text:
                        yield text

                elif etype == "error":
                    err = event.get("error") or {}
                    msg = err.get("data", {}).get("message") or str(err)
                    await emit_status(f"❌ {msg}", done=True)
                    yield f"\n\n**OpenCode error:** `{msg}`\n"
                    return

            await process.wait()

        except Exception as exc:
            log.exception("OpenCode pipe failed")
            await emit_status(f"Error: {exc}", done=True)
            yield f"\n\n**OpenCode error:** `{type(exc).__name__}: {exc}`\n"
            return

        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

        await emit_status("Done.", done=True)

        for chunk in await _upload_new_artifacts(
            scan_dirs, artifact_snapshot, (__user__ or {}).get("id"),
        ):
            yield chunk

        if total_tokens:
            yield f"\n\n_📊 Tokens: {total_tokens:,}_\n"
