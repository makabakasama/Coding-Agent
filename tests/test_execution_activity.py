"""Process recovery checks use a mocked inventory, never stop real processes."""
import json
import os
import unittest
from unittest.mock import patch

from repo_agent.execution_activity import active_processes


class ActivityTest(unittest.TestCase):
    def inventory(self, rows):
        if os.name == 'nt':
            return json.dumps([{'ProcessId': pid, 'ParentProcessId': parent, 'CommandLine': command}
                               for pid, parent, command in rows]).encode()
        return '\n'.join(f'{pid} {parent} {command or ""}' for pid, parent, command in rows).encode()

    def test_finds_children_and_workspace_commands_but_excludes_inspector(self):
        rows = [(100, 1, 'workbench'), (101, 100, 'pytest relative.py'),
                (102, 1, 'git -C E:/fixture/workspace status'), (999, 100, 'inventory')]
        with patch('repo_agent.execution_activity.subprocess.Popen') as factory:
            process = factory.return_value
            process.pid, process.returncode = 999, 0
            process.communicate.return_value = (self.inventory(rows), b'')
            self.assertEqual([101, 102], active_processes(['E:/fixture/workspace'], 100))

    def test_inventory_failure_is_not_interpreted_as_no_processes(self):
        with patch('repo_agent.execution_activity.subprocess.Popen') as factory:
            process = factory.return_value
            process.pid, process.returncode = 999, 1
            process.communicate.return_value = (b'', b'denied')
            with self.assertRaises(RuntimeError):
                active_processes(['E:/fixture/workspace'], 100)
