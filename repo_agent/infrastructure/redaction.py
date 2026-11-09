"""Credential and local-path redaction for public evidence."""
import os
import re
from repo_agent.core.config import read_dotenv


class Redactor:
    def __init__(self):
        local = read_dotenv()
        self.secrets = {v for source in (os.environ, local) for k, v in source.items()
                        if re.search(r"key|token|secret|password|credential", k, re.I) and len(v) >= 4}

    def private(self, text):
        for secret in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(secret, "[REDACTED]")
        return text

    def public(self, text):
        text = self.private(text)
        text = re.sub(r"(?i)[a-z]:[\\/][^\s\"'<>]*", "[LOCAL_PATH]", text)
        text = re.sub(r"(?<![\w.])/(?!dev/null\b)[^\s\"'<>]+", "[LOCAL_PATH]", text)
        return text

    def output(self, raw):
        return self.private(raw.decode("utf-8", errors="replace")).encode("utf-8")


