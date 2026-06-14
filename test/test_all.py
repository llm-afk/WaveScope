#!/usr/bin/env python3
"""综合性自测脚本 — 不启动 GUI，只测数据层和核心逻辑"""
import sys, os, traceback, tempfile

# 测试计数
passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ═══════════════════════════════════════════════════════════
section("1. 导入检查")

try:
    import numpy as np; check("import numpy", True)
except Exception as e: check("import numpy", False, str(e))

try:
    import pandas as pd; check("import pandas", True)
except Exception as e: check("import pandas", False, str(e))

try:
    from PySide6.QtCore import Qt, Signal, Slot, QTimer
    check("import PySide6.QtCore", True)
except Exception as e: check("import PySide6.QtCore", False, str(e))

try:
    from PySide6.QtGui import QColor, QAction, QIcon, QPalette, QPainter
    check("import PySide6.QtGui", True)
except Exception as e: check("import PySide6.QtGui", False, str(e))

try:
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QPushButton, QFileDialog, QComboBox, QLabel, QDoubleSpinBox,
        QGroupBox, QScrollArea, QMessageBox, QColorDialog,
        QFrame, QSizePolicy, QSpinBox, QSplitter, QMdiArea, QMdiSubWindow,
        QLineEdit, QDialog, QTabWidget, QFormLayout, QCheckBox,
    )
    check("import PySide6.QtWidgets", True)
except Exception as e: check("import PySide6.QtWidgets", False, str(e))

try:
    import pyqtgraph as pg
    check("import pyqtgraph", True)
except Exception as e: check("import pyqtgraph", False, str(e))

# 创建 QApplication（测试需要）
app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

# ═══════════════════════════════════════════════════════════
section("2. 源文件语法与 AST")

src = os.path.join(os.path.dirname(__file__), 'waveform_viewer.pyw')
check("源文件存在", os.path.exists(src), src)

import ast
try:
    with open(src, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    check("AST 解析", True)
except SyntaxError as e:
    check("AST 解析", False, str(e))

# ═══════════════════════════════════════════════════════════
section("3. 模块可导入")

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("waveform_viewer", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("模块加载", True)
except Exception as e:
    check("模块加载", False, f"{type(e).__name__}: {e}")
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("4. DataCenter 数据层")

try:
    dc = mod.DataCenter()
    check("DataCenter() 创建", True)
    check("is_loaded 初始为 False", not dc.is_loaded)
    check("row_count 初始为 0", dc.row_count == 0)
    check("columns 初始为空", dc.columns == [])
    check("file_path 初始为 None", dc.file_path is None)
except Exception as e:
    check("DataCenter 基本", False, str(e))
    traceback.print_exc()

# 用临时 CSV 测试加载
try:
    csv_utf8 = "A,B,C\n1,2,3\n4,5,6\n7,8,9\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
        f.write(csv_utf8)
        tmp_csv = f.name

    dc2 = mod.DataCenter()
    df = dc2.load_csv(tmp_csv)
    check("load_csv 返回 DataFrame", isinstance(df, pd.DataFrame))
    check("is_loaded 为 True", dc2.is_loaded)
    check("row_count = 3", dc2.row_count == 3)
    check("columns 包含 Index+A,B,C", set(dc2.columns) >= {'Index', 'A', 'B', 'C'})

    data_a = dc2.get_column_data('A')
    check("get_column_data 返回 ndarray", isinstance(data_a, np.ndarray))
    check("get_column_data 长度正确", len(data_a) == 3)
    check("get_column_data 值正确", np.allclose(data_a, [1.0, 4.0, 7.0]))

    # 测试不存在列返回空数组
    empty_arr = dc2.get_column_data('NonExistent')
    check("不存在列返回空数组", len(empty_arr) == 0)

    os.unlink(tmp_csv)
except Exception as e:
    check("CSV 加载", False, str(e))
    traceback.print_exc()

# 编码测试：GBK CSV
try:
    csv_gbk = "时间,电压\n1.0,2.0\n3.0,4.0\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='gbk') as f:
        f.write(csv_gbk)
        tmp_gbk = f.name

    dc3 = mod.DataCenter()
    df3 = dc3.load_csv(tmp_gbk)
    check("GBK CSV 加载", len(df3) == 2)
    check("GBK CSV 列名正确", '时间' in dc3.columns and '电压' in dc3.columns)
    os.unlink(tmp_gbk)
except Exception as e:
    check("GBK CSV", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("5. CurveConfig 曲线配置")

try:
    cc = mod.CurveConfig(0, color_seed=3)
    check("CurveConfig.curve_index", cc.curve_index == 0)
    check("CurveConfig.stroke_width 默认 0.5", abs(cc.stroke_width - 0.5) < 0.01)
    check("CurveConfig.alpha 默认 255", cc.alpha == 255)
    check("CurveConfig.display_mode 默认 'both'", cc.display_mode == 'both')
    check("CurveConfig.axis_index 默认 0", cc.axis_index == 0)
    check("CurveConfig.color 非空", cc.color.isValid())
except Exception as e:
    check("CurveConfig", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("6. ChannelConfig 通道配置")

try:
    ch = mod.ChannelConfig(0)
    check("ChannelConfig.channel_id", ch.channel_id == 0)
    check("ChannelConfig.window_name", "窗口" in ch.window_name)
    check("ChannelConfig.x_column 默认空", ch.x_column == "")
    check("ChannelConfig.link_group_x 默认 0", ch.link_group_x == 0)
    check("ChannelConfig.curves 初始空", ch.curves == [])
    check("ChannelConfig.auto_scale_y 默认 False", not ch.auto_scale_y)
except Exception as e:
    check("ChannelConfig", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("7. CursorManager 光标管理器")

try:
    cm = mod.CursorManager()
    check("CursorManager 创建", True)
    check("is_enabled 初始 False", not cm.is_enabled)
    check("set_enabled(True) 不报错", True)  # just no crash
    cm.set_enabled(True)
    check("is_enabled 变为 True", cm.is_enabled)
    cm.set_snap_to_data(True)
    cm.set_sync_enabled(True)
    cm.set_label_mode(0)
    cm.set_label_mode(1)
    cm.set_label_mode(2)
    check("label mode 切换不报错", True)
    cm.set_enabled(False)
except Exception as e:
    check("CursorManager", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("8. WaveformPlot + PlotItem")

try:
    pw = pg.PlotWidget()
    pi = pw.getPlotItem()
    wf = mod.WaveformPlot(pi)
    check("WaveformPlot 创建", True)
    check("_vbs 初始有 aidx=0", 0 in wf._vbs)
    check("_axes 初始有 aidx=0", 0 in wf._axes)

    # 测试 ensure_axis
    cc_test = mod.CurveConfig(0, 0)
    cc_test.axis_index = 1
    wf._ensure_axis(1, cc_test)
    check("_ensure_axis(1) 后 _vbs 有 aidx=1", 1 in wf._vbs)
    check("_ensure_axis(1) 后 _axes 有 aidx=1", 1 in wf._axes)

    cc_test2 = mod.CurveConfig(1, 1)
    cc_test2.axis_index = 2
    wf._ensure_axis(2, cc_test2)
    check("_ensure_axis(2) 后 _vbs 有 aidx=2", 2 in wf._vbs)
    check("_ensure_axis(2) 后 _axes 有 aidx=2", 2 in wf._axes)

    cc_test3 = mod.CurveConfig(2, 2)
    cc_test3.axis_index = 3
    wf._ensure_axis(3, cc_test3)
    check("_ensure_axis(3) 后 _vbs 有 aidx=3", 3 in wf._vbs)

    cc_test4 = mod.CurveConfig(3, 3)
    cc_test4.axis_index = 4
    wf._ensure_axis(4, cc_test4)
    check("_ensure_axis(4) 后 _vbs 有 aidx=4", 4 in wf._vbs)

    cc_test5 = mod.CurveConfig(4, 4)
    cc_test5.axis_index = 5
    wf._ensure_axis(5, cc_test5)
    check("_ensure_axis(5) 后 _vbs 有 aidx=5", 5 in wf._vbs)

    check("所有 6 个轴创建后 _vbs 数量=6", len(wf._vbs) == 6)
    check("所有 6 个轴创建后 _axes 数量=6", len(wf._axes) == 6)

    # 测试 set_curve_data
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    y = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
    wf.set_curve_data(cc_test, x, y)
    check("set_curve_data 不报错", True)

    # 测试 sync_curves
    curves = [cc_test]
    wf.sync_curves(curves, 'w')
    check("sync_curves 不报错", True)

    # 测试 clear_all
    wf.clear_all()
    check("clear_all 后 _items 为空", len(wf._items) == 0)

    pw.deleteLater()
except Exception as e:
    check("WaveformPlot", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("9. CursorManager register/unregister")

try:
    cm2 = mod.CursorManager()
    pw2 = pg.PlotWidget()
    pi2 = pw2.getPlotItem()
    wf2 = mod.WaveformPlot(pi2)

    cm2.register_plot(100, wf2)
    check("register_plot 后 _entries 长度=1", len(cm2._entries) == 1)
    check("entry 有 x_data=None", cm2._entries[100].get('x_data') is None)

    cm2.unregister_plot(100)
    check("unregister_plot 后 _entries 长度=0", len(cm2._entries) == 0)
    check("unregister 不存在 id 不报错", True)
    cm2.unregister_plot(999)  # should not crash

    # test set_plot_data
    cm2.register_plot(200, wf2)
    xd = np.array([1.0, 2.0, 3.0])
    yd = np.array([10.0, 20.0, 30.0])
    cm2.set_plot_data(200, [(xd, yd)])
    check("set_plot_data 后 x_data 非空", cm2._entries[200].get('x_data') is not None)

    # test fix_cursor_at_current
    cm2.fix_cursor_at_current(200)
    check("fix_cursor 不报错", True)

    cm2.clear_all()
    check("clear_all 后 _entries 为空", len(cm2._entries) == 0)

    pw2.deleteLater()
except Exception as e:
    check("CursorManager 注册", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("10. 主题系统")

try:
    check("THEMES 有 dark", "dark" in mod.THEMES)
    check("THEMES 有 light", "light" in mod.THEMES)
    check("dark theme 有 palette", isinstance(mod.THEMES["dark"][0], QPalette))
    check("dark theme 有 stylesheet", isinstance(mod.THEMES["dark"][1], str))
    check("light theme 有 palette", isinstance(mod.THEMES["light"][0], QPalette))
    check("light theme 有 stylesheet", isinstance(mod.THEMES["light"][1], str))
except Exception as e:
    check("主题系统", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("11. PRESET_COLORS / DISPLAY_MODES / LINK_GROUP_NAMES")

try:
    check("PRESET_COLORS 有 12 种颜色", len(mod.PRESET_COLORS) == 12)
    check("DISPLAY_MODES 有 3 种", len(mod.DISPLAY_MODES) == 3)
    check("LINK_GROUP_NAMES 有 5 组", len(mod.LINK_GROUP_NAMES) == 5)
except Exception as e:
    check("常量", False, str(e))

# ═══════════════════════════════════════════════════════════
section("12. 边界情况")

try:
    # NaN 数据处理
    dc4 = mod.DataCenter()
    csv_nan = "X,Y\n1,2\n3,abc\n5,6\n"
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
        f.write(csv_nan)
        tmp_nan = f.name
    df4 = dc4.load_csv(tmp_nan)
    data_y = dc4.get_column_data('Y')
    check("NaN 列处理", np.isnan(data_y[1]))
    os.unlink(tmp_nan)

    # 空数据 set_curve_data — 使用正确生命周期的 PlotWidget
    pw3 = pg.PlotWidget()
    wf3 = mod.WaveformPlot(pw3.getPlotItem())
    empty = np.array([], dtype=np.float32)
    cc_e = mod.CurveConfig(0, 0)
    cc_e.y_column = "test"
    wf3.set_curve_data(cc_e, empty, empty)  # 不应崩溃
    check("空数据 set_curve_data 不崩溃", True)

    # 全 NaN 数据
    all_nan = np.array([np.nan, np.nan, np.nan])
    wf3.set_curve_data(cc_e, all_nan, all_nan)
    check("全NaN set_curve_data 不崩溃", True)
    pw3.deleteLater()
except Exception as e:
    check("边界情况", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
section("13. ChannelControlPanel 信号")

try:
    # 需要一个真实的 CSV 才能创建 panel（需要 columns）
    # 使用临时数据
    dc5 = mod.DataCenter()
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
        f.write("X,Y1,Y2\n1,10,100\n2,20,200\n3,30,300\n")
        tmp5 = f.name
    dc5.load_csv(tmp5)

    cfg = mod.ChannelConfig(42)
    cfg.x_column = 'X'
    cc1 = mod.CurveConfig(0, 0)
    cc1.y_column = 'Y1'
    cfg.curves.append(cc1)

    panel = mod.ChannelControlPanel(42, cfg, dc5.columns)
    check("ChannelControlPanel 创建", True)

    # 测试 update_columns
    panel.update_columns(['A', 'B', 'C'])
    check("update_columns 不报错", True)

    # 测试 cleanup
    panel.cleanup()
    check("cleanup 不报错", True)

    panel.deleteLater()
    os.unlink(tmp5)
except Exception as e:
    check("ChannelControlPanel", False, str(e))
    traceback.print_exc()

# ═══════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"  测试结果: {passed} 通过 / {failed} 失败  (共 {passed+failed})")
print(f"{'='*60}")

if failed > 0:
    sys.exit(1)
else:
    print("\n*** 所有测试通过 ***")
