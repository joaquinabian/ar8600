"""Focused bank dwell/view/bulk-flag checks; no serial ports opened."""
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_current_bank_scan import Controller, main, header, membership, drain_pacing
from current_bank_scan import CurrentBankScan


VFO = 'VF RF0093500000 ST100000 AU0 MD0 AT0'


def channel_status(channel, skip=0):
    return 'MR MX%s MP%d RF0100300000 ST100000 AU0 MD0 AT1 TMStation' % (channel, skip)


def bulk_controller():
    ctrl = Controller()
    ctrl.connected = True
    ctrl.alive = SimpleNamespace(is_set=lambda: True)
    ctrl.memory_flag_edit = ctrl.memory_select_read = ctrl.memory_channel_pending = None
    ctrl.memory_flag_timer = Mock()
    ctrl.memory_rows = {channel: dict(select=False, skip=False,
                                    fields=[channel, '0', '0100300000', '100000', '0', '0', '1', 'Station'])
                        for channel in ('A00', 'A02', 'B01')}
    ctrl.selected_memory_bank = lambda: {'bank': 'A', 'channels': 10}
    ctrl.memory_flags_editable = lambda channel: ctrl.memory_flag_edit is None
    ctrl.show_memory_flags = Mock()
    ctrl.clear_memory_import_error = Mock()
    ctrl.monitor_muted = False
    for name in ('memory_bulk_channels', 'memory_bulk_flags_available', 'set_memory_bank_flags',
                 'next_bulk_memory_flag', 'on_bulk_memory_flag_status', 'on_memory_flag_status',
                 'finish_memory_flag_edit', 'read_memory_select_flags', 'release_monitor_mute',
                 'on_memory_flag_timeout'):
        setattr(ctrl, name, MethodType(getattr(main.AorCtrl, name), ctrl))
    ctrl.memory_flag_context_matches = MethodType(main.AorCtrl.memory_flag_context_matches, ctrl)
    return ctrl


class PassiveGroupChoiceTests(unittest.TestCase):
    def test_group_choice_does_not_select_scanner_group_or_send_queries(self):
        ctrl = SimpleNamespace(refresh_level_context=Mock(), update_operation_ui=Mock(),
                               write_serial=Mock(), request_group=Mock())
        main.AorCtrl.on_group_choice_changed(ctrl, None)
        ctrl.refresh_level_context.assert_called_once_with(query=False)
        ctrl.update_operation_ui.assert_called_once()
        ctrl.write_serial.assert_not_called()
        ctrl.request_group.assert_not_called()

    def test_source_selection_does_not_restore_or_query_group(self):
        ctrl = Controller()
        ctrl.connected = True
        ctrl.discard_pending_vfo_target = Mock()
        ctrl.request_group = Mock()
        ctrl.refresh_level_context = Mock()
        ctrl.restore_temporary_scan = Mock()
        ctrl.operation_selection = lambda: ('Frequency Search', 'Search Group')
        main.AorCtrl.on_source_changed(ctrl, None)
        ctrl.request_group.assert_not_called()
        ctrl.refresh_level_context.assert_called_once_with(query=False)
        ctrl.temporary_scan = SimpleNamespace(phase='active')
        main.AorCtrl.on_source_changed(ctrl, None)
        main.AorCtrl.on_memory_source_changed(ctrl, None)
        ctrl.restore_temporary_scan.assert_not_called()
        self.assertEqual(ctrl.commands, [])

    def test_explicit_start_reads_selected_group_then_continues_after_membership(self):
        for kind, operation, source, group in (('scan', 'Memory Scan', 'Scan Group', 2),
                                               ('search', 'Frequency Search', 'Search Group', 3)):
            ctrl = SimpleNamespace(connected=True, serial=SimpleNamespace(is_open=True),
                                   active_operation=None, memory_channel_pending=None,
                                   operation_selection=lambda: (operation, source),
                                   cbx_scan=Mock(), cbx_search=Mock(), aor_status=Mock(),
                                   group_loading={'scan': None, 'search': None},
                                   group_response={'scan': None, 'search': None},
                                   groups={'scan': {}, 'search': {}}, request_group=Mock())
            control = ctrl.cbx_scan if kind == 'scan' else ctrl.cbx_search
            control.GetValue.return_value = str(group)
            main.AorCtrl.on_operation_start(ctrl, None)
            ctrl.request_group.assert_called_once_with(kind)
            self.assertEqual(ctrl.pending_group_start, (operation, source, kind, group))
            ctrl.group_response[kind] = ctrl.group_loading[kind] = group
            ctrl.settings_dialog = None
            ctrl.group_dialogs = {'scan': None, 'search': None}
            ctrl.update_connection_ui = Mock()
            ctrl.on_operation_start = Mock()
            line = membership('A') if kind == 'scan' else 'BS A' + '-' * 39
            main.AorCtrl.set_group_response(ctrl, line)
            ctrl.on_operation_start.assert_called_once_with(None)
            self.assertIsNone(ctrl.pending_group_start)


class BulkFlagsTests(unittest.TestCase):
    def test_selected_capture_and_restore_once_then_verify_complete_gr(self):
        ctrl = bulk_controller()
        ctrl.set_memory_bank_flags('select', True)
        ctrl.on_memory_flag_status(VFO)
        ctrl.on_memory_flag_status(channel_status('A00'))
        ctrl.on_memory_flag_status(channel_status('A02'))
        self.assertEqual(ctrl.commands, [b'RX\r\n', b'MC1\r\nMRA00\r\nRX\r\n',
                                         b'GA1\r\nMRA02\r\nRX\r\n', b'GA1\r\nVF\r\nMC0\r\n',
                                         b'GR\r\nRX\r\n'])
        self.assertNotIn(b'MRB01', b''.join(ctrl.commands))
        self.assertNotIn(b'MRA01', b''.join(ctrl.commands))
        self.assertFalse(ctrl.monitor_muted)
        ctrl.memory_select_read.update(channels={'A00', 'A02'}, slots={0: 'A00', 1: 'A02'}, seen=True)
        ctrl.on_memory_flag_status(VFO)
        self.assertIsNone(ctrl.memory_flag_edit)
        self.assertTrue(ctrl.memory_rows['A00']['select'])
        self.assertTrue(ctrl.memory_rows['A02']['select'])
        ctrl.aor_status.SetStatusText.assert_called_with('Bulk Selected saved and verified; receiver restored.')

    def test_deselect_and_clear_skip_commands(self):
        for flag, token in (('select', b'GA0'), ('skip', b'MP0')):
            ctrl = bulk_controller()
            ctrl.memory_rows['A00'][flag] = True
            ctrl.memory_rows['A02'][flag] = True
            ctrl.set_memory_bank_flags(flag, False)
            ctrl.on_memory_flag_status(VFO)
            ctrl.on_memory_flag_status(channel_status('A00', skip=1 if flag == 'skip' else 0))
            self.assertIn(token + b'\r\n', ctrl.commands[-1])

    def test_skip_verifies_each_populated_channel_and_restores_once(self):
        ctrl = bulk_controller()
        ctrl.set_memory_bank_flags('skip', True)
        ctrl.on_memory_flag_status(VFO)
        for channel in ('A00', 'A02'):
            ctrl.on_memory_flag_status(channel_status(channel))
            ctrl.on_memory_flag_status(channel_status(channel, 1))
        self.assertEqual(ctrl.commands, [b'RX\r\n', b'MC1\r\nMRA00\r\nRX\r\n',
                                         b'MP1\r\nRX\r\n', b'MRA02\r\nRX\r\n',
                                         b'MP1\r\nRX\r\n', b'VF\r\nMC0\r\nRX\r\n'])
        ctrl.on_memory_flag_status(VFO)
        self.assertIsNone(ctrl.memory_flag_edit)
        ctrl.aor_status.SetStatusText.assert_called_with('Bulk Skip saved and verified; receiver restored.')

    def test_skip_mismatch_aborts_and_restores_without_updating_next_channel(self):
        ctrl = bulk_controller()
        ctrl.set_memory_bank_flags('skip', True)
        ctrl.on_memory_flag_status(VFO)
        ctrl.on_memory_flag_status(channel_status('A00'))
        ctrl.on_memory_flag_status(channel_status('A00'))
        self.assertIsNone(ctrl.memory_flag_edit)
        self.assertFalse(ctrl.monitor_muted)
        self.assertNotIn(b'MRA02', b''.join(ctrl.commands))
        self.assertEqual(ctrl.commands[-3:], [b'VF\r\n', b'MC0\r\n', b'RX\r\n'])

    def test_original_memory_context_restores_with_intentionally_changed_skip(self):
        ctrl = bulk_controller()
        ctrl.set_memory_bank_flags('skip', True)
        ctrl.on_memory_flag_status(channel_status('A00'))
        for channel in ('A00', 'A02'):
            ctrl.on_memory_flag_status(channel_status(channel))
            ctrl.on_memory_flag_status(channel_status(channel, 1))
        self.assertEqual(ctrl.commands[-1], b'MRA00\r\nMC0\r\nRX\r\n')
        ctrl.on_memory_flag_status(channel_status('A00', 1))
        self.assertIsNone(ctrl.memory_flag_edit)
        ctrl.aor_status.SetStatusText.assert_called_with('Bulk Skip saved and verified; receiver restored.')

    def test_selected_mismatch_is_reported_and_scanner_values_retained(self):
        ctrl = bulk_controller()
        ctrl.set_memory_bank_flags('select', True)
        ctrl.on_memory_flag_status(VFO)
        ctrl.on_memory_flag_status(channel_status('A00'))
        ctrl.on_memory_flag_status(channel_status('A02'))
        ctrl.memory_select_read.update(channels={'A00'}, slots={0: 'A00'}, seen=True)
        ctrl.on_memory_flag_status(VFO)
        self.assertFalse(ctrl.memory_rows['A02']['select'])
        self.assertIn('verification failed', ctrl.aor_status.SetStatusText.call_args.args[0])

    def test_bulk_timeout_releases_audio_and_disabled_actions_send_nothing(self):
        ctrl = bulk_controller()
        ctrl.set_memory_bank_flags('select', True)
        ctrl.on_memory_flag_status(VFO)
        ctrl.set_memory_bank_flags('skip', True)
        self.assertEqual(len(ctrl.commands), 2)
        ctrl.on_memory_flag_timeout(None)
        self.assertFalse(ctrl.monitor_muted)
        self.assertIsNone(ctrl.memory_flag_edit)
        self.assertEqual(ctrl.commands[-3:], [b'VF\r\n', b'MC0\r\n', b'RX\r\n'])
        self.assertIsNone(ctrl.memory_rows['A00']['select'])

    def test_prepared_scan_or_open_channel_editor_disables_bulk_actions(self):
        ctrl = bulk_controller()
        ctrl.temporary_scan = Mock()
        ctrl.set_memory_bank_flags('select', True)
        self.assertEqual(ctrl.commands, [])
        ctrl.temporary_scan = None
        ctrl.memory_channel_dialog = Mock()
        ctrl.set_memory_bank_flags('skip', True)
        self.assertEqual(ctrl.commands, [])


class DwellAndViewTests(unittest.TestCase):
    def configure(self, ctrl, target='J', borrowed_dwell=60):
        scan = CurrentBankScan(ctrl, ctrl.memory_banks[target])
        ctrl.temporary_scan = scan
        scan.start()
        scan.receive(header(4))
        scan.receive(membership('BCD'))
        scan.receive(header(1, dwell=borrowed_dwell))
        scan.receive(membership(''))
        drain_pacing(scan)
        return scan

    def test_per_bank_dwell_overrides_borrowed_group_but_original_is_restored(self):
        ctrl = Controller()
        ctrl.memory_bank_dwell = {'J': 40, 'B': 15}
        scan = self.configure(ctrl)
        self.assertEqual(ctrl.commands[-2:], [b'XP40\r\n', b'GM\r\n'])
        scan.receive('GM1 XD2.0 XB 012 XA 000 XP40 XMF')
        scan.receive(membership('J'))
        scan.receive('MS MXJ02 MP0 RF0446500000 ST012500 AU0 MD1 AT0 TMPMR')
        scan.restore()
        drain_pacing(scan)
        self.assertEqual(ctrl.commands[-2:], [b'XP60\r\n', b'GM\r\n'])
        scan.receive(header(1))
        scan.receive(membership(''))
        drain_pacing(scan)
        scan.receive(header(4))
        scan.receive(membership('BCD'))
        self.assertEqual(ctrl.memory_bank_dwell, {'J': 40, 'B': 15})
        self.configure(ctrl, borrowed_dwell=9)
        self.assertEqual(ctrl.commands[-2:], [b'XP40\r\n', b'GM\r\n'])

    def test_first_use_dwell_is_independent_of_borrowed_group(self):
        ctrl = Controller()
        ctrl.memory_bank_dwell = {}
        scan = self.configure(ctrl, borrowed_dwell=20)
        self.assertEqual(scan.profile['XP'], 0)
        self.assertEqual(ctrl.memory_bank_dwell, {})
        self.configure(ctrl, borrowed_dwell=60)
        self.assertEqual(ctrl.commands[-2:], [b'XP00\r\n', b'GM\r\n'])

    def test_switching_sources_loads_operation_bank_and_selected_list_independently(self):
        ctrl = Controller()
        ctrl.connected = True
        ctrl.discard_pending_vfo_target = Mock()
        ctrl.select_list_view = Mock()
        ctrl.on_select_list = Mock()
        ctrl.on_memory_source_changed = MethodType(main.AorCtrl.on_memory_source_changed, ctrl)
        bank = ctrl.memory_banks['B']
        ctrl.operation_memory_bank = lambda: bank
        ctrl.cbx_lists = Mock()
        ctrl.cbx_lists.GetCount.return_value = 3
        ctrl.cbx_lists.GetClientData.side_effect = [None, ctrl.memory_banks['A'], bank]
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        main.AorCtrl.on_source_changed(ctrl, None)
        ctrl.cbx_lists.SetSelection.assert_called_once_with(2)
        ctrl.on_select_list.assert_called_once_with(None)
        ctrl.operation_selection = lambda: ('Memory Scan', 'Select Scan')
        main.AorCtrl.on_source_changed(ctrl, None)
        ctrl.select_list_view.assert_called_once_with('SELECT SCAN')
        self.assertEqual(ctrl.on_select_list.call_count, 2)
        ctrl.cbx_lists.GetClientData.side_effect = [None, ctrl.memory_banks['A'], bank]
        ctrl.operation_selection = lambda: ('Memory Scan', 'Current Bank')
        main.AorCtrl.on_source_changed(ctrl, None)
        self.assertEqual(ctrl.on_select_list.call_count, 3)


if __name__ == '__main__':
    unittest.main()
