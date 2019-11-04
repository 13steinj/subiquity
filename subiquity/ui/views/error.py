# Copyright 2019 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging

from urwid import (
    connect_signal,
    disconnect_signal,
    Padding,
    Text,
    )

from subiquitycore.ui.buttons import other_btn
from subiquitycore.ui.container import (
    Pile,
    )
from subiquitycore.ui.spinner import Spinner
from subiquitycore.ui.stretchy import Stretchy
from subiquitycore.ui.table import (
    ColSpec,
    TablePile,
    TableRow,
    )
from subiquitycore.ui.utils import (
    button_pile,
    ClickableIcon,
    Color,
    rewrap,
    )
from subiquitycore.ui.width import (
    widget_width,
    )

from subiquity.controllers.error import (
    ErrorReportKind,
    ErrorReportState,
    )


log = logging.getLogger('subiquity.ui.error')


def close_btn(parent, label=None):
    if label is None:
        label = _("Close")
    return other_btn(label, on_press=lambda sender: parent.remove_overlay())


error_report_intros = {
    ErrorReportKind.BLOCK_PROBE_FAIL: _("""
Sorry, there was a problem examining the storage devices on this system.
"""),
    ErrorReportKind.DISK_PROBE_FAIL: _("""
Sorry, there was a problem examining the storage devices on this system.
"""),
    ErrorReportKind.INSTALL_FAIL: _("""
Sorry, there was a problem completing the installation.
"""),
    ErrorReportKind.UI: _("""
Sorry, the installer has restarted because of an error.
"""),
    ErrorReportKind.UNKNOWN: _("""
Sorry, an unknown error occurred.
"""),
}

error_report_state_descriptions = {
    ErrorReportState.INCOMPLETE: (_("""
Information is being collected from the system that will help the
developers diagnose the report.
"""), True),
    ErrorReportState.LOADING: (_("""
Loading report...
"""), True),
    ErrorReportState.ERROR_GENERATING: (_("""
Collecting information from the system failed. See the files in
/var/log/installer for more.
"""), False),
    ErrorReportState.ERROR_LOADING: (_("""
Loading the report failed. See the files in /var/log/installer for more.
"""), False),
}

error_report_options = {
    ErrorReportKind.BLOCK_PROBE_FAIL: (_("""
You can continue and the installer will just present the disks present
in the system and not other block devices, or you may be able to fix
the issue by switching to a shell and reconfiguring the system's block
devices manually.
"""), ['debug_shell', 'continue']),
    ErrorReportKind.DISK_PROBE_FAIL: (_("""
You may be able to fix the issue by switching to a shell and
reconfiguring the system's block devices manually.
"""), ['debug_shell', 'continue']),
    ErrorReportKind.INSTALL_FAIL: (_("""
Do you want to try starting the installation again?
"""), ['restart', 'close']),
    ErrorReportKind.UI: (_("Close this dialog to continue."), ['close']),
    ErrorReportKind.UNKNOWN: ("", ['close']),
}


class ErrorReportStretchy(Stretchy):

    def __init__(self, app, parent, report, interrupting=True):
        self.app = app
        self.report = report
        self.parent = parent
        self.interrupting = interrupting

        self.btns = {
            'close': close_btn(parent, _("Close report")),
            'continue': close_btn(parent, _("Continue")),
            'debug_shell': other_btn(
                _("Switch to a shell"), on_press=self.debug_shell),
            'restart': other_btn(
                _("Restart installer"), on_press=self.restart),
            'view': other_btn(
                _("View Full Report"), on_press=self.view_report),
            }
        w = 0
        for n, b in self.btns.items():
            w = max(w, widget_width(b))
        for n, b in self.btns.items():
            self.btns[n] = Padding(b, width=w, align='center')

        self.spinner = Spinner(app.loop, style='dots')
        self.pile = Pile([])
        self._report_changed()
        super().__init__("", [self.pile], 0, 0)
        connect_signal(self, 'closed', self.spinner.stop)

    def _pile_elements(self):
        widgets = [
            Text(rewrap(_(error_report_intros[self.report.kind]))),
            Text(""),
            ]

        self.spinner.stop()

        if self.report.state == ErrorReportState.DONE:
            widgets.append(self.btns['view'])
        else:
            text, spin = error_report_state_descriptions[self.report.state]
            widgets.append(Text(rewrap(_(text))))
            if spin:
                self.spinner.start()
                widgets.extend([
                    Text(""),
                    self.spinner])

        fs_label, fs_loc = self.report.persistent_details
        if fs_label is not None:
            location_text = _(
                "The error report has been saved to\n\n  {loc}\n\non the "
                "filesystem with label {label!r}.").format(
                    loc=fs_loc, label=fs_label)
            widgets.extend([
                Text(""),
                Text(location_text),
                ])

        widgets.append(Text(""))

        if self.interrupting:
            text, btns = error_report_options[self.report.kind]
            if text:
                widgets.extend([Text(rewrap(_(text))), Text("")])
            for b in btns:
                widgets.append(self.btns[b])
        else:
            widgets.extend([
                self.btns['close'],
                ])

        return widgets

    def _report_changed(self):
        self.pile.contents[:] = [
            (w, self.pile.options('pack')) for w in self._pile_elements()]
        while not self.pile.focus.selectable():
            self.pile.focus_position += 1

    def debug_shell(self, sender):
        self.parent.remove_overlay()
        self.app.debug_shell()

    def restart(self, sender):
        # Should unmount and delete /target.
        # We rely on systemd restarting us.
        self.app.exit()

    def view_report(self, sender):
        self.app.run_command_in_foreground(["less", self.report.path])

    def opened(self):
        self.report.mark_seen()
        connect_signal(self.report, 'changed', self._report_changed)

    def closed(self):
        disconnect_signal(self.report, 'changed', self._report_changed)


class ErrorReportListStretchy(Stretchy):

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        rows = [
            TableRow([
                Text(""),
                Text(_("DATE")),
                Text(_("KIND")),
                Text(_("STATUS")),
                Text(""),
            ])]
        self.report_to_row = {}
        for report in self.app.error_controller.reports:
            connect_signal(report, "changed", self._report_changed, report)
            r = self.report_to_row[report] = self.row_for_report(report)
            rows.append(r)
        self.table = TablePile(rows, colspecs={1: ColSpec(can_shrink=True)})
        widgets = [
            Text(_("Select an error report to view:")),
            Text(""),
            self.table,
            Text(""),
            button_pile([close_btn(parent)]),
            ]
        super().__init__("", widgets, 2, 2)

    def open_report(self, sender, report):
        self.parent.show_stretchy_overlay(
            ErrorReportStretchy(self.app, self.parent, report, False))

    def state_for_report(self, report):
        if report.seen:
            return _("VIEWED")
        return _("UNVIEWED")

    def cells_for_report(self, report):
        date = report.pr.get("Date", "???")
        icon = ClickableIcon(date)
        connect_signal(icon, 'click', self.open_report, report)
        return [
            Text("["),
            icon,
            Text(_(report.kind.value)),
            Text(_(self.state_for_report(report))),
            Text("]"),
            ]

    def row_for_report(self, report):
        return Color.menu_button(
            TableRow(self.cells_for_report(report)))

    def _report_changed(self, report):
        old_r = self.report_to_row.get(report)
        if old_r is None:
            return
        old_r = old_r.base_widget
        new_cells = self.cells_for_report(report)
        for (s1, old_c), new_c in zip(old_r.cells, new_cells):
            old_c.set_text(new_c.text)
        self.table.invalidate()
