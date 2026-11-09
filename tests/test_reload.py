from pathlib import Path
import tempfile
import threading
import time
import os
import unittest
from unittest.mock import patch, Mock

from repo_agent.infrastructure.reload import source_version, watch_reload, run_development


class ReloadTest(unittest.TestCase):
    def test_detects_source_change_addition_deletion_and_config_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'repo_agent').mkdir()
            code = root/'repo_agent/app.py'
            config = root/'.env'
            with patch('repo_agent.infrastructure.reload.SOURCE_ROOT',root), patch('repo_agent.infrastructure.reload.SOURCE_PACKAGES',('repo_agent',)):
                before = source_version([config])
                code.write_text('first',encoding='utf-8')
                added = source_version([config])
                self.assertNotEqual(before,added)
                code.write_text('changed source',encoding='utf-8')
                changed = source_version([config])
                self.assertNotEqual(added,changed)
                config.write_text('private configuration',encoding='utf-8')
                self.assertNotEqual(changed,source_version([config]))
                self.assertNotIn('private configuration',str(source_version([config])))
                code.unlink()
                self.assertNotEqual(changed,source_version([config]))

    def test_child_shutdown_callback_runs_after_reload_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'stop'
            stopped = threading.Event()
            with patch.dict('os.environ',{'CODING_AGENT_RELOAD_STOP_FILE':str(path)}):
                watch_reload(stopped.set)
                self.assertFalse(stopped.wait(.05))
                path.touch()
                self.assertTrue(stopped.wait(2))

    def test_supervisor_waits_for_child_exit_before_starting_replacement(self):
        first, second = Mock(), Mock()
        first.poll.side_effect = [None,None,0]
        first.returncode = 0
        second.poll.return_value = 0
        second.returncode = 0
        launched = []
        def poll_first():
            if first.poll.call_count == 2:
                self.assertTrue(Path(launched[0][1]['env']['CODING_AGENT_RELOAD_STOP_FILE']).exists())
            return None if first.poll.call_count < 3 else 0
        first.poll.side_effect = poll_first
        def spawn_checked(*args,**kwargs):
            if launched:
                self.assertEqual(3,first.poll.call_count)
            launched.append((args,kwargs))
            return first if len(launched)==1 else second
        with patch('repo_agent.infrastructure.reload.source_version',side_effect=['old','new','new','new']), \
             patch('repo_agent.infrastructure.reload.subprocess.Popen',side_effect=spawn_checked), \
             patch('repo_agent.infrastructure.reload.time.sleep'):
            self.assertEqual(0,run_development('example.module',['--reload']))
        self.assertEqual(2,len(launched))


    def test_real_child_process_drains_before_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'repo_agent').mkdir()
            watched = root/'repo_agent/watched.py'
            watched.write_text('# initial',encoding='utf-8')
            (root/'reload_fixture.py').write_text("""
import os, threading
from pathlib import Path
from repo_agent.infrastructure.reload import watch_reload
root=Path(os.environ['RELOAD_TEST_DIR'])
second=(root/'started-1').exists()
(root/('started-2' if second else 'started-1')).touch()
if not second:
    stopped=threading.Event()
    watch_reload(stopped.set)
    if not stopped.wait(10):
        raise SystemExit(3)
    (root/'draining').touch()
    import time
    deadline=time.monotonic()+10
    while not (root/'release').exists():
        if time.monotonic()>deadline:
            raise SystemExit(4)
        time.sleep(.02)
""",encoding='utf-8')
            result=[]
            def run():
                result.append(run_development('reload_fixture',[]))
            def wait_for(path):
                deadline=time.monotonic()+8
                while not path.exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(path.exists(),str(path))
            with patch('repo_agent.infrastructure.reload.SOURCE_ROOT',root), \
                 patch('repo_agent.infrastructure.reload.SOURCE_PACKAGES',('repo_agent',)), \
                 patch.dict(os.environ,{'PYTHONPATH':str(root)+os.pathsep+str(Path.cwd()),'RELOAD_TEST_DIR':str(root)}):
                thread=threading.Thread(target=run)
                thread.start()
                try:
                    wait_for(root/'started-1')
                    watched.write_text('# updated code',encoding='utf-8')
                    wait_for(root/'draining')
                    self.assertFalse((root/'started-2').exists())
                    (root/'release').touch()
                    wait_for(root/'started-2')
                finally:
                    (root/'release').touch()
                    thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
                self.assertEqual([0],result)
