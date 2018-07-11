import asyncio
import json
import logging
import pyqtgraph
import numpy as np

from artiq.applets.simple import SimpleApplet
from artiq.protocols.pc_rpc import AsyncioClient
from artiq.protocols.sync_struct import Subscriber
from quamash import QtWidgets, QtCore
from .utils import eval_param_default

logger = logging.getLogger(__name__)


# Colours to use for data series (RGBA)
SERIES_COLORS = ["#d9d9d999", "#fdb46299", "#80b1d399", "#fb807299", "#bebeada99", "#ffffb399"]


class _XYSeries:
    def __init__(self, plot, data_name, data_item, error_bar_name, error_bar_item, plot_left_to_right):
        self.plot = plot
        self.data_item = data_item
        self.data_name = data_name
        self.error_bar_item = error_bar_item
        self.error_bar_name = error_bar_name
        self.plot_left_to_right = plot_left_to_right
        self.num_current_points = 0

    def update(self, x_data, data):
        def channel(name):
            return data.get("ndscan.points.channel_" + name, (False, []))[1]

        y_data = channel(self.data_name)
        num_to_show = min(len(x_data), len(y_data))

        if self.error_bar_item:
            y_err = channel(self.error_bar_name)
            num_to_show = min(num_to_show, len(y_err))

        if num_to_show == self.num_current_points:
            return

        if self.plot_left_to_right:
            x_data = np.array(x_data)
            order = np.argsort(x_data[:num_to_show])

            y_data = np.array(y_data)
            self.data_item.setData(x_data[order], y_data[order])
            if self.num_current_points == 0:
                self.plot.addItem(self.data_item)

            if self.error_bar_item:
                y_err = np.array(y_err)
                self.error_bar_item.setData(x=x_data[order], y=y_data[order], height=y_err[order])
                if self.num_current_points == 0:
                    self.plot.addItem(self.error_bar_item)
        else:
            self.data_item.setData(x_data[:num_to_show], y_data[:num_to_show])
            if self.num_current_points == 0:
                self.plot.addItem(self.data_item)

            if self.error_bar_item:
                self.error_bar_item.setData(x=x_data[:num_to_show], y=y_data[:num_to_show],
                    height=(2 * np.array(y_err[:num_to_show])))
                if self.num_current_points == 0:
                    self.plot.addItem(self.error_bar_item)

        self.num_current_points = num_to_show


class _XYPlotWidget(pyqtgraph.PlotWidget):
    error = QtCore.pyqtSignal(str)

    def __init__(self, x_schema, set_dataset):
        super().__init__()

        self.set_dataset = set_dataset

        self.series_initialised = False
        self.series = []

        x_spec = x_schema["param"]["spec"]

        self.x_unit_suffix = ""
        unit = x_spec.get("unit", "")
        if unit:
            self.x_unit_suffix = " " + unit
            unit = "/ " + unit + " "

        path = x_schema["path"]
        if not path:
            path = "/"
        param = x_schema["param"]["fqn"] + "@" + path

        description = x_schema["param"]["description"]
        label = "<b>{} {}</b><i>({})</i>".format(description, unit, param)
        self.setLabel("bottom", label)

        self.x_data_to_display_scale = 1 / x_spec["scale"]
        self.getAxis("bottom").setScale(self.x_data_to_display_scale)
        self.getAxis("bottom").autoSIPrefix = False

        self.showGrid(x=True, y=True)

        # Crosshair cursor with coordinate display. The TextItems for displaying
        # the coordinates are updated on a timer to avoid a lag trail of buffered
        # redraws when there are a lot of points.
        #
        # TODO: Abstract out, use for other plots as well.
        self.getPlotItem().getViewBox().hoverEvent = self._on_viewbox_hover
        self.setCursor(QtCore.Qt.CrossCursor)
        self.crosshair_timer = QtCore.QTimer(self)
        self.crosshair_timer.timeout.connect(self._update_crosshair_text)
        self.crosshair_timer.setSingleShot(True)
        self.crosshair_x_text = None
        self.crosshair_y_text = None

        self._install_context_menu(x_schema)

    def _on_viewbox_hover(self, event):
        if event.isExit():
            self.removeItem(self.crosshair_x_text)
            self.crosshair_x_text = None
            self.removeItem(self.crosshair_y_text)
            self.crosshair_y_text = None

            self.crosshair_timer.stop()
            return

        self.last_hover_event = event
        self.crosshair_timer.start(0)

    def _update_crosshair_text(self):
        vb = self.getPlotItem().getViewBox()
        data_coords = vb.mapSceneToView(self.last_hover_event.scenePos())

        def make_text():
            text = pyqtgraph.TextItem()
            # Don't take text item into account for auto-scaling; otherwise
            # there will be positive feedback if the cursor is towards the
            # bottom right of the screen.
            text.setFlag(text.ItemHasNoContents)
            self.addItem(text)
            return text

        if not self.crosshair_x_text:
            self.crosshair_x_text = make_text()

        if not self.crosshair_y_text:
            self.crosshair_y_text = make_text()

        x_range, y_range = vb.state['viewRange']
        x_range = np.array(x_range) * self.x_data_to_display_scale
        def num_digits_after_point(r):
            # We want to be able to resolve at least 1000 points in the displayed
            # range.
            smallest_digit = np.floor(np.log10(r[1] - r[0])) - 3
            return int(-smallest_digit) if smallest_digit < 0 else 0

        self.crosshair_x_text.setText("{0:.{width}f}{1}".format(
            data_coords.x() * self.x_data_to_display_scale,
            self.x_unit_suffix, width=num_digits_after_point(x_range)))
        self.crosshair_x_text.setPos(data_coords)

        self.last_crosshair_x = data_coords.x()

        y_text_pos = QtCore.QPointF(self.last_hover_event.scenePos())
        y_text_pos.setY(self.last_hover_event.scenePos().y() + 10)
        self.crosshair_y_text.setText("{0:.{width}f}".format(data_coords.y(), width=num_digits_after_point(y_range)))
        self.crosshair_y_text.setPos(vb.mapSceneToView(y_text_pos))


    def data_changed(self, data, mods):
        def d(name):
            return data.get("ndscan." + name, (False, None))[1]

        if not self.series_initialised:
            channels_json = d("channels")
            if not channels_json:
                return

            channels = json.loads(channels_json)

            try:
                data_names, error_bar_names = _extract_scalar_channels(channels)
            except ValueError as e:
                self.emit.error(str(e))

            for i, name in enumerate(data_names):
                color = SERIES_COLORS[i % len(SERIES_COLORS)]
                data_item = pyqtgraph.ScatterPlotItem(pen=None, brush=color, size=5)

                error_bar_name = error_bar_names.get(name, None)
                error_bar_item = pyqtgraph.ErrorBarItem(pen=color) if error_bar_name else None

                self.series.append(_XYSeries(self, name, data_item, error_bar_name, error_bar_item, False))

            self.series_initialised = True

        x_data = d("points.axis_0")
        if not x_data:
            return

        for s in self.series:
            s.update(x_data, data)


    def _install_context_menu(self, x_schema):
        entries = []

        for d in _extract_linked_datasets(x_schema["param"]):
            action = QtWidgets.QAction("Set '{}' from crosshair".format(d), self)
            action.triggered.connect(lambda: self._set_dataset_from_crosshair_x(d))
            entries.append(action)

        if not entries:
            return

        separator = QtWidgets.QAction("", self)
        separator.setSeparator(True)
        entries.append(separator)
        self.plotItem.getContextMenus = lambda ev: entries + [self.getMenu()]

    def _set_dataset_from_crosshair_x(self, dataset):
        self.set_dataset(dataset, self.last_crosshair_x)


def _extract_linked_datasets(param_schema):
    datasets = []
    try:
        def log_datasets(dataset, default):
            datasets.append(dataset)
            return default
        eval_param_default(param_schema["default"], log_datasets)
    except Exception as e:
        # Ignore default parsing errors here; the user will get warnings from the
        # experiment dock and on the core device anyway.
        print(e)
        pass
    return datasets

def _extract_scalar_channels(channels):
    data_names = set(name for name, spec in channels.items() if spec["type"] in ["int", "float"])

    # Build map from "primary" channel names to error bar names.
    error_bar_names = {}
    for name in data_names:
        spec = channels[name]
        display_hints = spec.get("display_hints", {})
        eb = display_hints.get("error_bar_for", "")
        if eb:
            if eb in error_bar_names:
                raise ValueError("More than one set of error bars specified for channel '{}'".format(eb))
            error_bar_names[eb] = name

    data_names -= set(error_bar_names.values())

    return data_names, error_bar_names


class _Rolling1DSeries:
    def __init__(self, plot, data_name, data_item, error_bar_name, error_bar_item, history_length):
        self.plot = plot
        self.data_item = data_item
        self.data_name = data_name
        self.error_bar_item = error_bar_item
        self.error_bar_name = error_bar_name

        self.values = np.array([]).reshape((0, 2))
        self.set_history_length(history_length)

    def append(self, data):
        new_data = data["ndscan.point." + self.data_name][1]
        if self.error_bar_item:
            new_error_bar = data["ndscan.point." + self.error_bar_name][1]

        p = [new_data, 2 * new_error_bar] if self.error_bar_item else [new_data]

        is_first = (self.values.shape[0] == 0)
        if is_first:
            self.values = np.array([p])
        else:
            if self.values.shape[0] == len(self.x_indices):
                self.values = np.roll(self.values, -1, axis=0)
                self.values[-1, :] = p
            else:
                self.values = np.vstack((self.values, p))

        num_to_show = self.values.shape[0]
        self.data_item.setData(self.x_indices[-num_to_show:], self.values[:, 0].T)
        if self.error_bar_item:
            self.error_bar_item.setData(x=self.x_indices[-num_to_show:], y=self.values[:, 0].T,
                height=self.values[:, 1].T)

        if is_first:
            self.plot.addItem(self.data_item)
            if self.error_bar_item:
                self.plot.addItem(self.error_bar_item)

    def set_history_length(self, n):
        assert n > 0, "Invalid history length"
        self.x_indices = np.arange(-n, 0)
        if self.values.shape[0] > n:
            self.values = self.values[-n:, :]


class _RollingPlotWidget(pyqtgraph.PlotWidget):
    error = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()

        self.series_initialised = False
        self.series = []

        self.point_phase = False

        self.showGrid(x=True, y=True)

        self._install_context_menu()

    def data_changed(self, data, mods):
        def d(name):
            return data.get("ndscan." + name, (False, None))[1]

        if not self.series_initialised:
            channels_json = d("channels")
            if not channels_json:
                return

            channels = json.loads(channels_json)

            try:
                data_names, error_bar_names = _extract_scalar_channels(channels)
            except ValueError as e:
                self.emit.error(str(e))

            for i, data_name in enumerate(data_names):
                color = SERIES_COLORS[i % len(SERIES_COLORS)]
                data_item = pyqtgraph.ScatterPlotItem(pen=None, brush=color)

                error_bar_name = error_bar_names.get(data_name, None)
                error_bar_item = pyqtgraph.ErrorBarItem(pen=color) if error_bar_name else None

                self.series.append(_Rolling1DSeries(self, data_name, data_item,
                    error_bar_name, error_bar_item, self.num_history_box.value()))

            self.series_initialised = True

        phase = d("point_phase")
        if phase is not None and phase != self.point_phase:
            for s in self.series:
                s.append(data)
            self.point_phase = phase

    def set_history_length(self, n):
        for s in self.series:
            s.set_history_length(n)

    def _install_context_menu(self):
        self.num_history_box = QtWidgets.QSpinBox()
        self.num_history_box.setMinimum(1)
        self.num_history_box.setMaximum(2**16)
        self.num_history_box.setValue(100)
        self.num_history_box.valueChanged.connect(self.set_history_length)

        container = QtWidgets.QWidget()

        layout = QtWidgets.QHBoxLayout()
        container.setLayout(layout)

        label = QtWidgets.QLabel("N: ")
        layout.addWidget(label)

        layout.addWidget(self.num_history_box)

        action = QtWidgets.QWidgetAction(self)
        action.setDefaultWidget(container)

        separator = QtWidgets.QAction("", self)
        separator.setSeparator(True)
        entries = [
            action,
            separator
        ]
        self.plotItem.getContextMenus = lambda ev: entries + [self.getMenu()]


class _MainWidget(QtWidgets.QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args

        self.setWindowTitle("ndscan plot")
        self.resize(800, 500)

        self.layout = QtWidgets.QVBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(self.layout)

        self.widget_stack = QtWidgets.QStackedWidget()
        self.message_label = QtWidgets.QLabel(
            "Waiting for ndscan metadata for rid {}…".format(self.args.rid))
        self.widget_stack.addWidget(self.message_label)
        self.layout.addWidget(self.widget_stack)

        self.title_set = False
        self.plot_initialised = False

    def data_changed(self, data, mods):
        def d(name):
            return data.get("ndscan." + name, (False, None))[1]

        if not self.title_set:
            fqn = d("fragment_fqn")
            if not fqn:
                return
            self.setWindowTitle("{} – ndscan".format(fqn))
            self.title_set = True

        if not self.plot_initialised:
            axes_json = d("axes")
            if not axes_json:
                return
            axes = json.loads(axes_json)
            if len(axes) == 0:
                self.plot = _RollingPlotWidget()
            elif len(axes) == 1:
                self.plot = _XYPlotWidget(axes[0], self.set_dataset)
            else:
                self.message_label.setText(
                    "{}-dimensional scans are not yet supported".format(len(axes)))
                self._show(self.message_label)
                return

            self.plot.error.connect(self._show_error)
            self.widget_stack.addWidget(self.plot)
            self._show(self.plot)

            self.plot_initialised = True

        self.plot.data_changed(data, mods)

    def _show(self, widget):
        self.widget_stack.setCurrentIndex(self.widget_stack.indexOf(widget))

    def _show_error(self, message):
        self.message_label.setText("Error: " + message)
        self._show(self.message_label)

    def set_dataset(self, key, value):
        asyncio.ensure_future(self._set_dataset_impl(key, value))

    async def _set_dataset_impl(self, key, value):
        logger.info("Setting '%s' to %s", key, value)
        try:
            remote = AsyncioClient()
            await remote.connect_rpc(self.args.server, self.args.port_control,
                "master_dataset_db")
            try:
                await remote.set(key, value)
            finally:
                remote.close_rpc()
        except:
            logger.error("Failed to set dataset '%s'", key, exc_info=True)


class NdscanApplet(SimpleApplet):
    def __init__(self):
        # Use a small update delay by default to avoid lagging out the UI by
        # continuous redraws for plots with a large number of points. (20 ms
        # is a pretty arbitrary choice for a latency not perceptible by the
        # user in a normal use case).
        super().__init__(_MainWidget, default_update_delay=20e-3)

        self.argparser.add_argument(
            "--port-control", default=3251, type=int,
            help="TCP port for master control commands")
        self.argparser.add_argument("--rid", help="RID of the experiment to plot")

    def subscribe(self):
        # We want to subscribe only to the experiment-local datasets for our RID
        # (but always, even if using IPC – this can be optimised later).
        self.subscriber = Subscriber("datasets_rid_{}".format(self.args.rid),
                                     self.sub_init, self.sub_mod)
        self.loop.run_until_complete(self.subscriber.connect(
            self.args.server, self.args.port))

        # Make sure we still respond to non-dataset messages like `terminate` in
        # embed mode.
        if self.embed is not None:
            def ignore(*args):
                pass
            self.ipc.subscribe([], ignore, ignore)

    def unsubscribe(self):
        self.loop.run_until_complete(self.subscriber.close())

    def filter_mod(self, *args):
        return True


def main():
    pyqtgraph.setConfigOptions(antialias=True)

    applet = NdscanApplet()
    applet.run()

if __name__ == "__main__":
    main()
