"""Independent Worker process for persisted ordinary task dispatches."""
import argparse
import logging
import sys

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
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        parser.error('workers must be 1..32')
    try:
        service = build_service(args)
        pool = WorkerPool(service.execution.queue, service.execution.execution, workers=args.workers)
        pool.run()
    except TaskError as exc:
        parser.error(exc.message)


if __name__ == '__main__':
    main()
