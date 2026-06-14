#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 WaveScope — 传感器数据波形分析工具  v3.5
================================================================================
 基于 PySide6 + PyQtGraph + Pandas 构建。

 功能：
   · 加载 CSV，自动解析表头（采样检测编码，支持 utf-8/gbk/gb2312/latin-1/cp1252）
   · 动态添加波形窗口（QMdiArea 承载，可自由拖拽/缩放/排列，无吸附不挤占面板）
   · 每窗口支持多条曲线，每条独立配置 Y 轴、颜色、线径、透明度、显示模式
   · 多组独立 X/Y 轴联动（组 A/B/C/D 各自独立绑定，互不干扰）
   · 菜单栏 → 光标 → 十字追踪光标 + 多窗口 X 轴同步
   · 完整保留 PyQtGraph 原生鼠标交互 + 自动下采样
   · 默认配置：线径 0.5px，点线结合模式
   · O(log N) 数据点吸附（searchsorted 二分查找）
   · 深色/浅色 双主题，Fusion 风格

 运行：pythonw wavescope.pyw
 依赖：pip install PySide6 pyqtgraph pandas numpy
================================================================================
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QTimer, QThread
from PySide6.QtGui import QColor, QAction, QIcon, QPalette, QPainter
import socket
import struct

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QComboBox, QLabel, QDoubleSpinBox,
    QGroupBox, QScrollArea, QMessageBox, QColorDialog,
    QFrame, QSizePolicy, QSpinBox, QSplitter, QMdiArea, QMdiSubWindow,
    QLineEdit, QDialog, QTabWidget, QFormLayout, QCheckBox,
)
import pyqtgraph as pg

# ═══════════════════════════════════════════════════════════════════════════════
pg.setConfigOptions(antialias=True, background='k', foreground='w', useOpenGL=False)

# ═══════════════════════════════════════════════════════════════════════════════
# 全局常量
# ═══════════════════════════════════════════════════════════════════════════════
PRESET_COLORS: list[tuple[str, str]] = [
    ("🔴 红色", "#FF4444"), ("🟢 绿色", "#44FF44"),
    ("🔵 蓝色", "#4488FF"), ("🟡 黄色", "#FFFF44"),
    ("🟣 青色", "#44FFFF"), ("🟠 洋红", "#FF44FF"),
    ("🟤 橙色", "#FF8844"), ("⚪ 白色", "#FFFFFF"),
    ("🔘 灰色", "#888888"), ("⭐ 金色", "#FFD700"),
    ("💗 粉色", "#FF69B4"), ("🌿 浅绿", "#90EE90"),
]

DISPLAY_MODES: dict[str, str] = {
    "折线 (Line)":             "line",
    "散点 (Scatter)":          "scatter",
    "点线结合 (Line+Scatter)":  "both",
}

# X/Y 联动组：0 = 无联动，1~4 = 组 A~D
LINK_GROUP_NAMES: list[str] = ["无", "A", "B", "C", "D"]


# ==============================================================================
# 数据中心
# ==============================================================================
class DataCenter:
    """全局数据中心：加载 CSV，以 float64 NumPy 数组形式提供列数据。"""

    def __init__(self) -> None:
        self._df: pd.DataFrame | None = None
        self._file_path: str | None = None

    @property
    def is_loaded(self) -> bool:
        return self._df is not None

    @property
    def file_path(self) -> str | None:
        return self._file_path

    @property
    def row_count(self) -> int:
        return len(self._df) if self._df is not None else 0

    @property
    def columns(self) -> list[str]:
        return list(self._df.columns) if self._df is not None else []

    def load_csv(self, file_path: str) -> pd.DataFrame:
        for enc in ['utf-8', 'gbk', 'gb2312', 'latin-1', 'cp1252']:
            try:
                df = pd.read_csv(file_path, encoding=enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            raise ValueError(
                f"无法以任何已知编码读取文件:\n{file_path}\n"
                f"已尝试: utf-8, gbk, gb2312, latin-1, cp1252"
            )
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 提供一个默认的序列，从1开始到走过的长度
        if "Index" not in df.columns:
            df.insert(0, "Index", range(1, len(df) + 1))

        self._df = df
        self._file_path = file_path
        return df

    def get_column_data(self, col_name: str) -> np.ndarray:
        if self._df is None or col_name not in self._df.columns:
            return np.array([], dtype=np.float32)
        return self._df[col_name].to_numpy(dtype=np.float32, na_value=np.nan)


# ==============================================================================
# 动态接收模块 (Live Streaming)
# ==============================================================================

class RingBuffer:
    def __init__(self, capacity=100000):
        self.capacity = capacity
        self.data = np.full(capacity, np.nan, dtype=np.float32)
        self.head = 0
        self.is_full = False
        # 预分配线性化缓冲区，避免 get_data 每次调用 np.concatenate 分配 4MB+
        self._linear: np.ndarray = np.empty(capacity, dtype=np.float32)
        self._linear_valid: bool = False  # 线性化缓存是否有效

    def append_many(self, values: list[float]) -> None:
        n = len(values)
        if n == 0: return
        if n >= self.capacity:
            self.data[:] = values[-self.capacity:]
            self.head = 0
            self.is_full = True
            self._linear_valid = False
            return

        end = self.head + n
        if end <= self.capacity:
            self.data[self.head:end] = values
        else:
            overflow = end - self.capacity
            self.data[self.head:] = values[:-overflow]
            self.data[:overflow] = values[-overflow:]
        self.head = (self.head + n) % self.capacity
        if end >= self.capacity:
            self.is_full = True
        self._linear_valid = False  # 数据写入后缓存失效

    def get_data(self) -> np.ndarray:
        if not self.is_full:
            return self.data[:self.head]
        # 使用预分配缓冲区的懒线性化：只在实际读取时重建一次，避免每帧 concat 分配
        if not self._linear_valid:
            h = self.head
            c = self.capacity
            self._linear[:c - h] = self.data[h:]
            self._linear[c - h:] = self.data[:h]
            self._linear_valid = True
        return self._linear


class JustFloatParser:
    TAIL = bytes([0x00, 0x00, 0x80, 0x7F])
    
    def __init__(self) -> None:
        self.buffer = bytearray()
        
    def parse(self, data: bytes) -> list[np.ndarray]:
        self.buffer.extend(data)
        packets = []
        while True:
            idx = self.buffer.find(self.TAIL)
            if idx == -1:
                break
            packet_data = self.buffer[:idx]
            del self.buffer[:idx + 4]
            if len(packet_data) % 4 == 0 and len(packet_data) > 0:
                # 直接通过 numpy 从内存反序列化，极大提升解析速度
                arr = np.frombuffer(packet_data, dtype=np.float32)
                packets.append(arr)
        return packets


class LiveCenter:
    def __init__(self, capacity=1000000) -> None:
        self.capacity = capacity
        self.buffers: dict[str, RingBuffer] = {}
        self.time_buffer = RingBuffer(capacity)
        self.global_index = 0
        self.parser = JustFloatParser()
        self.columns_changed = False

    def reset_capacity(self, capacity: int) -> None:
        if self.capacity == capacity:
            return
            
        old_data = {k: v.get_data() for k, v in self.buffers.items()}
        old_time = self.time_buffer.get_data() if hasattr(self, 'time_buffer') else np.array([], dtype=np.float32)
        
        self.capacity = capacity
        
        n_ch = len(self.buffers)
        self.buffers.clear()
        for i in range(n_ch):
            col = f"通道 {i+1}"
            rb = RingBuffer(capacity)
            if col in old_data and len(old_data[col]) > 0:
                rb.append_many(old_data[col][-capacity:])
            self.buffers[col] = rb
            
        self.time_buffer = RingBuffer(capacity)
        if len(old_time) > 0:
            self.time_buffer.append_many(old_time[-capacity:])

    def clear(self) -> None:
        n_ch = len(self.buffers)
        self.buffers.clear()
        for i in range(n_ch):
            self.buffers[f"通道 {i+1}"] = RingBuffer(self.capacity)
        self.time_buffer = RingBuffer(self.capacity)
        self.global_index = 0
        self.parser = JustFloatParser()

    def is_loaded(self) -> bool:
        return len(self.buffers) > 0

    def get_columns(self) -> list[str]:
        if not self.buffers: return []
        return ["时间(Index)"] + list(self.buffers.keys())

    def process_data(self, data_bytes: bytes) -> None:
        packets = self.parser.parse(data_bytes)
        if not packets: return
        
        n_ch = len(packets[0])
        # 过滤掉畸形包以防 numpy vstack 报错
        valid_packets = [p for p in packets if len(p) == n_ch]
        if not valid_packets: return
        
        if len(self.buffers) != n_ch:
            self.buffers.clear()
            for i in range(n_ch):
                self.buffers[f"通道 {i+1}"] = RingBuffer(self.capacity)
            self.time_buffer = RingBuffer(self.capacity)
            self.global_index = 0
            self.columns_changed = True
            
        # 核心性能优化：利用 numpy 矩阵化合并数据，避免 Python for 循环处理单点浮点数
        arr = np.vstack(valid_packets)
        n_packets = arr.shape[0]
        
        times = np.arange(self.global_index + 1, self.global_index + 1 + n_packets, dtype=np.float32)
        self.global_index += n_packets
        
        self.time_buffer.append_many(times)
        for i in range(n_ch):
            self.buffers[f"通道 {i+1}"].append_many(arr[:, i])

    def get_column_data(self, col_name: str) -> np.ndarray:
        if col_name == "时间(Index)":
            return self.time_buffer.get_data()
        buf = self.buffers.get(col_name)
        if buf is not None:
            return buf.get_data()
        return np.array([], dtype=np.float32)


class ReceiverThread(QThread):
    data_received = Signal(bytes)
    error_occurred = Signal(str)
    connection_closed = Signal()
    
    def __init__(self, mode: str, port: str, baud_or_ip: str) -> None:
        super().__init__()
        self.mode = mode
        self.port = port
        self.baud_or_ip = baud_or_ip
        self.running = True
        self.sock = None
        self.ser = None
        
    def run(self) -> None:
        try:
            if self.mode == 'tcp':
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(1.0)
                self.sock.connect((self.baud_or_ip, int(self.port)))
                while self.running:
                    try:
                        data = self.sock.recv(4096)
                        if data:
                            self.data_received.emit(data)
                        else:
                            break
                    except socket.timeout:
                        continue
            elif self.mode == 'serial':
                import serial
                self.ser = serial.Serial(self.port, int(self.baud_or_ip), timeout=1.0)
                while self.running:
                    data = self.ser.read(4096)
                    if data:
                        self.data_received.emit(data)
        except Exception as e:
            if self.running:
                self.error_occurred.emit(str(e))
        finally:
            if self.running:
                self.connection_closed.emit()
            self.running = False
            if self.sock:
                try: self.sock.close()
                except: pass
            if self.ser:
                try: self.ser.close()
                except: pass

    def stop(self) -> None:
        self.running = False
        self.wait(1000)


# ==============================================================================
# 曲线配置（v3.0: line_width + scatter_size → stroke_width "线径"）
# ==============================================================================
class CurveConfig:
    """单条曲线配置。"""

    def __init__(self, curve_index: int, color_seed: int = 0) -> None:
        self.curve_index: int = curve_index
        self.color_seed: int = color_seed
        self.y_column: str = ""
        self.color: QColor = QColor(PRESET_COLORS[color_seed % len(PRESET_COLORS)][1])
        self.stroke_width: float = 0.5   # 线径，同时控制线宽和散点大小
        self.alpha: int = 255            # 0~255
        self.display_mode: str = 'both'  # 点线结合 / 折线 / 散点
        self.axis_index: int = 0         # 0=主轴(左1), 1=副轴(右1), 2=副轴(左2), 3=副轴(右2)
        self.visible: bool = True        # 是否显示曲线
        self._last_y_column: str = ""    # 记录上一次的Y列名，用于检测数据变更
        # Pen/Brush 缓存：避免每帧重新构建 QColor/QPen/QBrush
        self._cached_pen: object = None
        self._cached_brush: object = None
        self._cached_style_key: tuple = ()

    def _style_key(self) -> tuple:
        """返回当前样式的哈希键，用于检测是否需要重建 pen/brush"""
        return (self.display_mode, self.stroke_width, self.alpha,
                self.color.red(), self.color.green(), self.color.blue())

    def get_pen(self):
        """返回缓存的 QPen，仅在样式参数变更时重建"""
        key = self._style_key()
        if self._cached_style_key != key:
            base = QColor(self.color)
            base.setAlpha(self.alpha)
            self._cached_pen = pg.mkPen(color=base, width=self.stroke_width)
            self._cached_brush = pg.mkBrush(base)
            self._cached_style_key = key
        return self._cached_pen

    def get_brush(self):
        """返回缓存的 QBrush"""
        self.get_pen()  # 确保缓存有效
        return self._cached_brush


# ==============================================================================
# 通道配置（v3.0: link_x/link_y bool → link_group_x/y int 多组绑定）
# ==============================================================================
class ChannelConfig:
    """一个波形窗口的配置。link_group_x/y: 0=无, 1~4=组A~D"""

    def __init__(self, channel_id: int) -> None:
        self.channel_id: int = channel_id
        self.window_name: str = f"窗口 #{channel_id + 1}"
        self.x_column: str = ""
        self.link_group_x: int = 0
        self.link_group_y: int = 0
        self.auto_scale_y: bool = True
        self.x_initialized: bool = False
        self.curves: list[CurveConfig] = []


# ==============================================================================
# 鼠标十字光标管理器（NEW v3.0）
# ==============================================================================
class CursorManager:
    """
    鼠标十字追踪光标管理器。

    功能：
      · 显示/隐藏十字虚线光标 + 坐标标签
      · 追踪模式 / 固定模式（中键单击固定到当前数据点）
      · 吸附到最近数据点（阈值：10 像素视口距离）
      · 多窗口 X 轴同步
      · 多轴感知：Y 值相对于鼠标所在 ViewBox

    v3.5: 支持多 Y 轴下的坐标显示。Y 值根据鼠标所在 ViewBox 的实际 Y 范围计算。
    """

    SNAP_PX_THRESHOLD = 10  # 吸附到数据点的像素距离阈值

    def __init__(self) -> None:
        self._enabled: bool = False
        self._tracking: bool = True      # True=追踪, False=固定
        self._sync_enabled: bool = False
        self._snap_to_data: bool = False
        self._label_mode: int = 2        # 0=不显示, 1=跟随光标, 2=轴侧
        self._entries: dict[int, dict] = {}

    def set_label_mode(self, mode: int) -> None:
        """0=不显示坐标, 1=跟随光标(右上), 2=轴侧(X左Y下)"""
        self._label_mode = mode
        show_combined = (mode == 1)
        show_split   = (mode == 2)
        for e in self._entries.values():
            if 'label' in e:       e['label'].setVisible(self._enabled and show_combined)
            if 'x_label' in e:     e['x_label'].setVisible(self._enabled and show_split)
            if 'y_label' in e:     e['y_label'].setVisible(self._enabled and show_split)

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        self._enabled = on
        for e in self._entries.values():
            e['vLine'].setVisible(on)
            e['hLine'].setVisible(on)
            e['label'].setVisible(on and self._label_mode == 1)
            e['x_label'].setVisible(on and self._label_mode == 2)
            e['y_label'].setVisible(on and self._label_mode == 2)

    def set_snap_to_data(self, on: bool) -> None:
        self._snap_to_data = on

    def set_sync_enabled(self, on: bool) -> None:
        self._sync_enabled = on

    def fix_cursor_at_current(self, ch_id: int) -> None:
        """中键单击 → 固定光标在当前追踪位置（toggle）"""
        if ch_id not in self._entries:
            return
        self._tracking = not self._tracking
        style = Qt.PenStyle.DashLine if self._tracking else Qt.PenStyle.SolidLine
        for e in self._entries.values():
            e['vLine'].setPen(pg.mkPen(color=(180, 180, 180, 160), width=1, style=style))
            e['hLine'].setPen(pg.mkPen(color=(180, 180, 180, 160), width=1, style=style))

    def register_plot(self, ch_id: int, wf) -> None:
        plot_item = wf.plot_item
        pen = pg.mkPen(color=(180, 180, 180, 120), width=1, style=Qt.PenStyle.DashLine)
        vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        hline = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        vline.setVisible(self._enabled)
        hline.setVisible(self._enabled)
        plot_item.addItem(vline, ignoreBounds=True)
        plot_item.addItem(hline, ignoreBounds=True)

        # 跟随光标标签（模式1）：右上角组合显示
        label = pg.TextItem("", anchor=(0, 1), color=(200, 200, 200),
                           fill=pg.mkBrush(0, 0, 0, 150))
        label.setVisible(self._enabled and self._label_mode == 1)
        plot_item.addItem(label, ignoreBounds=True)
        # 轴侧标签（模式2）：X在纵线上端(最上面), Y在横线右端(最右边)
        x_label = pg.TextItem("", anchor=(0, 0), color=(180, 220, 255),
                              fill=pg.mkBrush(0, 0, 0, 150))
        x_label.setVisible(self._enabled and self._label_mode == 2)
        plot_item.addItem(x_label, ignoreBounds=True)
        y_label = pg.TextItem("", anchor=(1, 1), color=(180, 220, 255),
                              fill=pg.mkBrush(0, 0, 0, 150))
        y_label.setVisible(self._enabled and self._label_mode == 2)
        plot_item.addItem(y_label, ignoreBounds=True)

        # 鼠标移动追踪
        proxy = pg.SignalProxy(
            plot_item.scene().sigMouseMoved, rateLimit=30,
            slot=lambda evt: self._on_mouse_moved(ch_id, evt),
        )
        # 中键单击 → 固定/释放光标
        plot_item.scene().sigMouseClicked.connect(
            lambda evt: self._on_mouse_clicked(ch_id, evt)
        )

        self._entries[ch_id] = {
            'wf': wf, 'plot_item': plot_item, 'vLine': vline, 'hLine': hline,
            'label': label, 'x_label': x_label, 'y_label': y_label,
            'proxy': proxy, 'x_data': None, 'y_data': None,
        }

    def unregister_plot(self, ch_id: int) -> None:
        if ch_id not in self._entries:
            return
        e = self._entries.pop(ch_id)
        # 断开信号连接，防止内存泄漏与悬空回调
        if 'proxy' in e and e['proxy'] is not None:
            try:
                e['proxy'].disconnect()
            except Exception:
                pass
        try:
            e['plot_item'].scene().sigMouseClicked.disconnect()
        except Exception:
            pass
        for key in ('vLine', 'hLine', 'label', 'x_label', 'y_label'):
            try:
                e['plot_item'].removeItem(e[key])
            except Exception:
                pass

    def clear_all(self) -> None:
        for ch_id in list(self._entries.keys()):
            self.unregister_plot(ch_id)

    def set_plot_data(self, ch_id: int,
                      curves_data: list[tuple[np.ndarray, np.ndarray]]) -> None:
        """缓存所有曲线的有效数据，供吸附到数据点时全局查找最近点"""
        if ch_id in self._entries:
            all_x, all_y = [], []
            for xd, yd in curves_data:
                mask = np.isfinite(xd) & np.isfinite(yd)
                all_x.append(xd[mask]); all_y.append(yd[mask])
            if all_x:
                self._entries[ch_id]['x_data'] = np.concatenate(all_x)
                self._entries[ch_id]['y_data'] = np.concatenate(all_y)
            else:
                self._entries[ch_id]['x_data'] = None
                self._entries[ch_id]['y_data'] = None

    # ── 内部 ──

    def _on_mouse_clicked(self, ch_id: int, evt) -> None:
        """中键单击 → 固定/释放光标"""
        if not self._enabled:
            return
        if evt.button() == Qt.MouseButton.MiddleButton:
            self.fix_cursor_at_current(ch_id)
            evt.accept()

    def _on_mouse_moved(self, source_id: int, evt) -> None:
        if not self._enabled or not self._tracking:
            return
        pos = evt[0]
        entry = self._entries.get(source_id)
        if entry is None:
            return
        pi = entry['plot_item']
        if not pi.sceneBoundingRect().contains(pos):
            return
        mp = pi.vb.mapSceneToView(pos)
        mx, my = mp.x(), mp.y()

        # 吸附到最近数据点
        if self._snap_to_data and entry.get('x_data') is not None and len(entry['x_data']) > 0:
            mx, my = self._snap_nearest(entry, pi, mx, my)

        # 获取所有轴的 Y 值
        wf = entry['wf']
        y_strs = []
        for aidx, vb in wf._vbs.items():
            mp_vb = vb.mapSceneToView(pos)
            axis_names = {0: "左1", 1: "左2", 2: "左3", 3: "右1", 4: "右2", 5: "右3"}
            name = axis_names.get(aidx, f"轴{aidx}")
            y_strs.append(f"{name}={mp_vb.y():.4g}")
            if aidx == 0:
                my = mp_vb.y() # hline 永远跟随主轴
                
        y_text = " | ".join(y_strs)

        entry['vLine'].setPos(mx)
        entry['hLine'].setPos(my)
        vb = pi.vb.viewRect()
        entry['label'].setText(f"  X={mx:.4g}\n  {y_text}")
        entry['label'].setPos(mx, my)
        entry['x_label'].setText(f"X={mx:.4g}")
        entry['x_label'].setPos(mx, vb.bottom())
        entry['y_label'].setText(y_text)
        entry['y_label'].setPos(vb.right(), my)

        # 不同步时，隐藏其他窗口的残影
        if not self._sync_enabled:
            for cid, oe in self._entries.items():
                if cid != source_id:
                    oe['vLine'].setVisible(False)
                    oe['hLine'].setVisible(False)
                    oe['label'].setVisible(False)
                    oe['x_label'].setVisible(False)
                    oe['y_label'].setVisible(False)
            entry['vLine'].setVisible(True)
            entry['hLine'].setVisible(True)
            entry['label'].setVisible(self._label_mode == 1)
            entry['x_label'].setVisible(self._label_mode == 2)
            entry['y_label'].setVisible(self._label_mode == 2)

        if self._sync_enabled:
            for cid, oe in self._entries.items():
                if cid != source_id:
                    oe['vLine'].setPos(mx)
                    oe['hLine'].setPos(my)
                    vb2 = oe['plot_item'].vb.viewRect()
                    oe['label'].setText(f"  X={mx:.4g}")
                    oe['label'].setPos(mx, my)
                    oe['x_label'].setText(f"X={mx:.4g}")
                    oe['x_label'].setPos(mx, vb2.bottom())
                    oe['y_label'].setText("")

    def _snap_nearest(self, entry: dict, pi: pg.PlotItem,
                      mx: float, my: float) -> tuple[float, float]:
        """查找最近数据点，若在阈值内则吸附（已进行视口与性能优化）"""
        xd, yd = entry['x_data'], entry['y_data']
        px_size = pi.vb.viewPixelSize()
        if px_size is None or px_size[0] is None:
            return mx, my
        dx_thresh = self.SNAP_PX_THRESHOLD * px_size[0]
        dy_thresh = self.SNAP_PX_THRESHOLD * (px_size[1] if px_size[1] is not None else px_size[0])

        # 1. 快速切片：仅筛选 X 轴落在阈值范围内的数据点
        dx_abs = np.abs(xd - mx)
        mask = dx_abs <= dx_thresh
        if not np.any(mask):
            return mx, my

        # 2. 对少量候选点进行二维距离计算
        cand_x = xd[mask]
        cand_y = yd[mask]
        dx_norm = (cand_x - mx) / max(dx_thresh, 1e-12)
        dy_norm = (cand_y - my) / max(dy_thresh, 1e-12)
        dist_sq = dx_norm * dx_norm + dy_norm * dy_norm

        nearest_idx = int(np.argmin(dist_sq))
        if dist_sq[nearest_idx] <= 1.0:
            return float(cand_x[nearest_idx]), float(cand_y[nearest_idx])

        return mx, my

# ==============================================================================
# 单条曲线控制微件（v3.0: 线径统一参数）
# ==============================================================================
class CurveControlWidget(QFrame):
    """单条曲线 UI。"""

    config_changed = Signal()
    delete_requested = Signal()

    def __init__(self, curve_config: CurveConfig, columns: list[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg: CurveConfig = curve_config
        self._columns: list[str] = columns
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setSpacing(3)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.setSpacing(1)

        self.cb_y = QComboBox()
        self.cb_y.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.cb_y.setMinimumWidth(50)
        self.cb_y.addItems(self._columns)
        if self._cfg.y_column and self._cfg.y_column in self._columns:
            self.cb_y.setCurrentText(self._cfg.y_column)
        self.cb_y.currentTextChanged.connect(self._emit)
        row.addWidget(self.cb_y, 1)

        self.cb_axis = QComboBox()
        self.cb_axis.addItems(["左轴 1", "左轴 2", "左轴 3", "右轴 1", "右轴 2", "右轴 3"])
        self.cb_axis.setCurrentIndex(min(self._cfg.axis_index, 5))
        self.cb_axis.setToolTip("选择坐标轴 (主轴/副轴)")
        no_arrow_style = "QComboBox::drop-down { width: 0px; border: none; } QComboBox { padding: 0px 4px; }"
        
        self.cb_axis.setMaximumWidth(48)
        self.cb_axis.setStyleSheet(no_arrow_style)
        self.cb_axis.view().setMinimumWidth(60)
        self.cb_axis.currentIndexChanged.connect(self._emit)
        row.addWidget(self.cb_axis)

        self._mode_labels = ["点线", "折线", "散点"]
        self._mode_idx = {"both": 0, "line": 1, "scatter": 2}.get(self._cfg.display_mode, 0)
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(self._mode_labels)
        self.cb_mode.setCurrentIndex(self._mode_idx)
        self.cb_mode.setToolTip("显示模式：点线 / 折线 / 散点")
        self.cb_mode.setMaximumWidth(42)
        self.cb_mode.setStyleSheet(no_arrow_style)
        self.cb_mode.view().setMinimumWidth(60)
        self.cb_mode.currentIndexChanged.connect(self._apply_mode)
        row.addWidget(self.cb_mode)

        self.spin_stroke = QDoubleSpinBox()
        self.spin_stroke.setRange(0.1, 30.0)
        self.spin_stroke.setSingleStep(0.1)
        self.spin_stroke.setDecimals(1)
        self.spin_stroke.setMaximumWidth(36)
        self.spin_stroke.setButtonSymbols(self.spin_stroke.ButtonSymbols.NoButtons)
        self.spin_stroke.setValue(self._cfg.stroke_width)
        self.spin_stroke.setToolTip("线径")
        self.spin_stroke.valueChanged.connect(self._emit)
        row.addWidget(self.spin_stroke)

        self.btn_color = QPushButton()
        self.btn_color.setFixedSize(22, 22)
        self.btn_color.setStyleSheet("QPushButton { padding:0; }")
        self.btn_color.setToolTip("曲线颜色")
        self._refresh_btn()
        self.btn_color.clicked.connect(self._pick_color)
        row.addWidget(self.btn_color)

        self.chk_vis = QCheckBox("")
        self.chk_vis.setFixedSize(22, 22)
        self.chk_vis.setStyleSheet("QCheckBox::indicator { width: 18px; height: 18px; }")
        self.chk_vis.setToolTip("显示/隐藏此曲线")
        self.chk_vis.setChecked(getattr(self._cfg, 'visible', True))
        self.chk_vis.toggled.connect(self._emit)
        row.addWidget(self.chk_vis)

        self.btn_del = QPushButton("X")
        self.btn_del.setFixedSize(22, 22)
        self.btn_del.setToolTip("删除此曲线")
        self.btn_del.setStyleSheet(
            "QPushButton { padding:0; font-weight:bold; }"
            "QPushButton:hover { color:#e55; }"
        )
        self.btn_del.clicked.connect(self.delete_requested.emit)
        row.addWidget(self.btn_del)
        layout.addLayout(row)

    # ── 槽 ──
    _MODE_KEYS = ["both", "line", "scatter"]

    @Slot()
    def _emit(self, *_args) -> None:
        self._cfg.y_column = self.cb_y.currentText()
        self._cfg.stroke_width = self.spin_stroke.value()
        self._cfg.axis_index = self.cb_axis.currentIndex()
        if hasattr(self, 'chk_vis'):
            self._cfg.visible = self.chk_vis.isChecked()
        self.config_changed.emit()

    @Slot(int)
    def _apply_mode(self, idx: int) -> None:
        self._mode_idx = idx % 3
        self._cfg.display_mode = self._MODE_KEYS[self._mode_idx]
        self.config_changed.emit()

    @Slot()
    def _pick_color(self) -> None:
        c = QColorDialog.getColor(self._cfg.color, self, "选择曲线颜色")
        if c.isValid():
            c.setAlpha(self._cfg.alpha)
            self._cfg.color = c
            self._refresh_btn()
            self.config_changed.emit()

    def _refresh_btn(self) -> None:
        c = self._cfg.color
        self.btn_color.setStyleSheet(
            f"background-color: rgba({c.red()},{c.green()},{c.blue()},{c.alpha() / 255:.2f}); "
            f"border:1px solid #666; border-radius:4px;"
        )

    def update_columns(self, columns: list[str]) -> None:
        self._columns = columns
        prev = self.cb_y.currentText()
        self.cb_y.blockSignals(True)
        self.cb_y.clear()
        self.cb_y.addItems(columns)
        if prev in columns:
            self.cb_y.setCurrentText(prev)
        self.cb_y.blockSignals(False)


class ViewportIndicator(QWidget):
    """
    放置在左侧控制面板内的全局位置指示条。
    """
    manual_interact = Signal()
    
    def __init__(self, wf, cfg=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(12)
        self._wf = wf
        self._cfg = cfg
        self._plot_item = wf.plot_item
        self._plot_item.vb.sigXRangeChanged.connect(self.update)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("左键拖拽平移\n滚轮缩放 / 右键拖拽缩放")
        self._dragging = False
        self._dragging_zoom = False
        self._drag_start_mx = 0.0
        self._drag_start_center_x = 0.0
        self._drag_start_view_width = 0.0
        self._drag_data_range = 1.0
        self._drag_w = 1.0
        self._global_bounds: tuple[float, float] | None = None

    def set_global_bounds(self, xmin: float, xmax: float, buffer_ratio: float = 1.0) -> None:
        if np.isfinite(xmin) and np.isfinite(xmax):
            self._global_bounds = (xmin, xmax)
            self._buffer_ratio = buffer_ratio
        else:
            self._global_bounds = None
        self.update()

    def _get_bounds(self) -> tuple[float, float] | None:
        return self._global_bounds

    def _set_view_center(self, pos_x: float) -> None:
        bounds = self._get_bounds()
        if not bounds: return
        x_min, x_max = bounds
        data_range = x_max - x_min
        if data_range <= 1e-12: return
        
        w = self.width()
        ratio = getattr(self, '_buffer_ratio', 1.0)
        mapped_w = max(1.0, w * ratio)
        offset_px = w - mapped_w
        
        # 将屏幕坐标反向映射到数据中心
        if pos_x < offset_px: pos_x_c = offset_px
        elif pos_x > w: pos_x_c = w
        else: pos_x_c = pos_x
        
        center_x = x_min + ((pos_x_c - offset_px) / mapped_w) * data_range
        v_rect = self._plot_item.vb.viewRect()
        half_w = v_rect.width() / 2
        
        self._plot_item.vb.setXRange(center_x - half_w, center_x + half_w, padding=0)

    def mousePressEvent(self, event) -> None:
        bounds = self._get_bounds()
        if not bounds: return
        x_min, x_max = bounds
        data_range = x_max - x_min
        if data_range <= 1e-12: return
        
        w = self.width()
        ratio = getattr(self, '_buffer_ratio', 1.0)
        mapped_w = max(1.0, w * ratio)
        offset_px = w - mapped_w
        
        self._drag_data_range = data_range
        self._drag_w = mapped_w
        self._drag_offset_px = offset_px
        
        v_rect = self._plot_item.vb.viewRect()
        v_min, v_max = v_rect.left(), v_rect.right()
        
        v_min_c = max(x_min, min(x_max, v_min))
        v_max_c = max(x_min, min(x_max, v_max))
        
        start_px = offset_px + (v_min_c - x_min) / data_range * mapped_w
        end_px = offset_px + (v_max_c - x_min) / data_range * mapped_w
        
        mx = event.position().x()
        
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            if start_px <= mx <= end_px:
                self._drag_start_mx = mx
                self._drag_start_center_x = (v_min + v_max) / 2
            else:
                self._set_view_center(mx)
                if mx < offset_px: mx_c = offset_px
                elif mx > w: mx_c = w
                else: mx_c = mx
                self._drag_start_mx = mx_c
                self._drag_start_center_x = x_min + ((mx_c - offset_px) / mapped_w) * data_range
        elif event.button() == Qt.MouseButton.RightButton:
            self._dragging_zoom = True
            self._drag_start_mx = mx
            self._drag_start_view_width = v_rect.width()
            self._drag_start_center_x = (v_min + v_max) / 2

    def mouseMoveEvent(self, event) -> None:
        if not getattr(self, '_dragging', False) and not getattr(self, '_dragging_zoom', False):
            return
            
        mx = event.position().x()
        delta_px = mx - self._drag_start_mx
        
        if self._dragging:
            delta_data = (delta_px / self._drag_w) * self._drag_data_range
            new_center = self._drag_start_center_x + delta_data
            
            v_rect = self._plot_item.vb.viewRect()
            half_w = v_rect.width() / 2
            self._plot_item.vb.setXRange(new_center - half_w, new_center + half_w, padding=0)
            self.manual_interact.emit()
            
        elif self._dragging_zoom:
            scale = 2.0 ** (-delta_px / 100.0)
            new_width = self._drag_start_view_width * scale
            if new_width < 1e-9: new_width = 1e-9
            
            is_tracking = not getattr(self._cfg, 'is_user_dragging', False) if hasattr(self, '_cfg') and self._cfg else False
            
            if is_tracking:
                right_edge = self._drag_start_center_x + self._drag_start_view_width / 2
                self._plot_item.vb.setXRange(right_edge - new_width, right_edge, padding=0)
            else:
                half_w = new_width / 2
                new_center = self._drag_start_center_x
                self._plot_item.vb.setXRange(new_center - half_w, new_center + half_w, padding=0)
                self.manual_interact.emit()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
        elif event.button() == Qt.MouseButton.RightButton:
            self._dragging_zoom = False

    def wheelEvent(self, event) -> None:
        bounds = self._get_bounds()
        if not bounds: return
        
        delta = event.angleDelta().y()
        if delta == 0: return
        
        scale_factor = 0.85 if delta > 0 else 1.0 / 0.85
        
        v_rect = self._plot_item.vb.viewRect()
        v_left = v_rect.left()
        v_right = v_rect.right()
        new_width = v_rect.width() * scale_factor
        
        is_tracking = not getattr(self._cfg, 'is_user_dragging', False) if hasattr(self, '_cfg') and self._cfg else False
        
        if is_tracking:
            self._plot_item.vb.setXRange(v_right - new_width, v_right, padding=0)
        else:
            v_center = (v_left + v_right) / 2
            self._plot_item.vb.setXRange(v_center - new_width / 2, v_center + new_width / 2, padding=0)
            self.manual_interact.emit()
            
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        
        w = self.width()
        h = self.height()
        
        ratio = getattr(self, '_buffer_ratio', 1.0)
        mapped_w = max(1.0, w * ratio)
        offset_px = w - mapped_w
        
        # 画底层的细线（代表缓冲区的最大容量）
        line_h = 2
        bg_color = QColor(100, 100, 100, 80)
        painter.fillRect(0, (h - line_h) // 2, w, line_h, bg_color)
        
        # 画缓冲区使用量指示（透明背景块，固定在最右侧，向左生长）
        usage_color = QColor(150, 150, 150, 60)
        usage_h = 6
        painter.fillRect(int(offset_px), (h - usage_h) // 2, int(mapped_w), usage_h, usage_color)
        
        bounds = self._get_bounds()
        if not bounds: return
        x_min, x_max = bounds
        data_range = x_max - x_min
        if data_range <= 1e-12: return
            
        v_min, v_max = self._plot_item.vb.viewRect().left(), self._plot_item.vb.viewRect().right()
        v_min_c = max(x_min, min(x_max, v_min))
        v_max_c = max(x_min, min(x_max, v_max))
        
        start_px = int(offset_px + (v_min_c - x_min) / data_range * mapped_w)
        end_px = int(offset_px + (v_max_c - x_min) / data_range * mapped_w)
        bar_w = max(3, end_px - start_px)
        
        # 画上层较厚的色带（当前视口）
        band_h = 6
        band_color = QColor(80, 160, 255, 220)
        painter.fillRect(start_px, (h - band_h) // 2, bar_w, band_h, band_color)


# ==============================================================================
# 通道控制面板（v3.0: 联动组下拉菜单替代复选框）
# ==============================================================================
class ChannelControlPanel(QGroupBox):
    """单个波形窗口的控制面板。"""

    config_changed = Signal(int)
    link_changed = Signal(int)
    delete_requested = Signal(int)
    auto_requested = Signal(int)

    def __init__(self, channel_id: int, config: ChannelConfig,
                 columns: list[str], parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self._ch_id: int = channel_id
        self._cfg: ChannelConfig = config
        self._columns: list[str] = columns
        self._curve_widgets: list[CurveControlWidget] = []
        self._curve_wraps: dict[CurveControlWidget, QHBoxLayout] = {}
        
        # 移除有缺陷的定时器逻辑，依靠 pyqtgraph 原生自动缩放
        self._setup_ui()

    def _setup_ui(self) -> None:
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 4, 8, 8)

        # 窗口名称
        nr = QHBoxLayout()
        nr.addWidget(QLabel("窗口名称:"))
        self.edit_name = QLineEdit(self._cfg.window_name)
        self.edit_name.setToolTip("自定义波形窗口名称")
        self.edit_name.textChanged.connect(self._on_name_changed)
        nr.addWidget(self.edit_name, 1)

        self.chk_auto_y = QCheckBox("自适应 Y 轴")
        self.chk_auto_y.setChecked(self._cfg.auto_scale_y)
        self.chk_auto_y.setToolTip("开启后 Y 轴会根据当前视口内的 X 轴数据自动缩放")
        self.chk_auto_y.toggled.connect(self._on_auto_y_toggled)
        nr.addWidget(self.chk_auto_y)

        layout.addLayout(nr)

        layout.addWidget(self._h_sep())

        # X 轴与联动组（同一行）
        rx = QHBoxLayout()
        rx.addWidget(QLabel("X 轴:"))
        self.cb_x = QComboBox()
        self.cb_x.setMinimumWidth(50)
        self.cb_x.addItems(self._columns)
        if self._cfg.x_column and self._cfg.x_column in self._columns:
            self.cb_x.setCurrentText(self._cfg.x_column)
        self.cb_x.currentTextChanged.connect(self._emit_cfg)
        rx.addWidget(self.cb_x, 1)

        rx.addWidget(QLabel("X联动:"))
        self.cb_lx = QComboBox()
        self.cb_lx.addItems(LINK_GROUP_NAMES)
        self.cb_lx.setCurrentIndex(min(self._cfg.link_group_x, len(LINK_GROUP_NAMES) - 1))
        self.cb_lx.setToolTip("将 X 轴与同组窗口绑定\n同一组内共享 X 轴缩放/平移")
        self.cb_lx.currentIndexChanged.connect(self._on_link)
        rx.addWidget(self.cb_lx)

        rx.addWidget(QLabel("Y联动:"))
        self.cb_ly = QComboBox()
        self.cb_ly.addItems(LINK_GROUP_NAMES)
        self.cb_ly.setCurrentIndex(min(self._cfg.link_group_y, len(LINK_GROUP_NAMES) - 1))
        self.cb_ly.setToolTip("将 Y 轴与同组窗口绑定\n同一组内共享 Y 轴缩放/平移")
        self.cb_ly.currentIndexChanged.connect(self._on_link)
        rx.addWidget(self.cb_ly)
        
        layout.addLayout(rx)

        layout.addWidget(self._h_sep())

        # 曲线容器
        self._curve_layout = QVBoxLayout()
        self._curve_layout.setSpacing(4)
        layout.addLayout(self._curve_layout)
        for cc in self._cfg.curves:
            self._add_curve_widget(cc)

        # 按钮
        btn_row = QHBoxLayout()
        self.btn_ac = QPushButton("➕ 添加曲线")
        self.btn_ac.clicked.connect(self._on_add_curve)
        self.btn_ac.setObjectName("btn_add_curve")
        btn_row.addWidget(self.btn_ac, 1)

        self.btn_auto = QPushButton("🔄 Auto")
        self.btn_auto.setToolTip("自动缩放视口以适应数据")
        self.btn_auto.setObjectName("btn_auto")
        self.btn_auto.clicked.connect(lambda: self.auto_requested.emit(self._ch_id))
        btn_row.addWidget(self.btn_auto)

        self.btn_del_ch = QPushButton("🗑 删除")
        self.btn_del_ch.setToolTip("从面板中移除此波形窗口")
        self.btn_del_ch.setObjectName("btn_del_ch")
        self.btn_del_ch.clicked.connect(lambda: self.delete_requested.emit(self._ch_id))
        btn_row.addWidget(self.btn_del_ch)
        layout.addLayout(btn_row)

    # ── 曲线管理 ──

    def _add_curve_widget(self, cc: CurveConfig) -> CurveControlWidget:
        w = CurveControlWidget(cc, self._columns, self)
        w.config_changed.connect(self._emit_cfg)
        w.delete_requested.connect(lambda: self._on_del_curve(w))
        self._curve_widgets.append(w)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(2)
        lbl = QLabel(f"Y{cc.curve_index + 1}")
        lbl.setFixedWidth(14)
        lbl.setStyleSheet("color:#888; font-size:11px; font-weight:bold;")
        wrap.addWidget(lbl)
        wrap.addWidget(w, 1)
        self._curve_layout.addLayout(wrap)
        self._curve_wraps[w] = wrap
        return w

    def add_indicator(self, wf) -> None:
        self._wf = wf
        self._vb = wf.plot_item.vb
        self.indicator = ViewportIndicator(wf, self._cfg)
        self.layout().addWidget(self.indicator)

        def _init_auto_range():
            if not getattr(self, '_wf', None): return
            for vb in wf._vbs.values():
                vb.setAutoVisible(x=False, y=True)
                if self._cfg.auto_scale_y:
                    vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
                else:
                    vb.disableAutoRange(axis=pg.ViewBox.YAxis)

        # 延迟初始化，避免在 ViewBox 尺寸为 0x0 时触发 autoRange 导致内部矩阵 NaN 进而永久失效
        QTimer.singleShot(100, _init_auto_range)

        self._vb.sigYRangeChanged.connect(self._on_y_range_changed)

    def cleanup(self) -> None:
        """断开所有信号连接防止内存泄漏。"""
        try:
            if getattr(self, '_vb', None) is not None:
                self._vb.sigYRangeChanged.disconnect(self._on_y_range_changed)
        except Exception:
            pass

    @Slot()
    def _on_y_range_changed(self) -> None:
        if getattr(self, '_wf', None) is None:
            return
            
        # 检查主 ViewBox 的原生 autoRange 状态，如果被用户交互关闭了则同步 UI
        is_auto = self._vb.state['autoRange'][1]
        if not is_auto and self._cfg.auto_scale_y:
            self.chk_auto_y.blockSignals(True)
            self.chk_auto_y.setChecked(False)
            self._cfg.auto_scale_y = False
            self.chk_auto_y.blockSignals(False)

    @Slot(bool)
    def _on_auto_y_toggled(self, checked: bool) -> None:
        self._cfg.auto_scale_y = checked
        if getattr(self, '_wf', None) is None:
            return
        for vb in self._wf._vbs.values():
            if checked:
                vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            else:
                vb.disableAutoRange(axis=pg.ViewBox.YAxis)

    @Slot()
    def _on_add_curve(self) -> None:
        ni = len(self._cfg.curves)
        used_seeds = {c.color_seed for c in self._cfg.curves}
        offset = 0
        while (self._ch_id * 10 + offset) in used_seeds:
            offset += 1
        seed = self._ch_id * 10 + offset
        cc = CurveConfig(ni, seed)
        used = {c.y_column for c in self._cfg.curves}
        for col in self._columns:
            if col not in used and col != self._cfg.x_column:
                cc.y_column = col; break
        if not cc.y_column and self._columns:
            cc.y_column = self._columns[-1]
        self._cfg.curves.append(cc)
        self._add_curve_widget(cc)
        self._emit_cfg()
        
        # 延迟触发 Auto，修复添加曲线导致布局抖动产生的 pyqtgraph NaN 矩阵 bug
        QTimer.singleShot(100, lambda: self.auto_requested.emit(self._ch_id))

    def _on_del_curve(self, widget: CurveControlWidget) -> None:
        if len(self._cfg.curves) <= 1:
            QMessageBox.information(self, "提示",
                                    "每个窗口至少保留一条曲线。\n若要移除整个窗口请点击「删除此窗口」。")
            return
        idx = self._curve_widgets.index(widget)
        wrap = self._curve_wraps.pop(widget, None)
        if wrap:
            self._clear_layout(wrap)
            self._curve_layout.removeItem(wrap)
        self._curve_widgets.remove(widget)
        widget.deleteLater()
        del self._cfg.curves[idx]
        for i, c in enumerate(self._cfg.curves):
            c.curve_index = i
        self._emit_cfg()
        
        QTimer.singleShot(100, lambda: self.auto_requested.emit(self._ch_id))

    # ── 槽 ──
    @Slot(str)
    def _on_name_changed(self, text: str) -> None:
        if text.strip():
            self._cfg.window_name = text.strip()
            self.config_changed.emit(self._ch_id)

    @Slot()
    def _emit_cfg(self, *_args) -> None:
        self._cfg.x_column = self.cb_x.currentText()
        self.config_changed.emit(self._ch_id)

    @Slot(int)
    def _on_link(self, _idx: int) -> None:
        self._cfg.link_group_x = self.cb_lx.currentIndex()
        self._cfg.link_group_y = self.cb_ly.currentIndex()
        self.link_changed.emit(self._ch_id)

    @staticmethod
    def _h_sep() -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine); f.setFrameShadow(QFrame.Shadow.Sunken)
        return f

    @staticmethod
    def _clear_layout(layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def update_columns(self, columns: list[str]) -> None:
        self._columns = columns
        prev = self.cb_x.currentText()
        self.cb_x.blockSignals(True); self.cb_x.clear(); self.cb_x.addItems(columns)
        if prev in columns: self.cb_x.setCurrentText(prev)
        self.cb_x.blockSignals(False)
        for cw in self._curve_widgets:
            cw.update_columns(columns)


class LShapeAxis(pg.AxisItem):
    """刻度比默认密一倍的坐标轴，左轴最小宽度 24px。"""

    AXIS_MIN_SIZE = 24  # Y 轴最小宽度（与 X 轴底部距离对齐）

    def _updateWidth(self):
        """重写：在 pyqtgraph 计算完宽度后，强制保证左轴不小于 AXIS_MIN_SIZE。"""
        super()._updateWidth()
        if self.orientation == 'left' and self.fixedWidth is None:
            current = self.maximumWidth()
            if current < self.AXIS_MIN_SIZE:
                self.setMaximumWidth(self.AXIS_MIN_SIZE)
                self.setMinimumWidth(self.AXIS_MIN_SIZE)

    def tickValues(self, minVal, maxVal, size):
        """在默认刻度的每个间距中间额外插入一个刻度。"""
        ticks = super().tickValues(minVal, maxVal, size)
        result = []
        for spacing, vals in ticks:
            if spacing > 0 and len(vals) >= 2:
                half = spacing / 2.0
                # 在首尾各扩展一个间距，确保覆盖完整范围
                v0 = vals[0] - spacing
                vn = vals[-1] + spacing
                n = int(np.ceil((vn - v0) / half)) + 1
                fine_vals = np.round(v0 + np.arange(n) * half, decimals=10).tolist()
                result.append((half, fine_vals))
            else:
                result.append((spacing, vals))
        return result


class WaveformPlot:
    """
    支持多纵坐标（多 ViewBox）的波形绘制器。
    默认主轴在左侧，副轴分布在右侧和两侧外围。
    """

    SCATTER_SCALE = 4.0

    def __init__(self, plot_item: pg.PlotItem) -> None:
        self.plot_item: pg.PlotItem = plot_item
        self._items: dict[int, pg.PlotDataItem] = {}
        
        self.plot_item.showGrid(x=False, y=False)
        self.plot_item.setTitle(None)
        if self.plot_item.legend is not None:
            self.plot_item.legend.hide()
            
        self.plot_item.showAxis('top', False)
        self.plot_item.getAxis('left').setLabel('')
        self.plot_item.getAxis('bottom').setLabel('')
        # 多轴管理
        self._vbs: dict[int, pg.ViewBox] = {0: self.plot_item.vb}
        self.plot_item.vb._needs_initial_y_range = True
        self._axes: dict[int, pg.AxisItem] = {0: self.plot_item.getAxis('left')}
        
        # 劫持主 ViewBox 的 autoRange，使其能同时作用于所有多轴
        self._orig_autoRange = self.plot_item.vb.autoRange
        self.plot_item.vb.autoRange = self._custom_autoRange
        
        self.plot_item.vb.sigResized.connect(self._on_main_vb_resized)
        self.plot_item.vb.sigYRangeChanged.connect(self._on_main_vb_resized)

    def _custom_autoRange(self, *args, **kwargs) -> None:
        """拦截主轴的 autoRange（包括左下角 A 按钮和右键菜单 View All），应用到所有多轴"""
        self._orig_autoRange(*args, **kwargs)
        for aidx, vb in self._vbs.items():
            if aidx != 0:
                vb.autoRange(*args, **kwargs)

    @Slot()
    def _on_main_vb_resized(self) -> None:
        rect = self.plot_item.vb.sceneBoundingRect()
        # 所有辅助 ViewBox 对齐主 ViewBox 几何
        for aidx, vb in self._vbs.items():
            if aidx != 0:
                vb.setGeometry(rect)
                vb.linkedViewChanged(self.plot_item.vb, vb.XAxis)

        # 手动定位 scene 中的辅助轴（aidx=1,2,4,5），不依赖 layout
        self._position_aux_axes(rect)

    def _position_aux_axes(self, rect) -> None:
        """将 scene 中的辅助轴放在主 ViewBox 左右两侧，依次向外排列，并动态调整边距防止遮挡。"""
        AXIS_MIN_W = 30
        total_left_w = 0
        total_right_w = 0

        # ── 左辅助轴 (aidx=1,2)：紧贴主左轴左侧依次向左排 ──
        left_aidx = sorted([a for a in self._axes if a in (1, 2) and self._axes[a].isVisible()])
        main_left_ax = self.plot_item.getAxis('left')
        # 以主左轴 scene 左边界为基准
        try:
            ml_rect = main_left_ax.sceneBoundingRect()
            left_edge = ml_rect.left() if ml_rect.width() > 0 else rect.left() - 36
        except Exception:
            left_edge = rect.left() - 36
            
        current_left = left_edge
        for aidx in left_aidx:
            ax = self._axes[aidx]
            w = max(int(ax.effectiveSizeHint(Qt.SizeHint.PreferredSize).width()), AXIS_MIN_W)
            current_left -= w
            ax.setGeometry(current_left, rect.top(), w, rect.height())
            ax.update()
            total_left_w += w

        # ── 右辅助轴 (aidx=4,5)：从内建右轴/ViewBox 右边界向右依次排 ──
        right_aidx = sorted([a for a in self._axes if a >= 4 and self._axes[a].isVisible()])
        right_edge = rect.right()
        if 3 in self._axes:
            try:
                rax = self._axes[3]
                if rax.isVisible():
                    r = rax.sceneBoundingRect()
                    if r.width() > 0:
                        right_edge = r.right()
            except Exception:
                pass
                
        current_right = right_edge
        for aidx in right_aidx:
            ax = self._axes[aidx]
            w = max(int(ax.effectiveSizeHint(Qt.SizeHint.PreferredSize).width()), AXIS_MIN_W)
            ax.setGeometry(current_right, rect.top(), w, rect.height())
            current_right += w
            ax.update()
            total_right_w += w

        # 动态调整 PlotItem 的边距，确保额外的辅助轴不会被窗口边界裁切（遮挡）
        # 基础边距：左 15, 右 25
        new_left = 15 + total_left_w
        new_right = 25 + total_right_w
        left, top, right, bottom = self.plot_item.layout.getContentsMargins()
        if left != new_left or right != new_right:
            self.plot_item.layout.setContentsMargins(new_left, 15, new_right, 15)

    def _ensure_axis(self, aidx: int, cc: CurveConfig) -> None:
        if aidx in self._vbs:
            return

        vb = pg.ViewBox()
        vb._needs_initial_y_range = True
        vb.setZValue(100)
        self.plot_item.scene().addItem(vb)
        vb.setXLink(self.plot_item.vb)
        vb.sigYRangeChanged.connect(self._on_main_vb_resized)

        if aidx == 3:
            # 右轴 1：使用内建右轴（在 layout 中，pyqtgraph 原生管理）
            self.plot_item.showAxis('right', True)
            ax = self.plot_item.getAxis('right')
        elif aidx in (1, 2):
            # 左轴 2/3：直接放 scene，手动定位到主左轴左侧
            ax = pg.AxisItem('left')
            ax.setZValue(50)
            self.plot_item.scene().addItem(ax)
        elif aidx >= 4:
            # 右轴 2/3：直接放 scene，手动定位到内建右轴右侧
            ax = pg.AxisItem('right')
            ax.setZValue(50)
            self.plot_item.scene().addItem(ax)
        else:
            return

        ax.linkToView(vb)
        self._vbs[aidx] = vb
        self._axes[aidx] = ax

        base = QColor(cc.color)
        base.setAlpha(255)
        ax.setPen(base)
        ax.setTextPen(base)

        self._on_main_vb_resized()

    def set_curve_data(self, cc: CurveConfig, x_data: np.ndarray,
                       y_data: np.ndarray) -> None:
        ci = cc.curve_index
        aidx = cc.axis_index
        
        if len(x_data) == 0 or len(y_data) == 0:
            return

        ml = min(len(x_data), len(y_data))
        # 性能优化：仅当数据是底层环形缓冲区的 View 时（未满状态）才强制拷贝，满状态的 numpy slice 本身就是独立内存，直接复用以节约 50% 内存带宽
        x = x_data[:ml].copy() if not x_data.flags.owndata else x_data[:ml]
        y = y_data[:ml].copy() if not y_data.flags.owndata else y_data[:ml]
        
        mask = np.isfinite(x) & np.isfinite(y)
        x, y = x[mask], y[mask]
        if len(x) == 0:
            return

        self._ensure_axis(aidx, cc)

        # 使用缓存的 pen/brush，避免每帧重建 QColor/QPen/QBrush
        pen = cc.get_pen()
        brush = cc.get_brush()
        sw = cc.stroke_width

        switched_vb = False  # 标记是否切换了 ViewBox
        if ci not in self._items:
            it = pg.PlotDataItem()
            it.setDownsampling(auto=True, method='peak')
            it.setClipToView(True)
            self._items[ci] = it
            self._vbs[aidx].addItem(it)
            switched_vb = True
        else:
            it = self._items[ci]
            old_vb = it.getViewBox()
            if old_vb != self._vbs[aidx]:
                try:
                    if old_vb is not None:
                        old_vb.removeItem(it)
                    else:
                        self.plot_item.removeItem(it)
                except Exception:
                    pass
                
                # 创建新的 PlotDataItem 以避免 pyqtgraph 在不同 ViewBox 间移动 Item 时产生的底层 Bug
                it = pg.PlotDataItem()
                it.setDownsampling(auto=True, method='peak')
                it.setClipToView(True)
                self._items[ci] = it
                self._vbs[aidx].addItem(it)
                switched_vb = True

        # 先设置数据，再做自动缩放
        if cc.display_mode == 'line':
            it.setData(x=x, y=y)
            it.setPen(pen)
            it.setSymbol(None)
        elif cc.display_mode == 'scatter':
            it.setData(x=x, y=y)
            it.setPen(None)
            it.setSymbol('o')
            it.setSymbolSize(sw * self.SCATTER_SCALE)
            it.setSymbolBrush(brush)
            it.setSymbolPen(None)
        elif cc.display_mode == 'both':
            it.setData(x=x, y=y)
            it.setPen(pen)
            it.setSymbol('o')
            it.setSymbolSize(sw * self.SCATTER_SCALE)
            it.setSymbolBrush(brush)
            it.setSymbolPen(None)

        it.setVisible(getattr(cc, 'visible', True))

        # 数据变更检测
        y_column_changed = getattr(cc, '_last_y_column', None) != cc.y_column
        if y_column_changed:
            cc._last_y_column = cc.y_column

        # 处理 Y 轴自动缩放（基于当前可见的 X 范围）
        needs_y_auto = switched_vb or y_column_changed or getattr(self._vbs[aidx], '_needs_initial_y_range', False)
        if needs_y_auto:
            was_auto = self._vbs[aidx].state['autoRange'][1]
            self._vbs[aidx].enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            self._vbs[aidx].updateAutoRange()
            if not was_auto:
                self._vbs[aidx].disableAutoRange(axis=pg.ViewBox.YAxis)
            self._vbs[aidx]._needs_initial_y_range = False

        # 切换 ViewBox 后强制刷新，确保曲线立即可见
        if switched_vb:
            self._vbs[aidx].update()

    def sync_curves(self, curves: list[CurveConfig], fg: str) -> None:
        """移除已删除曲线，autoRange。更新多轴颜色。"""
        active = {cc.curve_index for cc in curves if cc.y_column}
        for ci in set(self._items.keys()) - active:
            self._remove(ci)

        active_curves = [cc for cc in curves if cc.y_column]
        active_aidx = {cc.axis_index for cc in active_curves}
        is_single_axis = len(active_aidx) == 1

        # 计算布局键：仅当可见轴集合变化时才重新计算 scene 轴位置
        layout_key = tuple(sorted(active_aidx))
        layout_changed = (layout_key != getattr(self, '_last_layout_key', None))

        for aidx, ax in self._axes.items():
            if aidx not in active_aidx:
                vb = self._vbs.get(aidx)
                if vb:
                    vb._needs_initial_y_range = True

                if aidx == 0:
                    self.plot_item.showAxis('left', False)
                elif aidx == 3:
                    self.plot_item.showAxis('right', False)
                else:
                    ax.hide()
                continue

            if aidx == 0:
                self.plot_item.showAxis('left', True)
            elif aidx == 3:
                self.plot_item.showAxis('right', True)
            else:
                ax.show()
                vb = self._vbs.get(aidx)
                if vb and 0 in self._vbs:
                    vb.setXLink(self._vbs[0])  # 副轴 X 永远跟随主轴

            if is_single_axis:
                ax.setPen(fg)
                ax.setTextPen(fg)
            else:
                # 使用该轴第一条曲线的颜色
                first_cc = next((cc for cc in active_curves if cc.axis_index == aidx), None)
                if first_cc:
                    c = QColor(first_cc.color)
                    c.setAlpha(255)
                    ax.setPen(c)
                    ax.setTextPen(c)
                else:
                    ax.setPen(fg)
                    ax.setTextPen(fg)

        # 仅在轴布局变化时重新计算 scene 轴位置（sceneBoundingRect 成本高）
        if layout_changed:
            rect = self.plot_item.vb.sceneBoundingRect()
            self._position_aux_axes(rect)
            self._last_layout_key = layout_key

    def _remove(self, ci: int) -> None:
        if ci in self._items:
            it = self._items[ci]
            vb = it.getViewBox()
            if vb is not None:
                vb.removeItem(it)
            else:
                self.plot_item.removeItem(it)
            del self._items[ci]

    def clear_all(self) -> None:
        for ci in list(self._items.keys()):
            self._remove(ci)
        self._items.clear()


# 主窗口（v3.5: 右击固定光标 + 吸附数据点 + 丰富窗口布局）
# ==============================================================================
class MainWindow(QMainWindow):
    """
    WaveScope 主窗口 v3.5。

    新特性：
      · 波形窗口使用 QMdiArea 承载，可自由拖拽/缩放/排列（无吸附，不挤占控制面板）
      · 菜单栏 → 光标 → 十字追踪光标 + 多窗口同步
      · 联动组下拉（组 A~D）支持多套独立 X/Y 轴绑定
      · 线径参数统一控制线宽和散点大小，默认点线结合模式
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("WaveScope — 传感器数据波形分析")
        self.resize(1200, 750)
        # 窗口居中
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - 1200) // 2, (screen.height() - 750) // 2)

        self.data_center: DataCenter = DataCenter()

        self._channels: dict[int, ChannelConfig] = {}
        self._channel_panels: dict[int, ChannelControlPanel] = {}
        self._waveform_plots: dict[int, WaveformPlot] = {}
        self._plot_widgets: dict[int, pg.PlotWidget] = {}
        self._channel_subs: dict[int, QMdiSubWindow] = {}
        self._channel_order: list[int] = []
        self._next_channel_id: int = 0
        self._cursor_mgr: CursorManager = CursorManager()
        
        self.live_center = LiveCenter()
        self.current_mode = 'offline'
        self.receiver = None
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._on_live_update)
        # v3.5.1: 存储 x 轴反向联动连接，用于清理
        self._x_link_reverse_connections: list[tuple] = []

        self._setup_ui()
        self._setup_menu()
        pass
        self.setAcceptDrops(True)



    def resizeEvent(self, event) -> None:
        """窗口大小变化时自动重新智能布局"""
        super().resizeEvent(event)
        if self._plot_widgets:
            self._smart_layout()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if urls and urls[0].toLocalFile().lower().endswith('.csv'):
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            if file_path.lower().endswith('.csv'):
                self._load_csv_file(file_path)
                event.acceptProposedAction()

    # ══════════════════════════════════════════════════════════════════════
    # 菜单栏
    # ══════════════════════════════════════════════════════════════════════

    def _setup_menu(self) -> None:
        mb = self.menuBar()

        # ── 模式切换菜单 ──
        mode_menu = mb.addMenu("模式")
        
        self.act_offline = QAction("📂 离线 CSV 模式", self)
        self.act_offline.setCheckable(True)
        self.act_offline.setChecked(True)
        self.act_offline.triggered.connect(lambda: self._set_mode('offline'))
        
        self.act_live = QAction("📡 动态接收模式", self)
        self.act_live.setCheckable(True)
        self.act_live.triggered.connect(lambda: self._set_mode('live'))
        
        mode_menu.addAction(self.act_offline)
        mode_menu.addAction(self.act_live)

        # ── 窗口布局菜单（丰富预定义布局） ──
        lm = mb.addMenu("窗口布局")

        act_smart = QAction("✨ 智能布局", self)
        act_smart.setToolTip("根据当前窗口数量自动选择最佳布局")
        act_smart.triggered.connect(self._smart_layout); lm.addAction(act_smart)
        lm.addSeparator()

        act_tile = QAction("▦ 平铺（自动网格）", self)
        act_tile.triggered.connect(self._tile_windows); lm.addAction(act_tile)
        act_cas = QAction("▣ 层叠", self)
        act_cas.triggered.connect(self._cascade_windows); lm.addAction(act_cas)
        lm.addSeparator()

        act_lr = QAction("⬌ 左右并排", self)
        act_lr.triggered.connect(self._split_left_right); lm.addAction(act_lr)
        act_tb = QAction("⬆ 上下并排", self)
        act_tb.triggered.connect(self._split_top_bottom); lm.addAction(act_tb)
        lm.addSeparator()

        act_1l2r = QAction("左 1 右 2", self)
        act_1l2r.setToolTip("左侧一个大窗口 + 右侧两个上下堆叠")
        act_1l2r.triggered.connect(lambda: self._layout_lr(1, 2)); lm.addAction(act_1l2r)
        act_2l1r = QAction("左 2 右 1", self)
        act_2l1r.triggered.connect(lambda: self._layout_lr(2, 1)); lm.addAction(act_2l1r)
        act_1l3r = QAction("左 1 右 3", self)
        act_1l3r.triggered.connect(lambda: self._layout_lr(1, 3)); lm.addAction(act_1l3r)
        lm.addSeparator()

        act_1t2b = QAction("上 1 下 2", self)
        act_1t2b.triggered.connect(lambda: self._layout_tb(1, 2)); lm.addAction(act_1t2b)
        act_2t1b = QAction("上 2 下 1", self)
        act_2t1b.triggered.connect(lambda: self._layout_tb(2, 1)); lm.addAction(act_2t1b)
        lm.addSeparator()

        act_2x2 = QAction("2x2 四宫格", self)
        act_2x2.triggered.connect(lambda: self._layout_grid(2, 2)); lm.addAction(act_2x2)
        act_2x3 = QAction("2x3 六宫格", self)
        act_2x3.triggered.connect(lambda: self._layout_grid(2, 3)); lm.addAction(act_2x3)
        act_3x2 = QAction("3x2 六宫格", self)
        act_3x2.triggered.connect(lambda: self._layout_grid(3, 2)); lm.addAction(act_3x2)
        act_3x3 = QAction("3x3 九宫格", self)
        act_3x3.triggered.connect(lambda: self._layout_grid(3, 3)); lm.addAction(act_3x3)
        lm.addSeparator()
        act_4x4 = QAction("4x4 十六宫格", self)
        act_4x4.triggered.connect(lambda: self._layout_grid(4, 4)); lm.addAction(act_4x4)
        lm.addSeparator()

        act_row = QAction("⬌ 单行均分", self)
        act_row.setToolTip("所有窗口排成一行，等宽均分")
        act_row.triggered.connect(self._layout_row); lm.addAction(act_row)
        act_col = QAction("⬆ 单列均分", self)
        act_col.setToolTip("所有窗口排成一列，等高均分")
        act_col.triggered.connect(self._layout_column); lm.addAction(act_col)

        # ── 视图菜单 ──
        vm = mb.addMenu("视图")

        self.act_grid = QAction("显示网格", self)
        self.act_grid.setCheckable(True); self.act_grid.setChecked(False)
        self.act_grid.setToolTip("显示/隐藏所有波形窗口的背景网格")
        self.act_grid.toggled.connect(self._on_toggle_grid)
        vm.addAction(self.act_grid)

        # ── 光标菜单 ──
        cm = mb.addMenu("光标")

        self.act_cur = QAction("显示光标", self)
        self.act_cur.setCheckable(True); self.act_cur.setChecked(False)
        self.act_cur.setToolTip("开/关十字追踪光标。在波形窗口上按鼠标中键可固定/释放光标")
        self.act_cur.toggled.connect(self._on_toggle_cursor)
        cm.addAction(self.act_cur)

        self.act_snap = QAction("吸附到数据点", self)
        self.act_snap.setCheckable(True); self.act_snap.setChecked(False)
        self.act_snap.setToolTip("光标自动吸附到最近的数据点（阈值 20px 视口距离）")
        self.act_snap.toggled.connect(self._on_toggle_snap_data)
        cm.addAction(self.act_snap)

        cm.addSeparator()

        cm.addSeparator()
        # 坐标标签位置（三选一）
        self._label_mode_group = [None, None, None]
        for i, (label, tip) in enumerate([
            ("坐标：不显示", "隐藏光标坐标值"),
            ("坐标：跟随光标", "坐标显示在光标交叉点右上角"),
            ("坐标：轴侧", "X 值在左贴纵轴，Y 值在下贴横轴"),
        ]):
            act = QAction(label, self)
            act.setCheckable(True); act.setChecked(i == 2)  # 默认轴侧
            act.setToolTip(tip)
            idx = i
            act.toggled.connect(lambda on, n=idx: self._on_label_mode(n) if on else None)
            cm.addAction(act)
            self._label_mode_group[i] = act

        cm.addSeparator()

        self.act_sync = QAction("多窗口同步光标", self)
        self.act_sync.setCheckable(True); self.act_sync.setChecked(False)
        self.act_sync.setToolTip("鼠标在任一窗口移动时所有窗口光标 X 位置同步")
        self.act_sync.toggled.connect(self._on_toggle_cursor_sync)
        cm.addAction(self.act_sync)

        # ── 配色菜单 ──
        tm = mb.addMenu("主题色")

        self._theme_actions: dict[str, QAction] = {}
        
        for t_id, t_info in self.PLOT_THEME.items():
            act = QAction(t_info.get("name", t_id), self)
            act.setCheckable(True)
            act.setChecked(t_id == "dark")
            act.toggled.connect(lambda on, t=t_id: self._apply_theme(t) if on else None)
            tm.addAction(act)
            self._theme_actions[t_id] = act

        self._current_theme: str = "dark"

    # ── 主题切换 ──

    # 波形图深浅色配置
    PLOT_THEME = {
        "dark":  {"bg": "k", "fg": "w", "grid_alpha": 0.3, "name": "默认深色"},
        "light": {"bg": "w", "fg": "k", "grid_alpha": 0.15, "name": "默认浅色"},
        "monokai": {"bg": "#272822", "fg": "#F8F8F2", "grid_alpha": 0.2, "name": "Monokai"},
        "solar_dark": {"bg": "#002B36", "fg": "#93A1A1", "grid_alpha": 0.2, "name": "暗夜青"},
        "solar_light": {"bg": "#FDF6E3", "fg": "#657B83", "grid_alpha": 0.15, "name": "日光白"},
    }

    def _toggle_theme(self) -> None:
        keys = list(self.PLOT_THEME.keys())
        idx = keys.index(self._current_theme) if self._current_theme in keys else 0
        new_theme = keys[(idx + 1) % len(keys)]
        self._apply_theme(new_theme)

    def _apply_theme(self, name: str) -> None:
        """运行时切换浅色/深色主题"""
        if name not in THEMES:
            return
        self._current_theme = name
        app = QApplication.instance()
        if app is None:
            return
        palette, stylesheet = THEMES[name]
        app.setPalette(palette)
        app.setStyleSheet(stylesheet)

        # 同步所有波形图：背景 / 网格 / 轴
        pt = self.PLOT_THEME[name]
        show_grid = self.act_grid.isChecked()
        for cid, pw in self._plot_widgets.items():
            pw.setBackground(pt["bg"])
            pi = pw.getPlotItem()
            pi.showGrid(x=show_grid, y=show_grid, alpha=pt["grid_alpha"])
        self._refresh_all_plots()

        # 同步菜单勾选状态
        for key, act in self._theme_actions.items():
            act.blockSignals(True)
            act.setChecked(key == name)
            act.blockSignals(False)

    @Slot(bool)
    def _on_toggle_grid(self, on: bool) -> None:
        """切换所有窗口的背景网格"""
        alpha = self.PLOT_THEME[self._current_theme]["grid_alpha"]
        for wf in self._waveform_plots.values():
            wf.plot_item.showGrid(x=on, y=on, alpha=alpha)

    @Slot(bool)
    def _on_toggle_cursor(self, on: bool) -> None:
        self._cursor_mgr.set_enabled(on)

    @Slot(bool)
    def _on_toggle_snap_data(self, on: bool) -> None:
        self._cursor_mgr.set_snap_to_data(on)

    def _on_label_mode(self, mode: int) -> None:
        """切换坐标标签显示模式：0=不显示, 1=跟随光标, 2=轴侧"""
        self._cursor_mgr.set_label_mode(mode)
        # 同步其他 radio 按钮状态
        for i, act in enumerate(self._label_mode_group):
            if act is not None:
                act.blockSignals(True)
                act.setChecked(i == mode)
                act.blockSignals(False)
        names = ["坐标已隐藏", "坐标跟随光标", "坐标显示在轴侧"]
        pass

    @Slot(bool)
    def _on_toggle_cursor_sync(self, on: bool) -> None:
        self._cursor_mgr.set_sync_enabled(on)
        pass

    # ══════════════════════════════════════════════════════════════════════
    # UI 构建
    # ══════════════════════════════════════════════════════════════════════

    def _setup_ui(self) -> None:
        # ── QSplitter：左侧控制面板 | 右侧 MDI 绘图区 ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # 左侧：控制面板
        left = self._build_left_panel()
        left.setMinimumWidth(320); left.setMaximumWidth(450)
        splitter.addWidget(left)

        # 右侧：MDI 区域 + 吸附覆盖层
        self._mdi = QMdiArea()
        self._mdi.setBackground(QColor(42, 42, 42))
        self._mdi.setViewMode(QMdiArea.ViewMode.SubWindowView)
        self._mdi.setTabsMovable(True)
        splitter.addWidget(self._mdi)

        splitter.setSizes([320, 880])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def _build_left_panel(self) -> QWidget:
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.Shape.NoFrame)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sc.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        
        self._ch_container = QWidget()
        self._ch_layout = QVBoxLayout(self._ch_container)
        self._ch_layout.setContentsMargins(8, 8, 8, 8); self._ch_layout.setSpacing(8)

        # 全局控制
        gg = QGroupBox("")
        gl = QVBoxLayout(gg); gl.setSpacing(6)

        self.wg_offline = QWidget()
        wl_off = QVBoxLayout(self.wg_offline)
        wl_off.setContentsMargins(0,0,0,0)
        self.btn_open = QPushButton("📂 打开 CSV 文件 ...")
        self.btn_open.setMinimumHeight(38); self.btn_open.clicked.connect(self._on_open_csv)
        wl_off.addWidget(self.btn_open)

        self.lbl_file = QLabel("📄 尚未加载文件")
        self.lbl_file.setWordWrap(True); self.lbl_file.setStyleSheet("color:#aaa; font-size:11px;")
        wl_off.addWidget(self.lbl_file)
        gl.addWidget(self.wg_offline)

        self.wg_live = QWidget()
        wl_live = QVBoxLayout(self.wg_live)
        wl_live.setContentsMargins(0,0,0,0)
        
        fl = QFormLayout()
        self.cb_protocol = QComboBox()
        self.cb_protocol.addItems(["TCP", "Serial"])
        fl.addRow("协议:", self.cb_protocol)
        
        self.lbl_baud_ip = QLabel("IP地址:")
        self.edit_baud = QLineEdit("127.0.0.1")
        fl.addRow(self.lbl_baud_ip, self.edit_baud)
        
        self.lbl_port = QLabel("端口:")
        self.edit_port = QLineEdit("8080")
        fl.addRow(self.lbl_port, self.edit_port)
        
        self.cb_protocol.currentTextChanged.connect(self._on_protocol_changed)
        self.spin_capacity = QSpinBox()
        self.spin_capacity.setRange(1000, 10000000)
        self.spin_capacity.setValue(100000)
        self.spin_capacity.setKeyboardTracking(False)
        self.spin_capacity.valueChanged.connect(self._on_capacity_changed)
        fl.addRow("缓冲点数:", self.spin_capacity)
        wl_live.addLayout(fl)
        
        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("▶ 连接设备")
        self.btn_connect.setMinimumHeight(34)
        self.btn_connect.clicked.connect(self._toggle_connection)
        btn_row.addWidget(self.btn_connect, 7)
        
        self.chk_pause = QPushButton("⏸ 冻结")
        self.chk_pause.setCheckable(True)
        self.chk_pause.setMinimumHeight(34)
        self.chk_pause.setToolTip("暂停画面刷新（此时后台仍在持续接收数据）")
        self.chk_pause.setStyleSheet("""
            QPushButton:checked {
                background-color: #d9534f;
                color: white;
                border: 1px solid #c9302c;
                border-radius: 4px;
            }
        """)
        btn_row.addWidget(self.chk_pause, 3)
        
        self.btn_clear = QPushButton("🗑 清空")
        self.btn_clear.setMinimumHeight(34)
        self.btn_clear.setToolTip("清空当前所有接收到的数据，图表重新开始")
        self.btn_clear.clicked.connect(self._on_clear_live_buffer)
        btn_row.addWidget(self.btn_clear, 3)
        
        wl_live.addLayout(btn_row)
        
        gl.addWidget(self.wg_live)
        self.wg_live.hide()

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet("color:#999; font-size:11px;")
        gl.addWidget(self.lbl_stats)

        gl.addWidget(self._h_sep())

        self.btn_add = QPushButton("➕ 新增波形窗口")
        self.btn_add.setMinimumHeight(38); self.btn_add.clicked.connect(self._on_add_channel)
        gl.addWidget(self.btn_add)

        self._ch_layout.addWidget(gg)
        self._ch_layout.addStretch()

        sc.setWidget(self._ch_container)
        return sc

    @staticmethod
    def _h_sep() -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine); f.setFrameShadow(QFrame.Shadow.Sunken)
        return f

    # ══════════════════════════════════════════════════════════════════════
    # Live Stream 模式操作
    # ══════════════════════════════════════════════════════════════════════

    def _set_mode(self, mode: str) -> None:
        self.current_mode = mode
        if mode == 'offline':
            self.act_offline.setChecked(True)
            self.act_live.setChecked(False)
            self.wg_offline.show()
            self.wg_live.hide()
            self.live_timer.stop()
            if self.receiver: self.receiver.stop()
        else:
            self.act_offline.setChecked(False)
            self.act_live.setChecked(True)
            self.wg_offline.hide()
            self.wg_live.show()

    @Slot(str)
    def _on_protocol_changed(self, protocol: str) -> None:
        if protocol == "TCP":
            self.lbl_baud_ip.setText("IP地址:")
            self.edit_baud.setText("127.0.0.1")
            self.lbl_port.setText("端口:")
            self.edit_port.setText("8080")
        elif protocol == "Serial":
            self.lbl_baud_ip.setText("波特率:")
            self.edit_baud.setText("115200")
            self.lbl_port.setText("串口号:")
            self.edit_port.setText("COM1")

    @Slot()
    def _toggle_connection(self) -> None:
        if self.receiver and self.receiver.running:
            self.receiver.stop()
            self.receiver = None
            self.btn_connect.setText("▶ 连接设备")
            self.btn_connect.setStyleSheet("")
            self.live_timer.stop()
        else:
            mode = self.cb_protocol.currentText().lower()
            port = self.edit_port.text()
            baud_or_ip = self.edit_baud.text()
            self.live_center.reset_capacity(self.spin_capacity.value())
            self.receiver = ReceiverThread(mode, port, baud_or_ip)
            self.receiver.data_received.connect(self.live_center.process_data)
            self.receiver.error_occurred.connect(self._on_connection_error)
            self.receiver.connection_closed.connect(self._on_connection_closed)
            self._auto_add_window_pending = True
            self.receiver.start()
            self.btn_connect.setText("⏹ 断开连接")
            self.btn_connect.setStyleSheet("color: red; font-weight: bold;")
            self.live_timer.start(33)
            
    def _on_connection_error(self, msg: str) -> None:
        QMessageBox.warning(self, "连接错误", msg)
        self._on_connection_closed()
        
    def _on_connection_closed(self) -> None:
        self.btn_connect.setText("▶ 连接设备")
        self.btn_connect.setStyleSheet("")
        self.live_timer.stop()
        if self.receiver:
            self.receiver.running = False
            self.receiver = None

    @Slot()
    def _on_clear_live_buffer(self) -> None:
        self.live_center.clear()
        
    @Slot(int)
    def _on_capacity_changed(self, val: int) -> None:
        self.live_center.reset_capacity(val)
        for cid, cfg in self._channels.items():
            cfg.x_initialized = False
            cfg.y_initialized_after_clear = False
            cfg.last_tracked_x = None
            cfg.is_user_dragging = False
            if cid in self._waveform_plots:
                self._waveform_plots[cid].plot_item.vb.enableAutoRange(axis=pg.ViewBox.XAxis, enable=True)
        self._refresh_all_plots()

    def _on_live_update(self) -> None:
        if self.chk_pause.isChecked():
            return
            
        if self.live_center.columns_changed:
            self.live_center.columns_changed = False
            cols = self.live_center.get_columns()
            self.lbl_stats.setText(f"动态接收中... 已解析通道数: {len(cols)-1}")
            for p in self._channel_panels.values():
                p.update_columns(cols)
                
            if getattr(self, '_auto_add_window_pending', False):
                self._auto_add_window_pending = False
                if len(self._channel_order) == 0:
                    self._on_add_channel()
                
        for cid in self._channel_order:
            self._update_single_plot(cid, live_update=True)

    # ══════════════════════════════════════════════════════════════════════
    # 全局操作
    # ══════════════════════════════════════════════════════════════════════

    @Slot()
    def _on_open_csv(self) -> None:
        fp, _ = QFileDialog.getOpenFileName(self, "选择 CSV 传感器数据文件", "",
                                            "CSV 文件 (*.csv);;所有文件 (*.*)")
        if fp:
            self._load_csv_file(fp)

    def _load_csv_file(self, fp: str) -> None:
        try:
            df = self.data_center.load_csv(fp)
        except ValueError as e:
            QMessageBox.critical(self, "加载失败 — 编码错误", str(e)); return
        except pd.errors.ParserError as e:
            QMessageBox.critical(self, "加载失败 — CSV 解析错误",
                                f"文件格式无法解析:\n{e}"); return
        except pd.errors.EmptyDataError:
            QMessageBox.critical(self, "加载失败", "CSV 文件为空或没有有效数据。"); return
        except (OSError, PermissionError) as e:
            QMessageBox.critical(self, "加载失败 — 文件访问错误",
                                f"无法读取文件:\n{e}"); return
        except Exception as e:
            QMessageBox.critical(self, "加载失败", f"{type(e).__name__}: {e}"); return

        fn = Path(fp).name
        self.lbl_file.setText(f"📄 {fn}")
        self._refresh_columns()
        self._refresh_all_plots()

        # 自动创建第一个波形窗口（若尚无窗口）
        if not self._channel_order:
            self._on_add_channel()


    def _refresh_columns(self) -> None:
        if not self.data_center.is_loaded:
            return
        cols = self.data_center.columns
        rows = self.data_center.row_count
        # 避免 deep=True 带来的大文件性能开销；numeric 列使用 shallow 即可
        mem_mb = (rows * len(cols) * 8) / (1024 * 1024)
        self.lbl_stats.setText(
            f"共 {rows} 行 · {len(cols)} 列  |  "
            f"内存约 {mem_mb:.1f} MiB"
        )
        for p in self._channel_panels.values():
            p.update_columns(cols)

    @Slot()
    def _on_add_channel(self) -> None:
        if self.current_mode == 'live':
            if not self.live_center.is_loaded():
                QMessageBox.information(self, "提示", "尚未接收到有效数据流，请先连接设备并等待数据。"); return
            cols = self.live_center.get_columns()
        else:
            if not self.data_center.is_loaded:
                QMessageBox.information(self, "提示", "请先加载 CSV 文件。"); return
            cols = self.data_center.columns

        cid = self._next_channel_id; self._next_channel_id += 1

        # 配置
        cfg = ChannelConfig(cid)
        if cols: cfg.x_column = cols[0]
        cc = CurveConfig(0, color_seed=cid * 10)
        if len(cols) >= 2: cc.y_column = cols[1]
        cfg.curves.append(cc)
        # 自动命名：用 Y 列名替代「窗口 #N」
        y_names = [c.y_column for c in cfg.curves if c.y_column]
        if y_names:
            cfg.window_name = ", ".join(y_names[:3])
            if len(y_names) > 3:
                cfg.window_name += " …"
        self._channels[cid] = cfg; self._channel_order.append(cid)

        # PlotWidget（L 型坐标轴：左下角，不显示负半轴）
        pw = pg.PlotWidget(axisItems={
            'bottom': LShapeAxis(orientation='bottom'),
            'left': LShapeAxis(orientation='left'),
        })
        bg = self.PLOT_THEME[self._current_theme]["bg"]
        pw.setBackground(bg)
        pw.setStyleSheet("PlotWidget { border:none; }")
        pi = pw.getPlotItem()
        pi.hideButtons()
        
        # 应用当前的网格设置
        show_grid = self.act_grid.isChecked()
        alpha = self.PLOT_THEME[self._current_theme]["grid_alpha"]
        pi.showGrid(x=show_grid, y=show_grid, alpha=alpha)
        
        # 增加图表四周的边距（Boundary），让坐标轴不至于紧贴着窗口边缘
        pi.layout.setContentsMargins(15, 15, 25, 15)
        # Y 轴自适应宽度（LShapeAxis._updateWidth 保证最小 24px）；X 轴固定高度
        pi.getAxis('left').setWidth(None)
        pi.getAxis('bottom').setHeight(24)
        wf = WaveformPlot(pi); self._waveform_plots[cid] = wf
        self._plot_widgets[cid] = pw

        # 注册光标
        self._cursor_mgr.register_plot(cid, wf)

        # QMdiSubWindow — 简洁标题栏（无系统菜单图标、无按钮）
        sub = QMdiSubWindow()
        sub.setWidget(pw)
        sub.setWindowTitle(f"窗口 #{cid + 1}")
        # 去除原生标题栏 — 只保留 PlotItem 内部标题 + 细边框可拖拽
        sub.setWindowFlags(Qt.WindowType.SubWindow | Qt.WindowType.FramelessWindowHint)
        sub.setWindowIcon(QIcon())
        sub.resize(650, 350)
        sub.setMinimumSize(300, 180)
        sub.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        def on_close(event, c=cid):
            self._delete_channel(c)
        sub.closeEvent = on_close

        self._mdi.addSubWindow(sub)
        sub.show()
        self._channel_subs[cid] = sub

        # 新建窗口自动触发智能布局
        self._smart_layout()

        # 控制面板
        panel = ChannelControlPanel(cid, cfg, cols)
        panel.config_changed.connect(self._on_channel_config_changed)
        panel.link_changed.connect(self._on_link_changed)
        panel.delete_requested.connect(self._on_delete_channel)
        panel.auto_requested.connect(self._on_auto_requested)
        panel.add_indicator(wf)
        self._channel_panels[cid] = panel
        idx = self._ch_layout.count() - 1
        self._ch_layout.insertWidget(max(0, idx), panel)
        
        # 绑定显式交互状态（仅在真实鼠标交互时中断自动追踪，忽略 resize 引发的浮点误差）
        # v3.5.1: 联动拖拽——同一 X 组的所有通道都标记为拖拽中，避免主通道自动滚动覆盖联动窗口
        def _on_manual_interact(*args):
            if getattr(cfg, 'x_initialized', False):
                cfg.is_user_dragging = True
                g = cfg.link_group_x
                if g > 0:
                    for ocid in self._channel_order:
                        if ocid != cid:
                            ocfg = self._channels.get(ocid)
                            if ocfg and ocfg.link_group_x == g:
                                ocfg.is_user_dragging = True
                
        wf.plot_item.vb.sigRangeChangedManually.connect(_on_manual_interact)
        panel.indicator.manual_interact.connect(_on_manual_interact)

        self._update_single_plot(cid)
        self._update_axis_linking()


    # ══════════════════════════════════════════════════════════════════════
    # 通道操作
    # ══════════════════════════════════════════════════════════════════════

    @Slot(int)
    def _on_channel_config_changed(self, cid: int) -> None:
        self._update_single_plot(cid)

    @Slot(int)
    def _on_link_changed(self, _cid: int) -> None:
        self._update_axis_linking()

    @Slot(int)
    def _on_delete_channel(self, cid: int) -> None:
        self._delete_channel(cid)

    @Slot(int)
    def _on_auto_requested(self, cid: int) -> None:
        if cid in self._waveform_plots:
            self._waveform_plots[cid].plot_item.getViewBox().autoRange()

    def _delete_channel(self, cid: int) -> None:
        """删除一个波形窗口（控制面板按钮或子窗口关闭按钮触发）"""
        if cid not in self._channels:
            return

        # 面板
        p = self._channel_panels.pop(cid, None)
        if p:
            p.cleanup()
            self._ch_layout.removeWidget(p)
            p.deleteLater()

        # MDI 子窗口
        sub = self._channel_subs.pop(cid, None)
        if sub:
            self._mdi.removeSubWindow(sub)
            sub.closeEvent = lambda e: None
            sub.deleteLater()

        # 光标
        self._cursor_mgr.unregister_plot(cid)

        # 数据
        self._channels.pop(cid, None)
        self._waveform_plots.pop(cid, None)
        self._plot_widgets.pop(cid, None)
        if cid in self._channel_order:
            self._channel_order.remove(cid)

        self._update_axis_linking()


    @Slot()
    def _on_clear_all(self) -> None:
        for cid in list(self._channel_order):
            self._delete_channel(cid)
        self._next_channel_id = 0


    # ══════════════════════════════════════════════════════════════════════
    # 窗口布局（参考 Windows 桌面多窗口管理：Win+←→↑↓ 吸附逻辑）
    # ══════════════════════════════════════════════════════════════════════

    # ── 智能布局（根据窗口数自动选最优排列） ──

    @Slot()
    def _smart_layout(self) -> None:
        """根据当前窗口数量自动选择最佳布局"""
        n = len(self._mdi.subWindowList())
        if n <= 0:
            return
        if n == 1:
            self._tile_windows()          # 占满
        elif n == 2:
            self._split_top_bottom()       # 上下并排
        elif n == 3:
            self._layout_column()          # 上中下三行
        elif n == 4:
            self._layout_grid(2, 2)        # 2×2 四宫格
        elif n in (5, 6):
            self._layout_grid(2, 3)        # 2×3 六宫格
        elif n in (7, 8, 9):
            self._layout_grid(3, 3)        # 3×3 九宫格
        else:
            self._tile_windows()           # 自动网格

    # ── 基础布局 ──

    @Slot()
    def _tile_windows(self) -> None:
        subs = self._mdi.subWindowList()
        if not subs: return
        n = len(subs); area = self._mdi.viewport().rect()
        cols = max(1, int(np.ceil(np.sqrt(n))))
        rows = max(1, int(np.ceil(n / cols)))
        cw, ch = area.width() // cols, area.height() // rows
        for i, sub in enumerate(subs):
            sub.setGeometry((i % cols) * cw, (i // cols) * ch, cw, ch)

    @Slot()
    def _cascade_windows(self) -> None:
        subs = self._mdi.subWindowList()
        if not subs: return
        off, x, y = 30, 0, 0
        for sub in subs:
            sub.setGeometry(x, y, 520, 340); x += off; y += off

    @Slot()
    def _split_left_right(self) -> None:
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect(); n = len(subs)
        cw = area.width() // n
        for i, sub in enumerate(subs):
            sub.setGeometry(i * cw, 0, cw, area.height())

    @Slot()
    def _split_top_bottom(self) -> None:
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect(); n = len(subs)
        ch = area.height() // n
        for i, sub in enumerate(subs):
            sub.setGeometry(0, i * ch, area.width(), ch)

    # ── 复合布局：左 M 右 N ──

    def _layout_lr(self, left_n: int, right_n: int) -> None:
        """左侧 left_n 个窗口堆叠，右侧 right_n 个窗口堆叠"""
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect()
        lw = area.width() // 2
        rw = area.width() - lw
        # 填充左侧
        for i in range(min(left_n, len(subs))):
            lh = area.height() // left_n
            subs[i].setGeometry(0, i * lh, lw, lh)
        # 填充右侧
        for j in range(min(right_n, len(subs) - left_n)):
            rh = area.height() // right_n
            idx = left_n + j
            subs[idx].setGeometry(lw, j * rh, rw, rh)

    # ── 复合布局：上 M 下 N ──

    def _layout_tb(self, top_n: int, bot_n: int) -> None:
        """上方 top_n 个窗口并排，下方 bot_n 个窗口并排"""
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect()
        th = area.height() // 2
        bh = area.height() - th
        for i in range(min(top_n, len(subs))):
            tw = area.width() // top_n
            subs[i].setGeometry(i * tw, 0, tw, th)
        for j in range(min(bot_n, len(subs) - top_n)):
            bw = area.width() // bot_n
            idx = top_n + j
            subs[idx].setGeometry(j * bw, th, bw, bh)

    # ── 通用网格布局 ──

    def _layout_grid(self, rows: int, cols: int) -> None:
        """rows × cols 等分网格"""
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect()
        cw, ch = area.width() // cols, area.height() // rows
        for i, sub in enumerate(subs):
            if i >= rows * cols: break
            sub.setGeometry((i % cols) * cw, (i // cols) * ch, cw, ch)

    @Slot()
    def _layout_row(self) -> None:
        """所有窗口排成一行，等宽均分"""
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect()
        cw = area.width() // len(subs)
        for i, sub in enumerate(subs):
            sub.setGeometry(i * cw, 0, cw, area.height())

    @Slot()
    def _layout_column(self) -> None:
        """所有窗口排成一列，等高均分"""
        subs = self._mdi.subWindowList()
        if not subs: return
        area = self._mdi.viewport().rect()
        ch = area.height() // len(subs)
        for i, sub in enumerate(subs):
            sub.setGeometry(0, i * ch, area.width(), ch)

    # ══════════════════════════════════════════════════════════════════════
    # 轴联动（v3.0: 多组独立绑定）
    # ══════════════════════════════════════════════════════════════════════

    def _update_axis_linking(self) -> None:
        # 全部解绑
        for wf in self._waveform_plots.values():
            wf.plot_item.setXLink(None); wf.plot_item.setYLink(None)

        # 清除反向连接（v3.5.1）
        for vb, slot in self._x_link_reverse_connections:
            try:
                vb.sigXRangeChanged.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._x_link_reverse_connections.clear()

        if not self._channel_order: return

        # X 轴：按 link_group_x 分组（v3.5.1: 双向联动）
        xg: dict[int, list[int]] = {}
        for cid in self._channel_order:
            g = self._channels[cid].link_group_x
            if g > 0 and cid in self._waveform_plots:
                xg.setdefault(g, []).append(cid)
        for g, mem in xg.items():
            if len(mem) >= 2:
                mv = self._waveform_plots[mem[0]].plot_item.getViewBox()
                for cid in mem[1:]:
                    self._waveform_plots[cid].plot_item.setXLink(mv)
                    # 反向连接：主 ViewBox 也跟随从 ViewBox 的范围变化
                    slave_vb = self._waveform_plots[cid].plot_item.getViewBox()
                    slave_vb.sigXRangeChanged.connect(mv.setXRange)
                    self._x_link_reverse_connections.append(
                        (slave_vb, mv.setXRange))

        # Y 轴：按 link_group_y 分组
        yg: dict[int, list[int]] = {}
        for cid in self._channel_order:
            g = self._channels[cid].link_group_y
            if g > 0 and cid in self._waveform_plots:
                yg.setdefault(g, []).append(cid)
        for g, mem in yg.items():
            if len(mem) >= 2:
                mv = self._waveform_plots[mem[0]].plot_item.getViewBox()
                for cid in mem[1:]:
                    self._waveform_plots[cid].plot_item.setYLink(mv)

    # ══════════════════════════════════════════════════════════════════════
    # 绘图
    # ══════════════════════════════════════════════════════════════════════

    def _update_single_plot(self, cid: int, live_update=False) -> None:
        if cid not in self._channels or cid not in self._waveform_plots:
            return
        cfg = self._channels[cid]; wf = self._waveform_plots[cid]
        pt = self.PLOT_THEME[self._current_theme]
        fg = pt["fg"]

        if not cfg.x_column:
            wf.clear_all(); return
        
        if self.current_mode == 'live':
            xd = self.live_center.get_column_data(cfg.x_column)
        else:
            xd = self.data_center.get_column_data(cfg.x_column)
            
        if len(xd) == 0:
            wf.clear_all(); return

        valid_x = xd[np.isfinite(xd)]
        if len(valid_x) > 0:
            # isfinite 已过滤 NaN/Inf，直接 .min()/.max() 比 nanmin/nanmax 更快
            global_min_x = float(valid_x.min())
            global_max_x = float(valid_x.max())
            # 移除拖动边界限制，允许自由平移
            wf.plot_item.vb.setLimits(xMin=None, xMax=None)

            if live_update:
                latest_x = float(valid_x[-1])
                current_rect = wf.plot_item.vb.viewRect()
                span = current_rect.width()
                if span <= 0:
                    span = 1000.0
                elif span > self.live_center.capacity:
                    span = float(self.live_center.capacity)
                    
                right_edge = current_rect.right()
                is_user_dragging = getattr(cfg, 'is_user_dragging', False)
                
                # --- 智能滚轮缩放判定 ---
                old_span = getattr(cfg, '_last_tick_span', span)
                cfg._last_tick_span = span
                was_tracking = getattr(cfg, '_was_tracking', True)
                
                # 如果 span 发生了明显变化（超过 1%），说明用户触发了纯粹的缩放
                is_zooming = abs(span - old_span) > (span * 0.01)
                
                if was_tracking and is_zooming:
                    # 如果刚才在自动追踪，且正在缩放时间轴，强制恢复追踪
                    # 从而拦截并修正主画板底层 WheelEvent 错误触发的拖拽暂停状态
                    is_user_dragging = False
                    cfg.is_user_dragging = False
                
                panel = self._channel_panels.get(cid)
                indicator_held = getattr(panel.indicator, '_dragging', False) if panel else False
                # v3.5.1: 联动组内其他通道的 indicator 正在拖拽时，本通道也视为被 hold，防止误恢复自动滚动
                if not indicator_held and cfg.link_group_x > 0:
                    for ocid in self._channel_order:
                        if ocid != cid:
                            ocfg = self._channels.get(ocid)
                            if ocfg and ocfg.link_group_x == cfg.link_group_x:
                                opanel = self._channel_panels.get(ocid)
                                if opanel and getattr(opanel.indicator, '_dragging', False):
                                    indicator_held = True
                                    break

                # 恢复自动滚动阈值：放宽高频下的捕捉范围（视口 10% 或至少 2000 个点，防止跑得太快抓不到）
                catch_threshold = max(span * 0.1, 2000.0)
                if is_user_dragging and not indicator_held and right_edge >= latest_x - catch_threshold:
                    is_user_dragging = False
                    cfg.is_user_dragging = False
                    
                cfg._was_tracking = not is_user_dragging
                
                # 兜底：如果用户拖拽后停留在某处，但该处的数据已经被环形缓冲区完全淘汰（落后于最老数据）
                # 为了防止显示一片空白，强制将其推到缓冲区最尾部
                if is_user_dragging and right_edge < global_min_x:
                    wf.plot_item.vb.setXRange(global_min_x, global_min_x + span, padding=0)
                    cfg.last_tracked_x = global_min_x + span
                elif not is_user_dragging:
                    wf.plot_item.vb.setXRange(latest_x - span, latest_x, padding=0)
                    cfg.last_tracked_x = latest_x
            else:
                # 第一次初始化 X 轴范围
                if not getattr(cfg, 'x_initialized', False):
                    if self.current_mode == 'live':
                        first_x = float(valid_x[0])
                        # 动态模式：初始可能只有1个点，给默认跨度避免抽搐
                        wf.plot_item.vb.setXRange(first_x, first_x + 1000, padding=0)
                    else:
                        # 静态模式：autoRange 显示全量数据，pyqtgraph 内置降采样负责渲染
                        wf.plot_item.vb.autoRange()
                    wf.plot_item.vb.disableAutoRange(axis=pg.ViewBox.XAxis)
                    cfg.x_initialized = True

        curves_full_data: list[tuple[np.ndarray, np.ndarray]] = []
        for cc in cfg.curves:
            if not cc.y_column:
                continue
            if self.current_mode == 'live':
                yd = self.live_center.get_column_data(cc.y_column)
            else:
                yd = self.data_center.get_column_data(cc.y_column)
            # 收集 (xd, yd_full)，避免后面游标循环再次从 buffer 拉取
            curves_full_data.append((xd, yd))

            if self.current_mode == 'live':
                # 动态模式：数据无限增长，按视口切片 + 峰值降采样
                v_rect = wf.plot_item.vb.viewRect()
                vw = v_rect.width()
                left_b = v_rect.left() - vw * 0.5
                right_b = v_rect.right() + vw * 0.5

                start_idx = np.searchsorted(xd, left_b)
                end_idx = np.searchsorted(xd, right_b, side='right')

                if start_idx < end_idx:
                    sliced_x = xd[start_idx:end_idx]
                    sliced_y = yd[start_idx:end_idx]

                    # --- 极限 NumPy 峰值降采样优化 ---
                    n_points = len(sliced_x)
                    if n_points > 10000:
                        target_pts = 4000
                        chunk_size = n_points // target_pts
                        trunc_len = chunk_size * target_pts

                        sx_2d = sliced_x[:trunc_len].reshape(target_pts, chunk_size)
                        sy_2d = sliced_y[:trunc_len].reshape(target_pts, chunk_size)

                        y_max = sy_2d.max(axis=1)
                        y_min = sy_2d.min(axis=1)
                        x_mid = sx_2d[:, 0]

                        final_x = np.empty(target_pts * 2, dtype=np.float32)
                        final_x[0::2] = x_mid
                        final_x[1::2] = x_mid + 1e-6

                        final_y = np.empty(target_pts * 2, dtype=np.float32)
                        final_y[0::2] = y_min
                        final_y[1::2] = y_max

                        if trunc_len < n_points:
                            rem_x = sliced_x[trunc_len:]
                            rem_y = sliced_y[trunc_len:]
                            sliced_x = np.concatenate((final_x, rem_x))
                            sliced_y = np.concatenate((final_y, rem_y))
                        else:
                            sliced_x = final_x
                            sliced_y = final_y
                else:
                    sliced_x = xd
                    sliced_y = yd

                wf.set_curve_data(cc, sliced_x, sliced_y)
            else:
                # 静态模式：设全量数据，pyqtgraph 内置 auto-downsample (peak)
                # 负责按像素宽度降采样，拖拽/缩放时 GPU 级实时更新
                wf.set_curve_data(cc, xd, yd)

        wf.sync_curves(cfg.curves, fg)
        
        if not getattr(cfg, 'y_initialized_after_clear', True):
            if cfg.auto_scale_y:
                panel = self._channel_panels.get(cid)
                if panel:
                    panel._on_auto_y_toggled(True)
            cfg.y_initialized_after_clear = True

        # 纯色底轴（仅主题/窗口名变更时更新，避免每帧重复设置）
        axis_style_key = (self._current_theme, cfg.window_name)
        if axis_style_key != getattr(cfg, '_last_axis_style_key', None):
            wf.plot_item.getAxis('bottom').setPen(fg)
            wf.plot_item.getAxis('bottom').setTextPen(fg)
            # 底轴不要 label（会挤高），用顶部标题居中显示窗口名
            wf.plot_item.getAxis('bottom').setLabel('')
            wf.plot_item.setTitle(cfg.window_name, size='8pt', color=fg)
            cfg._last_axis_style_key = axis_style_key

        # 游标数据（直接复用渲染循环中收集的完整 Y 数据，避免重复 fetch）
        if curves_full_data:
            self._cursor_mgr.set_plot_data(cid, curves_full_data)

        # 更新指示器（复用已计算的 valid_x min/max，避免重复 nanmin/nanmax）
        panel = self._channel_panels.get(cid)
        if panel and hasattr(panel, 'indicator'):
            if len(valid_x) > 0:
                ratio = 1.0
                if self.current_mode == 'live' and self.live_center.capacity > 0:
                    ratio = min(1.0, len(valid_x) / self.live_center.capacity)
                panel.indicator.set_global_bounds(float(valid_x.min()), float(valid_x.max()), ratio)
            panel.indicator.update()

        # 强制所有 ViewBox（含辅助轴）立即刷新
        wf.plot_item.update()

    def _refresh_all_plots(self) -> None:
        for cid in self._channel_order:
            self._update_single_plot(cid)


# ==============================================================================
# 主题系统 — 浅色 / 深色（均通过 QPalette 脱离系统主题色）
# ==============================================================================

def _build_dark_palette() -> QPalette:
    """VS Code 风格暗色调色板"""
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(204, 204, 204))
    p.setColor(QPalette.ColorRole.Base,            QColor(37, 37, 38))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(45, 45, 45))
    p.setColor(QPalette.ColorRole.Text,            QColor(204, 204, 204))
    p.setColor(QPalette.ColorRole.Button,          QColor(60, 60, 60))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(204, 204, 204))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(0, 122, 204))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(45, 45, 45))
    p.setColor(QPalette.ColorRole.ToolTipText,     QColor(204, 204, 204))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(128, 128, 128))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       QColor(128, 128, 128))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(128, 128, 128))
    return p


def _build_light_palette() -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window,          QColor(240, 240, 240))
    p.setColor(QPalette.ColorRole.WindowText,      QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.Base,            QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.AlternateBase,   QColor(245, 245, 245))
    p.setColor(QPalette.ColorRole.Text,            QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.Button,          QColor(235, 235, 235))
    p.setColor(QPalette.ColorRole.ButtonText,      QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.Highlight,       QColor(0, 120, 215))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase,     QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipText,     QColor(30, 30, 30))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(160, 160, 160))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       QColor(160, 160, 160))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(160, 160, 160))
    return p


_DARK_STYLESHEET = """
    QGroupBox {
        font-weight:normal; border:1px solid #3e3e3e; border-radius:6px;
        margin-top:4px; padding-top:4px; color:#ccc;
        background:#252526;
    }
    QGroupBox::title { subcontrol-origin:margin; left:10px; padding:4px 12px;
                      color:#eee; background:#383838; border-radius:4px; }
    QPushButton { border:1px solid #505050; border-radius:4px; padding:4px 12px;
                  background:#3c3c3c; color:#ccc; }
    QPushButton:hover { background:#505050; border-color:#666; }
    QPushButton:pressed { background:#2d2d2d; }
    
    QPushButton#btn_add_curve { background:#2d4232; color:#aaffaa; border-color:#3d5944; }
    QPushButton#btn_add_curve:hover { background:#3d5944; border-color:#4d6d56; }
    QPushButton#btn_add_curve:pressed { background:#213325; }

    QPushButton#btn_del_ch { background:#4a2b2b; color:#ffaaaa; border-color:#633939; }
    QPushButton#btn_del_ch:hover { background:#633939; border-color:#7d4848; }
    QPushButton#btn_del_ch:pressed { background:#362020; }
    QComboBox,QDoubleSpinBox,QSpinBox,QLineEdit {
        border:1px solid #505050; border-radius:3px; padding:2px 6px;
        background:#2d2d30; color:#ccc; }
    QComboBox::drop-down { border:none; }
    QComboBox QAbstractItemView {
        background:#2d2d30; color:#ccc; border:1px solid #3e3e3e;
        selection-background-color:#007acc; selection-color:#fff; }
    QScrollArea { background:#1e1e1e; border:none; }
    QScrollBar:vertical { background:#1e1e1e; width:10px; border-radius:5px; }
    QScrollBar::handle:vertical { background:#424242; border-radius:5px; min-height:20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
    QScrollBar:horizontal { background:#1e1e1e; height:10px; border-radius:5px; }
    QScrollBar::handle:horizontal { background:#424242; border-radius:5px; min-width:20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
    QMenuBar { background:#2d2d30; color:#ccc; border-bottom:1px solid #3e3e3e; }
    QMenuBar::item:selected { background:#3e3e3e; color:#fff; }
    QMenu { background:#2d2d30; color:#ccc; border:1px solid #3e3e3e; }
    QMenu::item { color:#ccc; padding:4px 24px; }
    QMenu::item:selected { background:#007acc; color:#fff; }
    QMenu::separator { height:1px; background:#3e3e3e; margin:4px 8px; }
    QStatusBar { background:#007acc; color:#fff; border-top:none; }
    QSplitter::handle { background:#3e3e3e; image:none; }
    QToolTip { background:#424242; color:#ccc; border:1px solid #555; }
    CurveControlWidget { background:transparent; border:none; }
    QMdiArea { background:#2a2a2a; }
    QMdiSubWindow { background: transparent; border: none; }
    QMdiSubWindow::title { height:0; padding:0; border:none; }
    QMdiSubWindow::systemMenu { width:0; height:0; }
    QMdiSubWindow::closeButton, QMdiSubWindow::minimizeButton, QMdiSubWindow::maximizeButton { width:0; height:0; padding:0; border:none; }
    QSizeGrip { width:0; height:0; image:none; }
"""

_LIGHT_STYLESHEET = """
    QGroupBox {
        font-weight:normal; border:1px solid #bbb; border-radius:6px;
        margin-top:4px; padding-top:4px; color:#222;
        background:#f8f8f8;
    }
    QGroupBox::title { subcontrol-origin:margin; left:10px; padding:4px 12px;
                      color:#333; background:#ddd; border-radius:4px; }
    QPushButton { border:1px solid #bbb; border-radius:4px; padding:4px 12px;
                  background:#e8e8e8; color:#222; }
    QPushButton:hover { background:#ddd; border-color:#999; }
    QPushButton:pressed { background:#ccc; }

    QPushButton#btn_add_curve { background:#e1f0e4; color:#1f5e2d; border-color:#b4d6bc; }
    QPushButton#btn_add_curve:hover { background:#cee6d2; border-color:#9cbdacc; }
    QPushButton#btn_add_curve:pressed { background:#b4d6bc; }

    QPushButton#btn_del_ch { background:#fce8e8; color:#a62b2b; border-color:#f0c0c0; }
    QPushButton#btn_del_ch:hover { background:#fad4d4; border-color:#e69e9e; }
    QPushButton#btn_del_ch:pressed { background:#f0c0c0; }
    QComboBox,QDoubleSpinBox,QSpinBox,QLineEdit {
        border:1px solid #bbb; border-radius:3px; padding:2px 6px;
        background:#fff; color:#222; }
    QComboBox::drop-down { border:none; }
    QComboBox QAbstractItemView {
        background:#fff; color:#222; border:1px solid #bbb;
        selection-background-color:#0078D7; selection-color:#fff; }
    QScrollArea { background:#f0f0f0; border:none; }
    QScrollBar:vertical { background:#e0e0e0; width:10px; border-radius:5px; }
    QScrollBar::handle:vertical { background:#aaa; border-radius:5px; min-height:20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
    QScrollBar:horizontal { background:#e0e0e0; height:10px; border-radius:5px; }
    QScrollBar::handle:horizontal { background:#aaa; border-radius:5px; min-width:20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; }
    QMenuBar { background:#f0f0f0; color:#222; border-bottom:1px solid #ccc; }
    QMenuBar::item:selected { background:#ddd; color:#000; }
    QMenu { background:#f5f5f5; color:#222; border:1px solid #ccc; }
    QMenu::item { color:#222; padding:4px 24px; }
    QMenu::item:selected { background:#0078D7; color:#fff; }
    QMenu::separator { height:1px; background:#ddd; margin:4px 8px; }
    QStatusBar { background:#f0f0f0; color:#444; border-top:1px solid #ccc; }
    QSplitter::handle { background:#ccc; image:none; }
    QToolTip { background:#fff; color:#222; border:1px solid #aaa; }
    CurveControlWidget { background:transparent; border:none; }
    QMdiArea { background:#e0e0e0; }
    QMdiSubWindow { background: transparent; border: none; }
    QMdiSubWindow::title { height:0; padding:0; border:none; }
    QMdiSubWindow::systemMenu { width:0; height:0; }
    QMdiSubWindow::closeButton, QMdiSubWindow::minimizeButton, QMdiSubWindow::maximizeButton { width:0; height:0; padding:0; border:none; }
    QSizeGrip { width:0; height:0; image:none; }
"""

THEMES: dict[str, tuple[QPalette, str]] = {
    "dark":  (_build_dark_palette(),  _DARK_STYLESHEET),
    "light": (_build_light_palette(), _LIGHT_STYLESHEET),
    "monokai": (_build_dark_palette(),  _DARK_STYLESHEET),
    "solar_dark": (_build_dark_palette(),  _DARK_STYLESHEET),
    "solar_light": (_build_light_palette(), _LIGHT_STYLESHEET),
}


# ==============================================================================
# 程序入口
# ==============================================================================
def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("WaveScope")
    app.setOrganizationName("SensorTools")
    app.setStyle("Fusion")

    # 默认深色主题
    palette, stylesheet = THEMES["dark"]
    app.setPalette(palette)
    app.setStyleSheet(stylesheet)
    app.setWindowIcon(QIcon())  # 移除默认 Qt 图标

    win = MainWindow(); win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
