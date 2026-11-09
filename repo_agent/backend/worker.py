"""Independent Worker process for persisted ordinary task dispatches."""
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
    parser.add_argument('--reload', action='store_true', help='Drain active tasks before reloading development source')
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error('workers must be 1..32')
    if args.reload and not os.environ.get('CODING_AGENT_RELOAD_CHILD'):
        from repo_agent.infrastructure.reload import run_development
        from repo_agent.core.config import ENV_FILE
        raise SystemExit(run_development('repo_agent.backend.worker', sys.argv[1:],
                         extra_paths=[ENV_FILE, args.projects or args.data_dir / 'projects.json']))
    service = None
    try:
        service = build_service(args)
        pool = WorkerPool(service.execution.queue, service.execution.execution, workers=args.workers)
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
