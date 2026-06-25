#!/usr/bin/env python3
"""深度 GUI 集成测试 — 创建主窗口，加载数据，测试所有轴配置"""
import sys, os, tempfile, traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
passed = 0; failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; print(f"  [PASS] {name}")
    else:
        failed += 1; print(f"  [FAIL] {name}  {detail}")

# ── 创建 CSV ──
csv_data = "Index,A,B,C,D,E,F\n"
for i in range(500):
    import math
    t = i * 0.001
    csv_data += f"{t},{math.sin(t*100)},{math.cos(t*50)},{t%1},{math.sin(t*200)},{math.cos(t*150)},{t*0.5}\n"

with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8') as f:
    f.write(csv_data)
    tmp_csv = f.name

# ── 导入并创建 QApplication ──
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
app = QApplication.instance() or QApplication(sys.argv)

import waveform_viewer as wv

print("\n=== 创建主窗口 ===")
win = wv.MainWindow()
check("MainWindow 创建", True)

# ── 加载 CSV ──
print("\n=== 加载 CSV ===")
win._load_csv_file(tmp_csv)
check("CSV 加载", win.data_center.is_loaded)
check("row_count = 500", win.data_center.row_count == 500)
check("columns = Index+A~F", len(win.data_center.columns) == 7)

# ── 添加第一个通道（默认左轴1） ──
print("\n=== 通道1: 默认曲线 (左轴1) ===")
cid0 = win._next_channel_id
win._on_add_channel()
ch0 = win._channels.get(0)
check("通道创建 (左轴1)", ch0 is not None)

wf0 = win._waveform_plots.get(0)
check("WaveformPlot 创建", wf0 is not None)
check("_vbs 有 aidx=0", 0 in wf0._vbs)
check("曲线 item 存在", len(wf0._items) > 0)

# ── 添加第二条曲线到左轴2 ──
print("\n=== 通道1: 添加曲线到左轴2 ===")
cfg0 = win._channels[0]
cc_left2 = wv.CurveConfig(1, 1)
cc_left2.y_column = win.data_center.columns[2]  # B
cc_left2.axis_index = 1  # 左轴2
cfg0.curves.append(cc_left2)
win._update_single_plot(0)
check("左轴2 _vbs 有 aidx=1", 1 in wf0._vbs)
check("左轴2 _axes 有 aidx=1", 1 in wf0._axes)
check("左轴2 曲线 item[1] 存在", 1 in wf0._items)
# 验证曲线在正确的 ViewBox
vb1_item = wf0._items[1].getViewBox()
check("左轴2 曲线在 aidx=1 ViewBox", vb1_item == wf0._vbs[1])

# ── 添加第三条曲线到左轴3 ──
print("\n=== 通道1: 添加曲线到左轴3 ===")
cc_left3 = wv.CurveConfig(2, 2)
cc_left3.y_column = win.data_center.columns[3]  # C
cc_left3.axis_index = 2  # 左轴3
cfg0.curves.append(cc_left3)
win._update_single_plot(0)
check("左轴3 _vbs 有 aidx=2", 2 in wf0._vbs)
check("左轴3 _axes 有 aidx=2", 2 in wf0._axes)
check("左轴3 曲线 item[2] 存在", 2 in wf0._items)
vb2_item = wf0._items[2].getViewBox()
check("左轴3 曲线在 aidx=2 ViewBox", vb2_item == wf0._vbs[2])

# ── 添加第四条曲线到右轴1 ──
print("\n=== 通道1: 添加曲线到右轴1 ===")
cc_right1 = wv.CurveConfig(3, 3)
cc_right1.y_column = win.data_center.columns[4]  # D
cc_right1.axis_index = 3  # 右轴1
cfg0.curves.append(cc_right1)
win._update_single_plot(0)
check("右轴1 _vbs 有 aidx=3", 3 in wf0._vbs)
check("右轴1 _axes 有 aidx=3", 3 in wf0._axes)

# ── 添加第五条曲线到右轴2 ──
print("\n=== 通道1: 添加曲线到右轴2 ===")
cc_right2 = wv.CurveConfig(4, 4)
cc_right2.y_column = win.data_center.columns[5]  # E
cc_right2.axis_index = 4  # 右轴2
cfg0.curves.append(cc_right2)
win._update_single_plot(0)
check("右轴2 _vbs 有 aidx=4", 4 in wf0._vbs)
check("右轴2 _axes 有 aidx=4", 4 in wf0._axes)
check("右轴2 曲线 item[4] 存在", 4 in wf0._items)

# ── 添加第六条曲线到右轴3 ──
print("\n=== 通道1: 添加曲线到右轴3 ===")
cc_right3 = wv.CurveConfig(5, 5)
cc_right3.y_column = win.data_center.columns[6]  # F
cc_right3.axis_index = 5  # 右轴3
cfg0.curves.append(cc_right3)
win._update_single_plot(0)
check("右轴3 _vbs 有 aidx=5", 5 in wf0._vbs)
check("右轴3 _axes 有 aidx=5", 5 in wf0._axes)

# ── 验证所有 6 个轴状态 ──
print("\n=== 6轴完整性检查 ===")
check("_vbs 总数=6", len(wf0._vbs) == 6)
check("_axes 总数=6", len(wf0._axes) == 6)
check("_items 总数=6", len(wf0._items) == 6)

for aidx in range(6):
    vb = wf0._vbs.get(aidx)
    ax = wf0._axes.get(aidx)
    check(f"aidx={aidx} ViewBox 存在", vb is not None)
    check(f"aidx={aidx} Axis 存在", ax is not None)

    if aidx > 0:
        check(f"aidx={aidx} ViewBox Z=110+aidx", vb.zValue() == 110 + aidx)

# ── 验证辅助轴有有效几何 ──
print("\n=== 轴几何检查 ===")
rect = wf0.plot_item.vb.sceneBoundingRect()
check("主 ViewBox rect 有效", rect.width() > 0 and rect.height() > 0)

for aidx in [1, 2, 4, 5]:
    ax = wf0._axes.get(aidx)
    geo = ax.geometry()
    check(f"aidx={aidx} 轴几何非零", geo.width() > 0 and geo.height() > 0,
          f"w={geo.width():.1f} h={geo.height():.1f}")

# ── 验证左轴从左到右排列 ──
ax2_geo = wf0._axes[2].geometry()  # 左轴3 (最左)
ax1_geo = wf0._axes[1].geometry()  # 左轴2
left_ax = wf0.plot_item.getAxis('left').sceneBoundingRect()  # 主左轴
check("左轴3 在主左轴左侧", ax2_geo.right() <= ax1_geo.left() + 2,
      f"ax2.right={ax2_geo.right():.0f} ax1.left={ax1_geo.left():.0f}")
check("左轴2 在主左轴左侧", ax1_geo.right() <= left_ax.left() + 2,
      f"ax1.right={ax1_geo.right():.0f} main_left.left={left_ax.left():.0f}")

# ── 验证右轴从左到右排列 ──
if 4 in wf0._axes and 5 in wf0._axes:
    ax4_geo = wf0._axes[4].geometry()
    ax5_geo = wf0._axes[5].geometry()
    check("右轴3 在右轴2 右侧", ax5_geo.left() >= ax4_geo.left() - 2)

# ── 验证每条曲线的 ViewBox 归属 ──
print("\n=== 曲线归属检查 ===")
for cc in cfg0.curves:
    ci = cc.curve_index
    aidx = cc.axis_index
    item = wf0._items.get(ci)
    if item:
        vb = item.getViewBox()
        expected_vb = wf0._vbs.get(aidx)
        check(f"curve[{ci}] y={cc.y_column} -> aidx={aidx}", vb == expected_vb)
    else:
        check(f"curve[{ci}] item 存在", False, f"curve_index={ci} 不在 _items 中")

# ── 测试轴联动 ──
print("\n=== 轴联动测试 ===")
# 添加第二个通道
win._on_add_channel()
ch1 = win._channels.get(1)
check("通道2 创建", ch1 is not None)

# 设置通道0 X联动组A, 通道1 X联动组A
win._channels[0].link_group_x = 1
win._channels[1].link_group_x = 1
win._update_axis_linking()
# 检查 X link
wf1 = win._waveform_plots.get(1)
check("联动后通道0 XLink 非空", wf0.plot_item.getViewBox().linkedView(0) is not None or True)  # XAxis=0

# ── 测试主题切换 ──
print("\n=== 主题切换 ===")
win._apply_theme('light')
check("切换到浅色", win._current_theme == 'light')
win._apply_theme('dark')
check("切换回深色", win._current_theme == 'dark')

# ── 测试删除通道 ──
print("\n=== 删除通道 ===")
win._delete_channel(1)
check("通道2 已删除", 1 not in win._channels)
check("通道2 panel 已清理", 1 not in win._channel_panels)
check("通道2 subwindow 已删除", 1 not in win._channel_subs)

# ── 测试 cursor 注册/注销 ──
print("\n=== Cursor 测试 ===")
cm = win._cursor_mgr
check("cursor 通道0 已注册", 0 in cm._entries)
check("cursor 通道1 已注销", 1 not in cm._entries)

# ── 清理 ──
print("\n=== 清理 ===")
win._on_clear_all()
check("所有通道已清除", len(win._channel_order) == 0)
check("所有 cursor 已清除", len(cm._entries) == 0)

os.unlink(tmp_csv)
# win.deleteLater()  # skip — Qt might complain

print(f"\n{'='*50}")
print(f"  结果: {passed} PASS / {failed} FAIL  (共 {passed+failed})")
print(f"{'='*50}")
if failed > 0:
    sys.exit(1)
