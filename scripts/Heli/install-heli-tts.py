from __future__ import annotations

import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path


# ============================================================
# Paths
# ============================================================

REPO_ROOT = Path(__file__).resolve().parents[2]

LOCAL_COSYVOICE_FILE = (
    REPO_ROOT
    / "main_logic"
    / "tts_client"
    / "workers"
    / "local_cosyvoice.py"
)

TTS_INIT_FILE = (
    REPO_ROOT
    / "main_logic"
    / "tts_client"
    / "__init__.py"
)

REGISTRY_META_FILE = (
    REPO_ROOT
    / "main_logic"
    / "tts_client"
    / "_registry_meta.py"
)

FILES_TO_PATCH = (
    LOCAL_COSYVOICE_FILE,
    TTS_INIT_FILE,
    REGISTRY_META_FILE,
)


# ============================================================
# Markers
# ============================================================

WORKER_PATCH_MARKER = "# HELI_TTS_LOCAL_COSYVOICE_ADAPTER_V1"
REGISTRY_PATCH_MARKER = "# HELI_TTS_LOCAL_COSYVOICE_PROVIDER_V1"
META_PATCH_MARKER = "# HELI_TTS_LOCAL_COSYVOICE_META_V1"


# ============================================================
# Helpers
# ============================================================

def fail(message: str) -> None:
    print()
    print(f"[ERROR] {message}")
    raise SystemExit(1)


def read(path: Path) -> str:
    if not path.is_file():
        fail(f"Required file not found:\n{path}")

    return path.read_text(
        encoding="utf-8",
    )


def write(path: Path, content: str) -> None:
    path.write_text(
        content,
        encoding="utf-8",
        newline="\n",
    )


def backup_files() -> Path:
    timestamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    backup_root = (
        REPO_ROOT
        / "scripts"
        / "heli"
        / "backups"
        / timestamp
    )

    for source in FILES_TO_PATCH:
        relative = source.relative_to(
            REPO_ROOT
        )

        destination = (
            backup_root
            / relative
        )

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            source,
            destination,
        )

    print(
        f"[OK] Backup created:\n"
        f"     {backup_root}"
    )

    return backup_root


def compile_file(path: Path) -> None:
    py_compile.compile(
        str(path),
        doraise=True,
    )


# ============================================================
# Patch 1
# local_cosyvoice.py
# ============================================================

def patch_local_cosyvoice() -> bool:
    path = LOCAL_COSYVOICE_FILE
    content = read(path)

    changed = False

    # --------------------------------------------------------
    # Disable system proxy for localhost WebSocket
    # --------------------------------------------------------

    old_connect = (
        "ws = await websockets.connect("
        "WS_URL, ping_interval=None)"
    )

    new_connect = """ws = await websockets.connect(
                WS_URL,
                ping_interval=None,
                proxy=None,
            )"""

    if "proxy=None" not in content:
        if old_connect not in content:
            fail(
                "Could not find expected "
                "websockets.connect() call in:\n"
                f"{path}"
            )

        content = content.replace(
            old_connect,
            new_connect,
            1,
        )

        changed = True

        print(
            "[PATCH] local_cosyvoice: "
            "disabled proxy for local WS"
        )

    else:
        print(
            "[SKIP] local_cosyvoice: "
            "proxy=None already present"
        )

    # --------------------------------------------------------
    # Provider selection adapter
    # --------------------------------------------------------

    if WORKER_PATCH_MARKER not in content:

        adapter_code = f'''


{WORKER_PATCH_MARKER}

def _local_cosyvoice_is_selected(ctx) -> bool:
    """
    Select the local CosyVoice-compatible endpoint
    configured through the N.E.K.O TTS settings.

    Our Heli TTS Gateway exposes that protocol while
    internally routing synthesis to Fish S2.
    """

    provider = str(
        ctx.core_config.get(
            "ttsModelProvider"
        )
        or ""
    ).strip().lower()

    if provider != "local_cosyvoice":
        return False

    try:
        tts_config = (
            ctx.cm.get_model_api_config(
                "tts_custom"
            )
            or {{}}
        )
    except Exception:
        return False

    if not tts_config.get("is_custom"):
        return False

    base_url = str(
        tts_config.get("base_url")
        or ""
    ).strip().lower()

    return base_url.startswith(
        (
            "ws://",
            "wss://",
        )
    )


def _local_cosyvoice_resolve(ctx):
    """
    Resolve the configured provider to the existing
    local CosyVoice worker.

    The gateway is local and doesn't need an API key.
    """

    _ = ctx

    return (
        local_cosyvoice_worker,
        None,
        "local_cosyvoice",
    )
'''

        content = (
            content.rstrip()
            + adapter_code
            + "\n"
        )

        changed = True

        print(
            "[PATCH] local_cosyvoice: "
            "added provider adapters"
        )

    else:
        print(
            "[SKIP] local_cosyvoice: "
            "provider adapters already installed"
        )

    if changed:
        write(
            path,
            content,
        )

    return changed


# ============================================================
# Patch 2
# main_logic/tts_client/__init__.py
# ============================================================

def patch_tts_init() -> bool:
    path = TTS_INIT_FILE
    content = read(path)

    changed = False

    # --------------------------------------------------------
    # Import provider adapters
    # --------------------------------------------------------

    old_import = (
        "from .workers.local_cosyvoice "
        "import local_cosyvoice_worker"
    )

    new_import = """from .workers.local_cosyvoice import (
    local_cosyvoice_worker,
    _local_cosyvoice_is_selected,
    _local_cosyvoice_resolve,
)"""

    if (
        "_local_cosyvoice_is_selected"
        not in content
    ):
        if old_import not in content:
            fail(
                "Could not find expected "
                "local_cosyvoice import in:\n"
                f"{path}"
            )

        content = content.replace(
            old_import,
            new_import,
            1,
        )

        changed = True

        print(
            "[PATCH] tts_client: "
            "imported Heli local TTS adapters"
        )

    else:
        print(
            "[SKIP] tts_client: "
            "adapter imports already present"
        )

    # --------------------------------------------------------
    # Register local provider
    # --------------------------------------------------------

    if REGISTRY_PATCH_MARKER not in content:

        vllm_marker = """_tts_providers.register(_tts_providers.TTSProvider(
    key='vllm_omni',"""

        if vllm_marker not in content:
            fail(
                "Could not find vllm_omni "
                "provider registration in:\n"
                f"{path}"
            )

        provider_block = f'''{REGISTRY_PATCH_MARKER}

_tts_providers.register(_tts_providers.TTSProvider(
    key="local_cosyvoice",
    kind="local",

    # After local GPT-SoVITS but before
    # vLLM-Omni and cloud fallbacks.
    priority=15,

    # heli.s2voice behaves as a configured
    # local preset from N.E.K.O's point of view.
    capabilities=frozenset({{"preset"}}),

    is_selected=_local_cosyvoice_is_selected,
    resolve=_local_cosyvoice_resolve,

    default_url="ws://127.0.0.1:8093",
    default_model="fish-s2-q5",
    default_voice="heli",

    editable_endpoint=True,
    configured_preset_voice=True,

    # Heli Gateway performs its own readiness
    # and Fish warm-up checks.
    probe_kind="none",

    # If our local TTS fails we don't silently
    # replace Heli's voice with another provider.
    fallback_on_failure=False,
))


'''

        content = content.replace(
            vllm_marker,
            provider_block + vllm_marker,
            1,
        )

        changed = True

        print(
            "[PATCH] tts_client: "
            "registered local_cosyvoice"
        )

    else:
        print(
            "[SKIP] tts_client: "
            "provider already registered"
        )

    if changed:
        write(
            path,
            content,
        )

    return changed


# ============================================================
# Patch 3
# _registry_meta.py
# ============================================================

def patch_registry_meta() -> bool:
    path = REGISTRY_META_FILE
    content = read(path)

    if META_PATCH_MARKER in content:
        print(
            "[SKIP] registry meta: "
            "Heli metadata already installed"
        )

        return False

    start_marker = (
        '    "local_cosyvoice": '
        'TTSProviderMeta('
    )

    end_marker = (
        '    "vllm_omni": '
        'TTSProviderMeta('
    )

    start = content.find(
        start_marker
    )

    end = content.find(
        end_marker,
        start,
    )

    if start == -1 or end == -1:
        fail(
            "Could not locate "
            "local_cosyvoice metadata block in:\n"
            f"{path}"
        )

    replacement = f'''    {META_PATCH_MARKER}
    "local_cosyvoice": TTSProviderMeta(
        name="local_cosyvoice",

        # The existing worker is a true
        # bidirectional WebSocket stream.
        category="ws_bistream",

        protocol=(
            "WebSocket "
            "(local CosyVoice-compatible stream)"
        ),

        input_streaming=True,
        output_streaming=True,

        # The Heli Gateway handles sentence
        # buffering before Fish S2.
        client_sentence_split=False,

        audio_format=(
            "PCM 22.05kHz -> "
            "resample 48kHz"
        ),

        notes=(
            "Heli local TTS gateway; "
            "routes N.E.K.O speech to Fish S2"
        ),
    ),

'''

    content = (
        content[:start]
        + replacement
        + content[end:]
    )

    write(
        path,
        content,
    )

    print(
        "[PATCH] registry meta: "
        "set local_cosyvoice to ws_bistream"
    )

    return True


# ============================================================
# Validation
# ============================================================

def validate_files() -> None:
    print()
    print(
        "=== Syntax validation ==="
    )

    for path in FILES_TO_PATCH:
        compile_file(path)

        print(
            f"[OK] {path.relative_to(REPO_ROOT)}"
        )


def validate_registry() -> None:
    print()
    print(
        "=== Provider validation ==="
    )

    # Ensure repository root is importable.
    sys.path.insert(
        0,
        str(REPO_ROOT),
    )

    import main_logic.tts_client  # noqa: F401
    from utils.tts import provider_registry

    provider = provider_registry.get(
        "local_cosyvoice"
    )

    if provider is None:
        fail(
            "local_cosyvoice was not "
            "registered"
        )

    if provider.priority != 15:
        fail(
            "Unexpected local_cosyvoice "
            f"priority: {provider.priority}"
        )

    print(
        "[OK] provider key: "
        f"{provider.key}"
    )

    print(
        "[OK] priority: "
        f"{provider.priority}"
    )

    print(
        "[OK] default URL: "
        f"{provider.default_url}"
    )

    print(
        "[OK] default model: "
        f"{provider.default_model}"
    )

    print(
        "[OK] default voice: "
        f"{provider.default_voice}"
    )


# ============================================================
# Main
# ============================================================

def main() -> None:
    print()
    print(
        "========================================"
    )
    print(
        " Heli TTS integration installer"
    )
    print(
        " N.E.K.O -> Gateway -> Fish S2"
    )
    print(
        "========================================"
    )
    print()

    print(
        f"Repository:\n{REPO_ROOT}"
    )
    print()

    # Verify source files before touching anything.
    for path in FILES_TO_PATCH:
        if not path.is_file():
            fail(
                f"Missing source file:\n{path}"
            )

    backup_files()

    changes = []

    changes.append(
        patch_local_cosyvoice()
    )

    changes.append(
        patch_tts_init()
    )

    changes.append(
        patch_registry_meta()
    )

    validate_files()
    validate_registry()

    print()
    print(
        "========================================"
    )

    if any(changes):
        print(
            "[SUCCESS] Heli TTS integration "
            "installed."
        )
    else:
        print(
            "[SUCCESS] Heli TTS integration "
            "was already installed."
        )

    print()
    print(
        "Next:"
    )
    print(
        "  1. Start Fish S2 on :3030"
    )
    print(
        "  2. Start Heli Gateway on :8093"
    )
    print(
        "  3. Configure N.E.K.O TTS provider"
    )
    print(
        "     provider = local_cosyvoice"
    )
    print(
        "     URL      = ws://127.0.0.1:8093"
    )
    print(
        "     model    = fish-s2-q5"
    )
    print(
        "     voice    = heli"
    )
    print(
        "========================================"
    )
    print()


if __name__ == "__main__":
    main()