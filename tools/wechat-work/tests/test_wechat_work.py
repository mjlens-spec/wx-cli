import base64
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('wechat_work', Path(__file__).resolve().parents[1] / 'wechat_work.py')
ww = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ww)


def row(**updates):
    value = {'talker': 'sample@chatroom', 'server_id': 9223372036854775000, 'sort_seq': 901,
             'create_time': 1790200000, 'msg_type': 3, 'sender': 'sample',
             'content': {'Image': {'md5': 'a' * 32}}}
    value.update(updates)
    return value


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.account = self.root / 'account'
        (self.account / 'db_storage').mkdir(parents=True)
        self.reader = ww.Reader({'binary': '/nonexistent/wx-cli', 'account_dir': str(self.account)})

    def tearDown(self):
        self.temporary.cleanup()

    def test_redaction_preserves_business_values(self):
        source = 'ROI 0.57; aeskey="secret-value" token=other-value\n<msg><img aeskey="image-secret"/></msg>'
        result = ww.text_safe(source)
        self.assertIn('ROI 0.57', result)
        for secret in ('secret-value', 'other-value', 'image-secret'):
            self.assertNotIn(secret, result)

    def test_redaction_handles_quoted_json_values(self):
        result = ww.text_safe('ROI 0.57 {"token": "secret with spaces", "image_aes_key": "private"}')
        self.assertIn('ROI 0.57', result)
        self.assertNotIn('secret with spaces', result)
        self.assertNotIn('private', result)

    def test_quotes_keep_reply_and_hide_image_xml(self):
        value = row(content={'Quote': {'reply_text': '请修改这张表', 'refer_sender': '同事',
                                      'refer_content': '<?xml version="1.0"?><msg><img aeskey="secret"/></msg>',
                                      'raw_xml': 'must not escape'}})
        text = ww.message_text(value)
        self.assertIn('请修改这张表', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('must not escape', text)

    def test_large_ids_roundtrip_without_float_loss(self):
        value = row()
        normalized = self.reader.normalize(value)
        self.assertEqual(normalized['message_id'], '9223372036854775000')
        self.assertEqual(self.reader.unpack(normalized['image_ref'])['server_id'], normalized['message_id'])

    def test_image_ref_rejects_other_account(self):
        token = self.reader.reference(row())
        self.reader.account_tag = 'different'
        with self.assertRaisesRegex(ww.Failure, 'ACCOUNT_MISMATCH'):
            self.reader.unpack(token)

    def test_same_second_message_requires_exact_identifier(self):
        expected = row()
        ref = self.reader.reference(expected)
        different = row(server_id=9223372036854774999)
        with patch.object(self.reader, 'native', return_value={'items': [different]}):
            with self.assertRaisesRegex(ww.Failure, 'REFERENCE_NOT_FOUND'):
                self.reader.resolve_image_message(ref)
        with patch.object(self.reader, 'native', return_value={'items': [expected]}):
            result, digest = self.reader.resolve_image_message(ref)
            self.assertEqual(result['server_id'], expected['server_id'])
            self.assertEqual(digest, 'a' * 32)

    def test_incomplete_reference_is_rejected(self):
        data = {'v': 1, 'chat': 'test', 'server_id': '1', 'sort_seq': '1', 'timestamp': 1}
        token = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')
        with self.assertRaisesRegex(ww.Failure, 'REFERENCE_INVALID'):
            self.reader.unpack(token)

    def test_chat_cannot_be_interpreted_as_native_option(self):
        with self.assertRaisesRegex(ww.Failure, 'CHAT_INVALID'):
            self.reader.history('--show-hidden')

    def test_date_range_is_inclusive_and_validated(self):
        start, end, _ = ww.date_bounds('2026-09-24', '2026-09-24')
        self.assertEqual(dt.datetime.fromtimestamp(start).hour, 0)
        self.assertEqual(dt.datetime.fromtimestamp(end).strftime('%H:%M:%S'), '23:59:59')
        with self.assertRaisesRegex(ww.Failure, 'INVALID_DATE'):
            ww.date_bounds('2026-02-31', None)

    def test_pagination_and_chronological_output(self):
        values = [row(sort_seq=902, create_time=1790200001), row()]
        with patch.object(self.reader, 'native', return_value={'items': values, 'paging': {'has_more': True}}):
            result = self.reader.history('sample@chatroom', offset=20)
            self.assertTrue(result['paging']['has_more'])
            self.assertEqual(result['next_offset'], 22)
            self.assertLess(result['items'][0]['timestamp'], result['items'][1]['timestamp'])

    def test_mixed_conversations_are_not_combined(self):
        with patch.object(self.reader, 'native', return_value={'items': [row(), row(talker='other@chatroom')]}):
            with self.assertRaisesRegex(ww.Failure, 'CHAT_AMBIGUOUS'):
                self.reader.history('sample')

    def test_quality_priority_across_months(self):
        folder = self.account / 'msg/attach' / ww.hashlib.md5(b'sample@chatroom').hexdigest()
        for month, suffix in [('2026-09', '_t'), ('2026-08', '_h'), ('2026-09', '')]:
            p = folder / month / 'Img' / ('a' * 32 + suffix + '.dat')
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'synthetic')
        self.assertEqual([quality for _, quality in self.reader.candidates('sample@chatroom', 'a' * 32)],
                         ['cached_hd', 'cached_regular', 'thumbnail'])

    def test_broken_hd_falls_back_and_reports_actual_quality(self):
        folder = self.account / 'msg/attach' / ww.hashlib.md5(b'sample@chatroom').hexdigest() / '2026-09/Img'
        folder.mkdir(parents=True)
        for suffix in ('_h', ''):
            (folder / ('a' * 32 + suffix + '.dat')).write_bytes(b'synthetic')

        def decode(args, query=True):
            if args[2].endswith('_h.dat'):
                raise ww.Failure('IMAGE_DECODE_FAILED')
            Path(args[-1]).write_bytes(b'GIF89a' + b'synthetic')
            return ''

        with patch.object(self.reader, 'resolve_image_message', return_value=(row(), 'a' * 32)), \
             patch.object(self.reader, 'native', side_effect=decode), \
             patch.object(ww, 'invoke', return_value='pixelWidth: 800\npixelHeight: 600'):
            result = self.reader.image('ref', self.root / 'image')
        self.assertEqual(result['quality'], 'cached_regular')
        self.assertTrue(result['fallback_used'])
        self.assertEqual(result['skipped_candidate_errors'], ['IMAGE_DECODE_FAILED'])
        self.assertEqual(result['format'], 'gif')
        self.assertEqual(Path(result['path']).stat().st_mode & 0o777, 0o600)

    def test_tmp_alias_is_supported(self):
        with tempfile.TemporaryDirectory(prefix='wechat-work-test-', dir='/tmp') as folder:
            self.assertEqual(ww.private_dir(folder), Path(folder).resolve())

    def test_private_output_does_not_overwrite(self):
        p = self.root / 'output.png'
        ww.write_new(p, b'original')
        with self.assertRaisesRegex(ww.Failure, 'OUTPUT_EXISTS'):
            ww.write_new(p, b'replacement')
        self.assertEqual(p.read_bytes(), b'original')
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_private_read_rejects_symlink(self):
        actual = self.root / 'actual.json'
        ww.write_new(actual, b'{}')
        link = self.root / 'link.json'
        link.symlink_to(actual)
        with self.assertRaises(OSError):
            ww.private_read(link)

    def test_stderr_diagnostics_do_not_escape(self):
        backend = ww.subprocess.CompletedProcess([], 1, '', 'permission denied aeskey=never-return-this')
        with patch.object(ww.subprocess, 'run', return_value=backend):
            with self.assertRaisesRegex(ww.Failure, '^ACCESS_DENIED$'):
                ww.invoke(['backend'])

    def test_cleanup_requires_own_job(self):
        with self.assertRaisesRegex(ww.Failure, 'CLEANUP_TARGET_INVALID'):
            ww.cleanup_job(self.root)
        job = ww.new_job()
        ww.write_new(job / 'context.json', b'{}')
        self.assertTrue(ww.cleanup_job(job)['cleaned'])
        self.assertFalse(job.exists())

    def test_prepare_marks_partial_failure_and_no_analysis(self):
        ok = self.reader.envelope('history', [self.reader.normalize(row(msg_type=1, content={'Text': '待办'}))], {'has_more': False})
        with patch.object(self.reader, 'history', side_effect=[ok, ww.Failure('ACCESS_DENIED')]):
            result = ww.prepare(self.reader, ['one', 'two'], None, None, 10, 0)
        try:
            context = json.loads(ww.private_read(result['context_path']))
            self.assertEqual(result['failed_conversations'], 1)
            self.assertFalse(context['analysis_complete'])
            self.assertEqual(context['errors'][0]['code'], 'ACCESS_DENIED')
        finally:
            ww.cleanup_job(result['job'])


if __name__ == '__main__':
    unittest.main()
