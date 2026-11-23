"""Credential and local-path redaction for public evidence."""
import os
import re
from repo_agent.core.config import read_dotenv


class Redactor:
    def __init__(self):
        local = read_dotenv()
        self.secrets = {v for source in (os.environ, local) for k, v in source.items()
                        if not re.fullmatch(r'GIT_CONFIG_KEY_\d+', k, re.I)
                        and re.search(r"key|token|secret|password|credential", k, re.I) and len(v) >= 4}
        # Git KEY_n contains a configuration name, not its value. Credentials in
        # an associated VALUE_n still need protection when the key is sensitive.
        for source in (os.environ, local):
            for name, key in source.items():
                if (re.fullmatch(r'GIT_CONFIG_KEY_\d+', name)
                        and re.search(r'authorization|extraheader|password|token|secret|credential', key, re.I)):
                    value = source.get(name.replace('_KEY_', '_VALUE_'), '')
                    if len(value) >= 4:
                        self.secrets.add(value)

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

    def private_value(self, value):
        """Redact credentials in local structured evidence while retaining path references."""
        if isinstance(value, str):
            return self.private(value)
        if isinstance(value, list):
            return [self.private_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.private_value(item) for key, item in value.items()}
        return value

    def public_value(self, value):
        """Redact structured values without editing JSON quoting or field names."""
        if isinstance(value, str):
            return self.public(value)
        if isinstance(value, list):
            return [self.public_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.public_value(item) for key, item in value.items()}
        return value


