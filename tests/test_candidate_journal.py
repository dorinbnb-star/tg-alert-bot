import csv
from dataclasses import asdict
from datetime import datetime
import io
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from ai_crypto_monitor import candidate_journal as cj, candidate_monitor as cm
from tests.test_candidate_monitor import C, bar
from tests.test_workflow import monitor_steps, LIVE_PATH, DRY_STEP


def ms(value):
    return int(datetime.fromisoformat(value).timestamp()*1000)


def csv_bytes(rows):
    stream=io.StringIO(newline='')
    writer=csv.DictWriter(stream,fieldnames=cm.CSV_FIELDS)
    writer.writeheader()
    for row in rows: writer.writerow(row)
    return stream.getvalue().encode()


def row(date, status='SENT', coin='BTC', outcome='', r=''):
    return dict(zip(('data','oră','status','monedă','rezultat paper','R paper'),
                    (date[:10],date[11:],status,coin,outcome,r)))


class WeeklyTests(unittest.TestCase):
    def test_exact_message_counts_journal_events_and_as_of_open_positions(self):
        rows=[row('2026-11-01 19:00'), row('2026-11-01 20:00'),
              row('2026-11-02 10:00',coin='ETH'), row('2026-11-03 08:00','MISSED_STALE'),
              row('2026-11-03 12:00','PAPER',outcome='2R ATINS',r='2.000000'),
              row('2026-11-04 12:00','PAPER',outcome='SL ATINS',r='-1.200000'),
              row('2026-11-05 12:00','PAPER',outcome='TIMEOUT',r='0.300000'),
              row('2026-11-08 20:00',coin='ETH')]
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/cj.JOURNAL; path.write_bytes(csv_bytes(rows))
            self.assertEqual('📋 RAPORT SĂPTĂMÂNAL (01.11.2026–08.11.2026)\n'
                'Candidați trimiși: 2 (BTC 1, ETH 1) | ratați noaptea (MISSED_STALE): 1\n'
                'Rezultate paper închise: 2R atins 1 | SL 1 | timeout 1 | R total paper: +1.100000R\n'
                'Candidați încă deschiși (paper): 1',cj.weekly_message(path,ms('2026-11-08T20:07:00+01:00')))

    def test_empty_week_sends_zero_report_and_full_document(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp); journal=directory/cj.JOURNAL
            journal.write_bytes(csv_bytes([])); sent=[]
            self.assertTrue(cj.weekly_delivery(directory,ms('2026-11-08T20:01:00+01:00'),lambda p,t:sent.append((p.read_bytes(),t))))
            self.assertEqual(csv_bytes([]),sent[0][0])
            self.assertIn('Candidați trimiși: 0 (BTC 0, ETH 0)',sent[0][1])
            self.assertIn('2R atins 0 | SL 0 | timeout 0 | R total paper: +0.000000R',sent[0][1])

    def test_dst_dates_only_sunday_after_20_once_per_week_after_reload(self):
        for date,offset in [('2026-03-29','+02:00'),('2026-10-25','+01:00')]:
            with self.subTest(date=date),tempfile.TemporaryDirectory() as temp:
                directory=Path(temp); (directory/cj.JOURNAL).write_bytes(csv_bytes([])); send=Mock()
                self.assertFalse(cj.weekly_delivery(directory,ms(date+'T19:59:59'+offset),send))
                self.assertTrue(cj.weekly_delivery(directory,ms(date+'T20:00:00'+offset),send))
                self.assertFalse(cj.weekly_delivery(directory,ms(date+'T21:30:00'+offset),send))
                self.assertEqual(1,send.call_count)
                self.assertIn(date,json.loads((directory/cj.REPORT_STATE).read_text())['sent_weeks'])
        with tempfile.TemporaryDirectory() as temp:
            send=Mock()
            for stamp in ('2026-11-07T21:00:00+01:00','2026-11-09T21:00:00+01:00'):
                self.assertFalse(cj.weekly_delivery(Path(temp),ms(stamp),send))
            send.assert_not_called()

    def test_calendar_week_spans_167_and_169_real_hours(self):
        for stamp,hours in [('2026-03-29T20:00:00+02:00',167),('2026-10-25T20:00:00+01:00',169)]:
            start,end=cj.week_window(ms(stamp))
            self.assertEqual(hours,(end.timestamp()-start.timestamp())/3600)

    def test_next_week_and_failed_delivery_do_not_mark_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp); (directory/cj.JOURNAL).write_bytes(csv_bytes([]))
            with self.assertRaises(cm.monitor.TelegramError):
                cj.weekly_delivery(directory,ms('2026-11-08T20:00:00+01:00'),Mock(side_effect=cm.monitor.TelegramError('offline')))
            self.assertFalse((directory/cj.REPORT_STATE).exists())
            send=Mock()
            for date in ('2026-11-08','2026-11-15'):
                self.assertTrue(cj.weekly_delivery(directory,ms(date+'T20:00:00+01:00'),send))
            self.assertEqual(2,send.call_count)

    def test_preview_never_reads_secrets_or_calls_network(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cm.monitor,'credentials',side_effect=AssertionError),patch.object(cj.urllib.request,'urlopen',side_effect=AssertionError),patch('builtins.print') as output:
            path=Path(temp)/cj.JOURNAL; path.write_bytes(csv_bytes([]))
            self.assertEqual(0,cj.main(['--journal',str(path),'--as-of','2026-11-08T20:07:00+01:00']))
            self.assertIn('RAPORT SĂPTĂMÂNAL',output.call_args.args[0])

    def test_multipart_contains_exact_csv_and_caption(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/cj.JOURNAL; path.write_bytes(csv_bytes([]))
            response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False)
            response.read.return_value=b'{"ok":true,"result":{"message_id":123}}'
            with patch.object(cj.urllib.request,'urlopen',return_value=response) as opening:
                self.assertEqual(123,cj.send_document('test-token','test-chat',path,'raport')['message_id'])
                request=opening.call_args.args[0]
                self.assertTrue(request.full_url.endswith('/sendDocument'))
                self.assertIn(path.read_bytes(),request.data)
                self.assertIn(b'name="caption"\r\n\r\nraport',request.data)

    def test_upload_errors_do_not_leak_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/cj.JOURNAL; path.write_bytes(csv_bytes([]))
            with patch.object(cj.urllib.request,'urlopen',side_effect=OSError('test-token test-chat')):
                with self.assertRaises(cm.monitor.TelegramError) as caught: cj.send_document('test-token','test-chat',path,'report')
                self.assertNotIn('test-token',str(caught.exception))
                self.assertNotIn('test-chat',str(caught.exception))


class RecoveryTests(unittest.TestCase):
    def bundle(self):
        state={C.key:{'candidate':asdict(C),'status':'SENT'}}
        with tempfile.TemporaryDirectory() as temp:
            journal=Path(temp)/cj.JOURNAL; cm.write_journal(journal,state)
            return {cj.JOURNAL:journal.read_bytes(),cj.DEDUP:json.dumps(state).encode(),
                    cj.REPORT_STATE:b'{"sent_weeks":["2026-11-01"]}'}

    def test_complete_recovery_preserves_dedup_and_weekly_history(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cj,'latest_bundle',return_value=self.bundle()),patch('builtins.print') as out:
            directory=Path(temp)
            cm.monitor.write_json(directory/cj.REPORT_STATE,{'sent_weeks':['2026-11-08']})
            self.assertEqual('RECOVERED_FROM_ARTIFACT',cj.prepare_live(directory,'repo','token'))
            self.assertIn(C.key,cm.monitor.read_json(directory/cj.DEDUP))
            self.assertEqual(['2026-11-01','2026-11-08'],cm.monitor.read_json(directory/cj.REPORT_STATE)['sent_weeks'])
            out.assert_any_call('RECOVERED_FROM_ARTIFACT')
            sent=[]; cj.notify_reset(directory,sent.append); self.assertEqual([],sent)

    def test_unavailable_backup_resets_and_notifies_once_across_runs(self):
        for backup in ({},OSError('unavailable')):
            with tempfile.TemporaryDirectory() as temp,patch.object(cj,'latest_bundle',side_effect=backup if isinstance(backup,Exception) else None,return_value=backup),patch('builtins.print') as out:
                directory=Path(temp); sent=[]
                self.assertEqual('JOURNAL_RESET',cj.prepare_live(directory,'repo','token'))
                cj.notify_reset(directory,sent.append)
                self.assertEqual('CACHE',cj.prepare_live(directory,'repo','token'))
                cj.notify_reset(directory,sent.append)
                self.assertEqual([cj.RESET_MESSAGE],sent)
                self.assertEqual({},cm.monitor.read_json(directory/cj.DEDUP))
                out.assert_any_call('JOURNAL_RESET')

    def test_reset_warning_failure_is_retried_without_marking_sent(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp); cm.monitor.write_json(directory/cj.REPORT_STATE,{'reset_pending':True})
            with self.assertRaises(cm.monitor.TelegramError): cj.notify_reset(directory,Mock(side_effect=cm.monitor.TelegramError('offline')))
            self.assertTrue(cm.monitor.read_json(directory/cj.REPORT_STATE)['reset_pending'])
            send=Mock(); cj.notify_reset(directory,send); cj.notify_reset(directory,send); send.assert_called_once_with(cj.RESET_MESSAGE)

    def test_journal_without_dedup_never_recovers_or_sends(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cj,'latest_bundle',side_effect=AssertionError):
            directory=Path(temp); (directory/cj.JOURNAL).write_bytes(csv_bytes([]))
            with self.assertRaisesRegex(cm.monitor.MonitorError,'Telegram oprit'): cj.prepare_live(directory,'repo','token')
            self.assertFalse((directory/cj.DEDUP).exists())

    def test_old_csv_only_artifact_stops_instead_of_fabricating_dedup(self):
        bundle=self.bundle(); del bundle[cj.DEDUP]
        with tempfile.TemporaryDirectory() as temp,patch.object(cj,'latest_bundle',return_value=bundle):
            directory=Path(temp)
            with self.assertRaisesRegex(cm.monitor.MonitorError,'jurnal fără dedup'): cj.prepare_live(directory,'repo','token')
            self.assertTrue((directory/cj.JOURNAL).exists())
            self.assertFalse((directory/cj.DEDUP).exists())

    def test_inconsistent_backup_is_rejected(self):
        bundle=self.bundle(); bundle[cj.DEDUP]=b'{}'
        with self.assertRaisesRegex(ValueError,'disagree'): cj.validate_bundle(bundle)

    def test_missing_report_cache_restored_without_touching_primary_state(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cj,'latest_bundle',return_value=self.bundle()):
            directory=Path(temp); cm.monitor.write_json(directory/cj.DEDUP,{})
            self.assertEqual('CACHE',cj.prepare_live(directory,'repo','token'))
            self.assertEqual({},cm.monitor.read_json(directory/cj.DEDUP))
            self.assertEqual(['2026-11-01'],cm.monitor.read_json(directory/cj.REPORT_STATE)['sent_weeks'])

    def test_latest_main_artifact_pagination_and_no_path_extraction(self):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as z:
            for name,value in self.bundle().items(): z.writestr('../../'+name,value)
        response=Mock(); response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=False); response.read.return_value=raw.getvalue()
        unrelated={'name':'candidate-journal-branch','workflow_run':{'head_branch':'other'},'created_at':'2026-11-05','id':1}
        match={'name':'candidate-journal-main','workflow_run':{'head_branch':'main'},'created_at':'2026-11-04','id':42}
        with patch.object(cj,'_request_json',side_effect=[{'artifacts':[unrelated]*100},{'artifacts':[match]}]) as listing,patch.object(cj.urllib.request,'urlopen',return_value=response) as opening:
            bundle=cj.latest_bundle('owner/repo','token')
            self.assertEqual(2,listing.call_count)
            self.assertIn('/artifacts/42/zip',opening.call_args.args[0].full_url)
            self.assertEqual(set(self.bundle()),set(bundle))


class IntegrationTests(unittest.TestCase):
    def test_live_scan_reset_and_weekly_document_only_once(self):
        env={'GITHUB_ACTIONS':'true','GITHUB_REF':'refs/heads/main','GITHUB_RUN_ATTEMPT':'1','GITHUB_EVENT_NAME':'schedule'}
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,env),patch.object(cj,'latest_bundle',return_value={}),patch.object(cm.monitor,'credentials',return_value=('token','chat')),patch.object(cm.monitor,'require_delivery_identity') as identity,patch.object(cm.monitor,'telegram_call') as telegram,patch.object(cj,'send_document') as document,patch.object(cm.monitor,'OkxClient') as client,patch.object(cm.PublicHistory,'candles',return_value=[bar(22)]),patch.object(cm,'detect',return_value=[]),patch('builtins.print'):
            root=Path(temp)
            cm.monitor.write_json(cm.monitor.package_dir(root)/'config-v0.1.json',{})
            client.return_value.server_time_ms.return_value=ms('2026-11-08T20:07:00+01:00')
            client.return_value.tick_sizes.return_value=dict.fromkeys(cm.SYMBOLS,'0.01')
            self.assertEqual(0,cm.main(['--root',temp,'--send']))
            self.assertEqual(0,cm.main(['--root',temp,'--send']))
            telegram.assert_called_once_with('token','sendMessage',{'chat_id':'chat','text':cj.RESET_MESSAGE})
            self.assertEqual(1,document.call_count)
            self.assertEqual(1,identity.call_count)
            self.assertIn('Candidați trimiși: 0',document.call_args.args[3])

    def test_dry_scan_never_recovers_or_uploads(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(cj,'prepare_live',side_effect=AssertionError),patch.object(cj,'send_document',side_effect=AssertionError),patch.object(cm.monitor,'credentials',side_effect=AssertionError),patch.object(cm.monitor,'OkxClient') as client,patch.object(cm.PublicHistory,'candles',return_value=[bar(22)]),patch.object(cm,'detect',return_value=[]),patch('builtins.print'):
            client.return_value.server_time_ms.return_value=23*cm.H
            client.return_value.tick_sizes.return_value=dict.fromkeys(cm.SYMBOLS,'0.01')
            self.assertEqual(0,cm.main(['--root',temp,'--dry-run']))

    def test_weekly_state_cache_is_live_only_and_dry_has_no_credentials(self):
        steps=monitor_steps()
        self.assertIn(LIVE_PATH,steps['Restore weekly report state'])
        self.assertIn("steps.live.outcome != 'skipped'",steps['Save weekly report state'])
        self.assertIn('github.run_attempt',steps['Save weekly report state'])
        for forbidden in ('GITHUB_TOKEN','secrets.','TELEGRAM'):
            self.assertNotIn(forbidden,steps[DRY_STEP])
        self.assertIn("steps.live.outcome != 'skipped'",steps['Upload candidate journal'])
        self.assertIn('candidate-preview-',steps['Upload dry-run candidate journal'])


if __name__=='__main__': unittest.main()
