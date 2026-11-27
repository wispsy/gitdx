from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import gitdx
from gitdx import (IngestionProfile, ObjectFormat, PushCommand, ReceivePolicy, Repository,
                   apply_maintenance, plan_maintenance)
from gitdx.confined_fs import supported
from test_checkout_plan import commit_tree
import test_objects as oracle


class HostingPublicAPITests(unittest.TestCase):
    def test_selected_public_names_and_unqualified_default_report(self):
        selected = ('Repository', 'ObjectFormat', 'ObjectId', 'RawObject', 'StoreLimits', 'PackLimits',
                    'UploadPack', 'UploadPackLimits', 'ReceivePolicy', 'ReceiveLimits', 'ReceiveResult',
                    'ReceiveInterrupted', 'ReceiveRejected', 'ReceiveState', 'IngestionProfile',
                    'RefChange', 'RefTransactionResult', 'RefTransactionInterrupted', 'RefTransactionState',
                    'ReftableChange', 'ReftableWriteResult', 'ReftableWriteInterrupted',
                    'plan_maintenance', 'apply_maintenance', 'recover_maintenance', 'backup_repository',
                    'verify_backup', 'capability_report', 'QualificationEvidence')
        self.assertTrue(set(selected) <= set(gitdx.__all__))
        with patch('subprocess.Popen', side_effect=AssertionError('runtime helper')):
            report = gitdx.capability_report()
        self.assertIsNone(report['qualification'])
        selected = next(c for c in report['capabilities'] if c['id'] == 'hosting.api-v1')
        self.assertNotEqual(selected['state'], 'tested')


class HostingConcurrencyNativeTests(unittest.TestCase):
    setUpClass = classmethod(oracle.GitOracleTests.setUpClass.__func__)

    def test_receive_with_active_descriptor_intake_and_online_maintenance_preserves_native_graphs(self):
        for format in ObjectFormat:
            with self.subTest(format=format), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp);source = Repository.init(root/'source', bare=True, object_format=format)
                target = Repository.init(root/'target', bare=True, object_format=format)
                first = commit_tree(source, {b'file': b'old'})
                second = commit_tree(source, {b'file': b'new'}, parents=(first,))
                profile = IngestionProfile.UNTRUSTED if format is ObjectFormat.SHA256 and supported() else IngestionProfile.TRUSTED_LOCAL
                target.receive((PushCommand(b'refs/heads/main', None, first),), source.pack((first,)).pack,
                               policy=ReceivePolicy(ingestion=profile), coordinated=True)
                plan = plan_maintenance(target, grace_seconds=0)
                expected_first = source.objects.read(first)
                ready = threading.Event();resume = threading.Event();failures = [];results = []
                def guard(view, command):
                    if not ready.is_set():
                        ready.set()
                        if not resume.wait(10): raise RuntimeError('maintenance did not settle')
                    self.assertEqual(view.read(first), expected_first)
                def writer():
                    try:
                        results.append(target.receive((PushCommand(b'refs/heads/main', first, second),), source.pack((second,)).pack,
                            policy=ReceivePolicy(ingestion=profile, ref_check=guard), coordinated=True))
                    except BaseException as error: failures.append(error)
                worker = threading.Thread(target=writer);worker.start()
                try:
                    self.assertTrue(ready.wait(10))
                    result = apply_maintenance(target, plan, prune=True)
                    self.assertGreater(result.loose_pruned, 0)
                    # The active receive's policy can continue reading both old
                    # and new graphs after all planned loose copies disappeared.
                    self.assertEqual(target.objects.read(first), source.objects.read(first))
                finally:
                    resume.set();worker.join(10)
                self.assertFalse(worker.is_alive());self.assertEqual(failures, [])
                self.assertEqual(len(results), 1);self.assertTrue(results[0].complete)
                self.assertEqual(target.refs.resolve().oid, second)
                oracle.git(target.git_dir, 'fsck', '--strict', '--no-dangling')
