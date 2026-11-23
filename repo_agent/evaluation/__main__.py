"""Small-subset evaluation CLI. No work happens without an explicit subcommand."""
import argparse
import json

from repo_agent.evaluation.grader import execute_grader, grading_command
from repo_agent.evaluation.runner import run_agent
from repo_agent.evaluation.swebench import DATASETS, import_grade, prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    preparation = commands.add_parser('prepare', help='Prepare one local official record and a fresh checkout; no model/tests')
    preparation.add_argument('--dataset', choices=DATASETS, default='lite')
    preparation.add_argument('--task-file', required=True, help='Official selected record(s), JSON or JSONL; may contain hidden grading fields')
    preparation.add_argument('--instance-id', required=True)
    preparation.add_argument('--source-repo', required=True, help='Local source checkout containing the fixed base commit')
    preparation.add_argument('--runs-root', default='runs/evaluation/swe-bench')
    preparation.add_argument('--workspaces-root', help='Override experiments/<upstream-project>/workspaces')
    running = commands.add_parser('run-agent', help='Call the real configured Provider on the prepared task')
    running.add_argument('--run-dir', required=True)
    running.add_argument('--baseline-report', required=True, help='Externally verified baseline report with tests_status')
    running.add_argument('--max-steps', type=int, required=True)
    running.add_argument('--max-total-tokens', type=int, required=True,
                         help='Hard cumulative accounting limit. Complete provider usage is charged when available; '
                              'otherwise each request uses its preflight estimate, including output allowance. '
                              'One maximum-context request is reserved for a final answer.')
    running.add_argument('--max-seconds', type=int, required=True)
    running.add_argument('--context-tokens', type=int, default=128_000)
    running.add_argument('--output-tokens', type=int, default=2048)
    running.add_argument('--tokenizer', default='auto')
    running.add_argument('--agent-tests', action='store_true', help='Enable temporary/existing developer checks in a verified container; no official hidden tests')
    running.add_argument('--check-image', help='Preloaded immutable sha256 image ID with clean fixed G/tree')
    running.add_argument('--check-timeout', type=int, default=90, help='Per-check test time limit, 1-300 seconds')
    running.add_argument('--check-python', help='Host interpreter with Docker SDK; defaults to current Python')
    running.add_argument('--check-argv', type=json.loads,
                         help='Authorized container test argv with separate {target} and optional {report} arguments')
    grading = commands.add_parser('grade', help='Show the official command; --execute explicitly starts evaluator/Docker/tests')
    grading.add_argument('--run-dir', required=True)
    grading.add_argument('--python', help='Interpreter with swebench==5.0.2')
    grading.add_argument('--timeout', type=int, default=600)
    grading.add_argument('--execute', action='store_true')
    importing = commands.add_parser('import-grade', help='Import an existing official per-instance report; no tests/model')
    importing.add_argument('--run-dir', required=True)
    importing.add_argument('--report', required=True)
    importing.add_argument('--evaluated-patch', required=True)
    importing.add_argument('--test-output')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            result = prepare(args.task_file, args.instance_id, args.source_repo, dataset=args.dataset,
                             runs_root=args.runs_root, workspaces_root=args.workspaces_root)
        elif args.action == 'run-agent':
            result = run_agent(args.run_dir, baseline_report=args.baseline_report,
                               max_steps=args.max_steps, max_total_tokens=args.max_total_tokens,
                               max_seconds=args.max_seconds, context_tokens=args.context_tokens,
                               output_tokens=args.output_tokens, tokenizer=args.tokenizer,
                               agent_tests=args.agent_tests, check_image=args.check_image,
                               check_timeout=args.check_timeout, check_python=args.check_python,
                               check_argv=args.check_argv)
        elif args.action == 'grade':
            if not args.execute:
                print(json.dumps({'executed': False,
                      'command': grading_command(args.run_dir, python=args.python, timeout=args.timeout)}, ensure_ascii=False))
                return
            result = execute_grader(args.run_dir, python=args.python, timeout=args.timeout)
        else:
            result = import_grade(args.run_dir, args.report, args.evaluated_patch, test_output=args.test_output)
        print(json.dumps({'artifactDirectory': str(result)}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f'Evaluation stopped: {type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
