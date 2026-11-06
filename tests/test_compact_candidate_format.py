import csv
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from ai_crypto_monitor import candidate_monitor as cm
from tests.test_candidate_monitor import C, bar


def timestamp(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


class CompactFormatTests(unittest.TestCase):
    def test_exact_short_with_instrument_precision(self):
        c = replace(C, symbol='BTC-USDT-SWAP', sign=-1, entry=85000.1,
                    stop=85100.1, tp=84800.1, tick='0.1')
        self.assertEqual('🔴 BTC SHORT\n\nEntry teoretic: 85000.1\n'
                         'Preț verificat: 85001.2\nSL: 85100.1\nTP: 84800.1\n'
                         'R:R: 2.00\nValabil până la: 01:00\nStatus: PENDING',
                         cm.candidate_message(c, 85001.24))

    def test_validity_two_elapsed_hours_through_spring_and_autumn_dst(self):
        for confirmation, expected in (
                ('2026-03-29T01:00:00+01:00', '04:00'),
                ('2026-10-25T01:00:00+02:00', '02:00'),
                ('2026-11-06T11:00:00+01:00', '13:00')):
            c = replace(C, confirmation_ms=timestamp(confirmation))
            self.assertIn('Valabil până la: ' + expected, cm.candidate_message(c, 100))

    def test_exact_paper_messages_and_gap_result_not_changed(self):
        for outcome, r, expected in (
                ('2R ATINS', 2, '✅ ETH LONG: TP HIT (+2.00R)'),
                ('SL ATINS', -1, '❌ ETH LONG: SL HIT (−1R)'),
                ('TIMEOUT', .37, '⏱ ETH LONG: ÎNCHIS LA TIMEOUT (+0.37R)'),
                ('TIMEOUT', -.37, '⏱ ETH LONG: ÎNCHIS LA TIMEOUT (−0.37R)'),
                ('SL ATINS', -1.25, '❌ ETH LONG: SL HIT (−1.25R)')):
            self.assertEqual(expected, cm.paper_message(C, (23*cm.H, outcome, r)))
        self.assertEqual('✅ ETH SHORT: TP HIT (+2.00R)',
                         cm.paper_message(replace(C, sign=-1), (23*cm.H, '2R ATINS', 2)))

    def test_display_rr_is_two_r_target_not_structural_target(self):
        self.assertEqual(3, C.rr)
        self.assertIn('R:R: 2.00', cm.candidate_message(C, 50))
        self.assertEqual(3, C.rr)
        c = replace(C, tp=110.01)
        self.assertIn('R:R: 2.00', cm.candidate_message(c, 50))
        self.assertIn('R:R: 2.01', cm.candidate_message(replace(c, tp=110.04), 50))

    def test_fresh_ticker_read_only_for_new_nonstale_candidate(self):
        events, state = [], {}
        def quote(symbol):
            events.append(('quote', symbol))
            return 101.23
        def send(message): events.append(('send', message))
        def persist(): events.append(('persist',))
        cm.update(C.symbol, [C], [], C.confirmation_ms, state, persist, send, price_reader=quote)
        self.assertEqual(['quote', 'send', 'persist'], [event[0] for event in events])
        self.assertIn('Preț verificat: 101.23', events[1][1])
        no_quote = Mock(side_effect=AssertionError('dedup/stale cannot fetch ticker'))
        cm.update(C.symbol, [C], [], C.confirmation_ms, state, persist, send, price_reader=no_quote)
        cm.update(C.symbol, [C], [], C.confirmation_ms+2*cm.H+1, {}, persist, send, price_reader=no_quote)
        no_quote.assert_not_called()

    def test_price_failure_does_not_change_eligibility_or_paper_state(self):
        for reader in (Mock(side_effect=cm.monitor.MonitorError('offline')),
                       Mock(return_value=float('nan')), Mock(return_value=0)):
            state, sent = {}, []
            cm.update(C.symbol, [C], [], C.confirmation_ms, state, lambda: None, sent.append, price_reader=reader)
            self.assertIn('Preț verificat: indisponibil', sent[0])
            self.assertEqual({C.key: {'candidate': asdict(C), 'status': 'SENT'}}, state)

    def test_ticker_does_not_change_dedup_results_or_csv(self):
        journals = []
        for quote in (1, 101.23, 99999):
            state, sent = {}, []
            cm.update(C.symbol, [C], [bar(22, high=111)], 23*cm.H,
                      state, lambda: None, sent.append, price_reader=lambda symbol: quote)
            self.assertEqual({C.key: {'candidate': asdict(C), 'status': 'SENT',
                                      'result': (23*cm.H, '2R ATINS', 2)}}, state)
            self.assertEqual('✅ ETH LONG: TP HIT (+2.00R)', sent[-1])
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'journal.csv'
                cm.write_journal(path, state)
                journals.append(path.read_bytes())
                with path.open(encoding='utf-8', newline='') as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(['SENT','PAPER'], [r['status'] for r in rows])
                self.assertEqual(['3.00','3.00'], [r['R:R'] for r in rows])
                self.assertEqual('2.000000', rows[-1]['R paper'])
                self.assertEqual('102.00', rows[0]['intrare de referință'])
                self.assertEqual(C.daily, rows[0]['trend Daily'])
                self.assertEqual('100.00', rows[0]['nivel-cheie'])
                self.assertEqual('114.00', rows[0]['TP structural'])
        self.assertEqual(journals[0], journals[1])
        self.assertEqual(journals[1], journals[2])

    def test_rejected_telegram_does_not_mark_dedup(self):
        state = {}
        with self.assertRaises(cm.monitor.TelegramError):
            cm.update(C.symbol, [C], [], C.confirmation_ms, state, lambda: None,
                      Mock(side_effect=cm.monitor.TelegramError('offline')), price_reader=lambda symbol: 100)
        self.assertEqual({}, state)


if __name__ == '__main__':
    unittest.main()
