"""Read the project's local model configuration without changing process env."""
from pathlib import Path


CONFIG_KEYS = {"LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
               "LLM_PROFILE", "OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY", "DASHSCOPE_API_KEY"}
CONFIG_KEYS.update(f"{profile}_{field}" for profile in ("QWEN", "GPT")
                   for field in ("MODEL", "BASE_URL", "API_KEY"))
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def read_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    """Support literal KEY=value lines, optional outer quotes and full-line comments.

    Values are never executed or expanded. Inline comments are not supported.
    Only model configuration keys are read; a missing file is optional.
    """
    if not path.exists():
        return {}
    values = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if key not in CONFIG_KEYS:
            continue
        if not separator:
            raise ValueError(f"Invalid model configuration in .env at line {number}")
        value = value.strip()
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError(f"Unclosed quote in .env at line {number}")
            value = value[1:-1]
        values[key] = value
    return values
