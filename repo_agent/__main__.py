from __future__ import annotations
import argparse
import json
from pathlib import Path
import uuid

from repo_agent.core.agent import Agent
from repo_agent.core.coding_budget import coding_budget
from repo_agent.core.provider import OpenAICompatibleProvider, ProviderError
from repo_agent.core.tools import RepoTools
from repo_agent.infrastructure.redaction import Redactor


def main():
    p = argparse.ArgumentParser(description="Minimal repo Coding Agent — only use disposable trusted checkouts")
    p.add_argument("--repo", type=Path, required=True, help="Local git repo path")
    p.add_argument("--task", required=True, help="Issue / coding task description")
    p.add_argument("--model", help="Override OPENAI_MODEL")
    p.add_argument("--base-url", help="Override OPENAI_BASE_URL")
    p.add_argument("--max-steps", type=int, default=50)
    p.add_argument("--trace-dir", type=Path, default=Path("runs"))
    p.add_argument("--allow-writes", action="store_true", help="Allow git apply on checkout")
    p.add_argument("--allow-tests", action="store_true", help="Allow repository tests in the local environment")
    p.add_argument("--check-target", action="append", default=[], help="Restrict checks to this repository target")
    p.add_argument("--allow-temporary-checks", action="store_true",
                   help="Allow model-authored Python checks in the local environment")
    p.add_argument("--test-python", type=Path, help="Python executable for the target repository's test environment")
    p.add_argument("--test-argv", type=json.loads,
                   help='Authorized JSON argv with a separate {target} argument and optional {report} destination')
    args = p.parse_args()
    try:
        model = OpenAICompatibleProvider(model=args.model, base_url=args.base_url)
        redactor = Redactor()
        if model.api_key:
            redactor.secrets.add(model.api_key)
        directory = args.trace_dir.resolve() / ('developer-checks_' + uuid.uuid4().hex) if args.allow_tests else None
        tools = RepoTools(args.repo, allow_writes=args.allow_writes, allow_tests=args.allow_tests,
                          test_python=args.test_python, test_argv=args.test_argv,
                          test_log_dir=directory, test_output_filter=redactor.output, test_junit=True)
        if args.allow_temporary_checks and not (args.allow_tests and args.allow_writes):
            raise ValueError('Temporary checks require --allow-tests and --allow-writes')
        prompt = args.task
        if args.allow_tests and (args.check_target or args.allow_temporary_checks):
            from repo_agent.backend.services.local_development_checks import LocalDevelopmentChecks, source_snapshot, visible_path
            from repo_agent.backend.services.task_execution_tools import TaskRepoTools

            root = args.repo.resolve()
            contract = {'kind': 'fix_bug' if args.allow_writes else 'read_code',
                        'allowedPaths': ['.'] if args.allow_writes else [],
                        'allowTests': True, 'checkTargets': args.check_target,
                        'allowTemporaryChecks': args.allow_temporary_checks,
                        'current': args.task, 'expected': args.task}
            tools = TaskRepoTools(root, contract=contract, test_python=args.test_python,
                                 test_argv=args.test_argv,
                                 test_log_dir=directory, test_output_filter=redactor.output, test_junit=True,
                                 test_env={'PYTHONPATH': str(root), 'PYTHONNOUSERSITE': '1'})
            initial = source_snapshot(root)
            initial_bytes = {path: (root / path).read_bytes() for path, digest in initial.items()
                             if digest != 'LINK' and visible_path(path)}
            tools.checks = LocalDevelopmentChecks(tools, contract, directory, initial=initial,
                          snapshot=lambda: source_snapshot(root), read_initial=initial_bytes.__getitem__,
                          redactor=redactor)
            tools.require_check_before_completion = args.allow_writes
            model.configure_development_checks(temporary=args.allow_temporary_checks)
            prompt += '\nAuthorized repository check targets: ' + json.dumps(args.check_target)
        measured = coding_budget(model, {'problemStatement': args.task}, redactor)
        result = Agent(measured, tools, trace_dir=args.trace_dir, max_steps=args.max_steps,
                       token_budget=measured.budget).run(prompt)
        result['tokenAccounting'] = measured.accounting()
    except (OSError, ValueError, ProviderError) as exc:
        p.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
