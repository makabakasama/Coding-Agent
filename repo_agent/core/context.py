"""Run-local fixed inputs, append-only dialogue, and a replaceable state tail."""
from copy import deepcopy
import hashlib
import json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def latest_tool_result(messages):
    """Scripted models can locate a result independently of the runtime tail."""
    for message in reversed(messages):
        if message.get('role') == 'tool':
            return json.loads(message['content'])
    raise ValueError('No tool result is available in the dialogue')


class RunContext:
    def __init__(self, system_prompt, task, tool_definitions):
        self.prefix = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': 'Coding task:\n' + task + '\nThe local workspace is ready.'},
        ]
        self.history = []
        self.prefix_sha256 = fingerprint(self.prefix)
        self.tools_sha256 = fingerprint(tool_definitions)

    def extend(self, messages):
        self.history.extend(deepcopy(messages))

    def observe(self, decision, observation):
        self.extend([decision.assistant_message, decision.feedback_message(observation)])

    def assemble(self, runtime_state):
        tail = {'role': 'user', 'content': json.dumps({'runtime_state': runtime_state}, ensure_ascii=False)}
        messages = deepcopy(self.prefix + self.history) + [tail]
        metadata = {'prefix_sha256': self.prefix_sha256, 'tools_sha256': self.tools_sha256,
                    'history_messages': len(self.history), 'state_chars': len(tail['content']),
                    'input_chars': len(json.dumps(messages, ensure_ascii=False)),
                    'input_sha256': fingerprint(messages)}
        return messages, metadata
