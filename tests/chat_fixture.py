"""Loopback-only acceptance fixtures with scripted models and explicit test DB."""
import argparse
import json
import logging
import os
from pathlib import Path
import threading
import time

from repo_agent.backend.app import create_server
from repo_agent.backend.repositories.database_task_store import DatabaseTaskStore
from repo_agent.backend.repositories.task_projects import TaskProjects
from repo_agent.backend.services.task_service import TaskService
from repo_agent.backend.services.worker_pool import WorkerPool
from repo_agent.core.decisions import Decision
from repo_agent.core.provider import ProviderError, TextReply
from repo_agent.core.provider import ContextLimitError


class FixtureChatModel:
    model = 'scripted-chat-acceptance'
    calls = []

    def respond(self, messages, *, tools, response_limits):
        type(self).calls.append(messages)
        if tools:
            raise AssertionError('Text-only fixture received tools')
        question = messages[-1]['content']
        if '[FAIL]' in question:
            raise ProviderError('Injected provider failure')
        if '[SLOW]' in question:
            time.sleep(0.8)
        if '[HOLD]' in question:
            root = Path(os.environ['CHAT_FIXTURE_ROOT'])
            (root / 'model-entered').touch()
            while not (root / 'release-model').exists():
                time.sleep(0.05)
        user_count = sum(message['role'] == 'user' for message in messages)
        text = f'脚本回复：{question}\n用户消息数={user_count}'
        return TextReply(text, {'role': 'assistant', 'content': text})


class FixtureCodingModel:
    def decide(self, messages):
        return Decision('finish', {'answer': '脚本模型只读交付', 'outcome': 'completed'})


class FixtureTokenBudget:
    """Deterministic counter for scripted tests; never used for real models."""
    def __init__(self, model, *, context_tokens, output_tokens, reserve_tokens, tokenizer):
        self.model = model
        self.input_tokens = context_tokens - output_tokens - reserve_tokens
        self.output_tokens = output_tokens
        self.counter_name = 'fixture:characters'

    def count(self, messages, tools):
        return len(json.dumps([messages, tools], ensure_ascii=False))

    def require(self, messages, tools):
        used = self.count(messages, tools)
        if used > self.input_tokens:
            raise ContextLimitError('Fixture token budget exceeded')
        return used

    def manifest(self, used):
        return {'model': self.model, 'counter': self.counter_name,
                'method': 'fixture', 'maxInputTokens': self.input_tokens,
                'estimatedInputTokens': used}


def build_fixture(root, *, database_url=None):
    root.mkdir(parents=True, exist_ok=True)
    config = root / 'projects.json'
    if not config.exists():
        config.write_text(json.dumps({'projects': []}), encoding='utf-8')
    store = DatabaseTaskStore(root / 'data', root / 'runs', root / 'workspaces',
                              database_url or os.environ['CODING_AGENT_TEST_DATABASE_URL'])
    return TaskService(store, TaskProjects(config), project_root=root,
                       model_factory=FixtureCodingModel, chat_model_factory=FixtureChatModel,
                       chat_token_counter_factory=FixtureTokenBudget)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('server', 'worker'))
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    os.environ['CHAT_FIXTURE_ROOT'] = str(args.root.resolve())
    logging.basicConfig(level=logging.INFO)
    service = build_fixture(args.root.resolve())
    stop = threading.Event()

    def monitor():
        while not stop.wait(0.05):
            if (args.root / 'stop').exists():
                stop.set()
                return

    threading.Thread(target=monitor, daemon=True).start()
    try:
        if args.mode == 'worker':
            WorkerPool(service.execution.queue, service.execution.execution,
                       chat=service.chat_execution, workers=2, poll_seconds=0.05).run(stop)
        else:
            frontend = Path(__file__).resolve().parents[1] / 'frontend'
            server = create_server(service, port=0, frontend=frontend)
            (args.root / 'server.json').write_text(json.dumps({'port': server.server_port}), encoding='utf-8')
            threading.Thread(target=lambda: (stop.wait(), server.shutdown()), daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.05)
            finally:
                server.server_close()
    finally:
        stop.set()
        service.close()


if __name__ == '__main__':
    main()
