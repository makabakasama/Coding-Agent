"""Read imported experiment reports from their database snapshots."""
from repo_agent.backend.schemas.task_contract import load_json
from repo_agent.backend.repositories.task_history import TaskHistory


class DatabaseTaskHistory(TaskHistory):
    def __init__(self, root, store):
        super().__init__(root)
        self.store = store

    def _stored_bytes(self, path):
        relative = path.relative_to(self.root).as_posix()
        def load():
            with self.store._connection() as conn:
                row = conn.execute('SELECT content FROM historical_materials WHERE path=%s', (relative,)).fetchone()
            return bytes(row[0]) if row is not None else None
        cache = getattr(self.store, 'cache', None)
        return cache.binary('history:' + relative, load) if cache else load()

    def _artifact_available(self, path):
        return self._stored_bytes(path) is not None or super()._artifact_available(path)

    def _artifact_bytes(self, path):
        raw = self._stored_bytes(path)
        return raw[:2 * 1024 * 1024 + 1] if raw is not None else super()._artifact_bytes(path)

    def _read(self, path, directory, warnings):
        path = self._safe(path, directory)
        relative = path.relative_to(self.root).as_posix()
        raw = self._stored_bytes(path)
        if raw is None:
            return super()._read(path, directory, warnings)
        try:
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('Report too large')
            value = load_json(raw)
            if not isinstance(value, dict):
                raise ValueError('Report is not an object')
            return value
        except (ValueError, UnicodeError, RecursionError):
            warnings.append('数据库历史报告无法解析 ' + relative)
            return {}
