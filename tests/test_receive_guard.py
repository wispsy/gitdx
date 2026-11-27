from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitdx import (ObjectFormat, PushCommand, ReceiveInterrupted, ReceivePolicy, ReceiveState, Repository)
from gitdx.refs import FilesRefStore
from test_checkout_plan import commit_tree


class ReceiveGuardTests(unittest.TestCase):
    def test_host_guard_precedes_ref_locks_and_spans_final_checks_and_publication(self):
        for format in ObjectFormat:
            for coordinated in (False, True):
                with self.subTest(format=format, coordinated=coordinated), tempfile.TemporaryDirectory() as tmp:
                    source = Repository.init(Path(tmp)/'source', bare=True, object_format=format)
                    target = Repository.init(Path(tmp)/'target', bare=True, object_format=format)
                    tip = commit_tree(source, {b'file': b'data'})
                    command = PushCommand(b'refs/heads/main', None, tip)
                    active = [];checks = []
                    @contextmanager
                    def publication(view, commands):
                        self.assertEqual(commands, (command,))
                        self.assertFalse((target.git_dir/'packed-refs.lock').exists())
                        self.assertEqual(view.read(tip), source.objects.read(tip))
                        active.append(True)
                        try: yield
                        finally:
                            self.assertEqual(target.refs.resolve().oid, tip)
                            active.pop()
                    def policy(view, selected):
                        checks.append((bool(active), (target.git_dir/'packed-refs.lock').exists()))
                    original = FilesRefStore._write
                    def write(store, *args, **options):
                        self.assertTrue(active)
                        return original(store, *args, **options)
                    with patch.object(FilesRefStore, '_write', write):
                        result = target.receive((command,), source.pack((tip,)).pack,
                            policy=ReceivePolicy(ref_check=policy, publication_guard=publication), coordinated=coordinated)
                    self.assertTrue(result.complete);self.assertFalse(active)
                    self.assertIn((False, False), checks)
                    if coordinated: self.assertIn((True, True), checks)

    def test_guard_refusal_exit_failure_and_cancellation_preserve_actual_outcomes(self):
        for mode in ('enter', 'exit', 'cancel'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                source = Repository.init(Path(tmp)/'source', bare=True)
                target = Repository.init(Path(tmp)/'target', bare=True)
                tip = commit_tree(source, {b'file': b'data'});command = PushCommand(b'refs/heads/main', None, tip)
                cancelled = threading.Event();closed = []
                @contextmanager
                def publication(view, commands):
                    if mode == 'enter': raise RuntimeError('host refused')
                    try:
                        if mode == 'cancel': cancelled.set()
                        yield
                    finally:
                        closed.append(True)
                        if mode == 'exit': raise RuntimeError('host commit failed')
                with self.assertRaises(ReceiveInterrupted) as caught:
                    target.receive((command,), source.pack((tip,)).pack,
                        policy=ReceivePolicy(publication_guard=publication), coordinated=True,
                        cancel=cancelled)
                result = caught.exception.result
                self.assertEqual(result.outcomes[0].state, ReceiveState.ACCEPTED if mode == 'exit' else ReceiveState.NOT_ATTEMPTED)
                self.assertEqual(target.refs.resolve().oid, tip if mode == 'exit' else None)
                if mode != 'enter': self.assertEqual(closed, [True])
                self.assertFalse((target.git_dir/'packed-refs.lock').exists())

    def test_invalid_host_guard_is_refused_without_implicit_execution(self):
        with self.assertRaises(TypeError): ReceivePolicy(publication_guard='configured-command')
