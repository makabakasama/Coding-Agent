"""Independent Worker process sharing capacity between tasks and text discussions."""
import argparse
import logging
import sys
import os
import threading

from repo_agent.backend.app import add_storage_arguments, build_service
from repo_agent.backend.schemas.task_contract import TaskError
from repo_agent.backend.services.worker_pool import WorkerPool


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s [%(threadName)s] %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_arguments(parser)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--chat-max-input-bytes', type=int, default=2_000_000,
                        help='Transport safety cap in bytes, separate from the model token budget')
    parser.add_argument('--chat-max-output-tokens', type=int, default=2048)
    parser.add_argument('--chat-context-tokens', type=int, default=128_000,
                        help='Declared model context window; set to the selected model limit')
    parser.add_argument('--chat-reserve-tokens', type=int, default=4096,
                        help='Additional room for chat-template and tool growth')
    parser.add_argument('--chat-tokenizer', default='auto',
                        help='auto, tiktoken:<encoding>, or hf:<local tokenizer.json>')
    parser.add_argument('--chat-max-turn-tokens', type=int, default=250_000,
                        help='Cumulative estimated input plus reserved output tokens per Chat turn')
    parser.add_argument('--chat-max-turn-seconds', type=int, default=600,
                        help='Soft wall-time allowance for one Chat turn')
    parser.add_argument('--reload', action='store_true', help='Drain active tasks before reloading development source')
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error('workers must be 1..32')
    if (args.chat_max_input_bytes < 1 or args.chat_max_output_tokens < 1
            or args.chat_context_tokens <= args.chat_max_output_tokens + args.chat_reserve_tokens
            or args.chat_reserve_tokens < 1 or args.chat_max_turn_tokens <= args.chat_max_output_tokens
            or args.chat_max_turn_seconds < 1):
        parser.error('chat token and byte limits are inconsistent')
    if args.reload and not os.environ.get('CODING_AGENT_RELOAD_CHILD'):
        from repo_agent.infrastructure.reload import run_development
        from repo_agent.core.config import ENV_FILE
        raise SystemExit(run_development('repo_agent.backend.worker', sys.argv[1:],
                         extra_paths=[ENV_FILE, args.projects or args.data_dir / 'projects.json']))
    service = None
    try:
        service = build_service(args)
        service.chat_execution.configure(max_input_bytes=args.chat_max_input_bytes,
                                         max_output_tokens=args.chat_max_output_tokens,
                                         context_tokens=args.chat_context_tokens,
                                         reserve_tokens=args.chat_reserve_tokens,
                                         tokenizer=args.chat_tokenizer,
                                         max_turn_tokens=args.chat_max_turn_tokens,
                                         max_turn_seconds=args.chat_max_turn_seconds)
        pool = WorkerPool(service.execution.queue, service.execution.execution,
                          workers=args.workers, chat=service.chat_execution)
        stop_event = threading.Event()
        if args.reload:
            from repo_agent.infrastructure.reload import watch_reload
            watch_reload(stop_event.set)
        pool.run(stop_event)
    except TaskError as exc:
        parser.error(exc.message)
    finally:
        if service is not None:
            service.close()


if __name__ == '__main__':
    main()
