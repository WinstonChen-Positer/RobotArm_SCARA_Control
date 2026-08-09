"""
SCARA 机械臂控制界面

对齐机械臂(robot_arm)页设计：卡片内联表头(▍标题, 不用 QGroupBox 边框标题)、
关节角度=标签+渐变条+数值、cssClass 分级按钮、等宽玻璃输入、紧凑工业控制台密度。
三栏：左=控制，中=相机实时画面，右=状态/数据反馈；对接 ScaraController，支持镜像。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QLabel, QPushButton,
    QLineEdit, QDoubleSpinBox, QSpinBox, QFrame, QSizePolicy, QPlainTextEdit,
    QProgressBar, QButtonGroup, QCheckBox, QComboBox, QScrollArea,
)

from utils import get_logger
from scara.config.scara_config import ScaraConfig, load_scara_config
from scara.controller.scara_controller import ScaraController
from scara.ui.camera_view import ScaraCameraThread
from robot_arm.ui import theme as T

logger = get_logger("scara.ui")

JOINT_NAME = ["J1", "J2", "J3", "J4"]
JOINT_UNIT = ["°", "°", "mm", "°"]
JOINT_RANGE = [(-132, 132), (-141, 141), (-150, 10), (-360, 360)]
POSE_LABELS = ["X", "Y", "Z", "Rx", "Ry", "Rz"]
POSE_UNIT = ["mm", "mm", "mm", "°", "°", "°"]

_CHK = Path(__file__).parent.joinpath("check.svg").as_posix()

SUPPLEMENT = f"""
QWidget#scaraRoot {{ background-color:{T.BG_DARK}; }}
QWidget {{ font-size:12px; }}
QCheckBox::indicator {{ width:15px; height:15px; border-radius:4px;
    border:1px solid {T.BORDER_COLOR}; background:{T.BG_GLASS}; }}
QCheckBox::indicator:checked {{ background:{T.ACCENT_COLOR}; border-color:{T.ACCENT_COLOR};
    image:url({_CHK}); }}
#card {{ background:{T.BG_CARD}; border:1px solid {T.BORDER_COLOR}; border-radius:14px; }}
#cardBar {{ background:{T.ACCENT_COLOR}; border-radius:2px; }}
/* 栏目标题：与基准页 orchestrator/ui/control_widget.APP_QSS 逐值一致（2026-07-28 统一）。
   卡头只放标题，原 #cardHint 说明行已全仓移除。 */
#cardTitle {{ color:{T.ACCENT_COLOR}; font-weight:900; font-size:12px; letter-spacing:0.7px; }}
#colScroll, #colScroll > QWidget > QWidget {{ background:transparent; border:none; }}
#kv {{ color:{T.TEXT_SECONDARY}; font-size:12px; }}
#val {{ font-family:{T.MONO}; font-weight:700; font-size:13px; color:{T.TEXT_PRIMARY}; }}
#jname {{ font-family:{T.MONO}; font-weight:700; color:{T.PRIMARY_COLOR}; font-size:12px; }}
#muted {{ color:{T.TEXT_MUTED}; font-size:11px; }}
#pill {{ border-radius:9px; padding:1px 9px; font-size:11px; font-weight:700; }}
#light {{ background:{T.BG_GLASS}; border:1px solid {T.BORDER_SOFT}; border-radius:7px; }}
#lightK {{ color:{T.TEXT_SECONDARY}; font-size:11px; }}
#io {{ background:#E7ECF3; border:1px solid {T.BORDER_SOFT}; border-radius:2px; min-width:11px; min-height:11px; }}
#ioOn {{ background:{T.SUCCESS_COLOR}; border:1px solid {T.SUCCESS_COLOR}; border-radius:2px; min-width:11px; min-height:11px; }}
#cam {{ background:#0b1220; border-radius:8px; color:{T.TEXT_MUTED}; font-size:13px; }}
QPushButton {{ padding:4px 9px; min-height:22px; outline:none; }}
QPushButton:focus {{ outline:none; }}
QPushButton:pressed {{ background:{T.PRIMARY_SOFT}; }}
QPushButton[cssClass="primary"]:pressed {{ background:#1D54CC; }}
QPushButton[cssClass="success"]:pressed {{ background:#1A8A5E; }}
QPushButton[cssClass="jog"]:pressed {{ background:{T.ACCENT_SOFT}; border-color:{T.ACCENT_COLOR}; }}
QPushButton#estop:pressed {{ background:#B23244; }}
QPushButton#estop {{ background:{T.ERROR_COLOR}; color:#fff; border:none; border-radius:7px;
    font-size:13px; font-weight:800; letter-spacing:2px; min-height:0; padding:2px 16px; }}
QPushButton#estop:hover {{ background:#C53B4C; }}
QPushButton#estop:disabled {{ background:#E8A6AE; color:#FBEAEC; }}
QPushButton#cambtn {{ background:#1e293b; color:#e2e8f0; border:1px solid #334155; }}
QPushButton#cambtn:hover {{ background:#273449; }}
/* JOG 按键：清晰大字号 + 足够高度（原 min-height:0 会把 −/+ 压没）*/
QPushButton[cssClass="jog"] {{ min-height:34px; padding:4px 8px; border-radius:9px;
    font-size:15px; font-weight:700; letter-spacing:1px; }}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{ min-height:20px; padding:2px 6px; }}
QPushButton#seg {{ border-radius:0; background:{T.BG_CARD}; color:{T.TEXT_SECONDARY};
    padding:5px 3px; border:1px solid {T.BORDER_COLOR}; font-weight:600; }}
QPushButton#seg:checked {{ background:{T.PRIMARY_COLOR}; color:#fff; border-color:{T.PRIMARY_COLOR}; }}
QProgressBar#posbar {{ border:none; background:#E8EEF6; border-radius:4px; max-height:8px; min-height:8px; }}
QProgressBar#posbar::chunk {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
    stop:0 #7CC1EE, stop:1 {T.PRIMARY_COLOR}); border-radius:4px; }}
QPlainTextEdit#logbox {{ background:#F8FAFD; border:1px solid {T.BORDER_SOFT}; border-radius:8px;
    font-family:{T.MONO}; font-size:11px; color:{T.TEXT_SECONDARY}; }}
QLabel#chip {{ border-radius:11px; padding:2px 9px; font-size:11px; font-weight:700; }}
"""


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """一个栏目 = 一张卡。卡头**只放标题**，不带说明行（2026-07-28 全仓统一）。"""
    f = QFrame(); f.setObjectName("card")
    v = QVBoxLayout(f); v.setContentsMargins(11, 9, 11, 10); v.setSpacing(7)
    head = QHBoxLayout(); head.setSpacing(7)
    bar = QFrame(); bar.setObjectName("cardBar"); bar.setFixedSize(3, 13)
    t = QLabel(title); t.setObjectName("cardTitle")
    head.addWidget(bar); head.addWidget(t); head.addStretch(1)
    v.addLayout(head)
    return f, v


def _btn(text: str, css: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)   # 点击后不留焦点虚线框
    if css:
        b.setProperty("cssClass", css)
    return b


def _pill(text: str, color: str, bg: str) -> QLabel:
    p = QLabel(text); p.setObjectName("pill")
    p.setStyleSheet(f"color:{color}; background:{bg};")
    p.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return p


def _light(key: str) -> tuple[QFrame, QLabel]:
    f = QFrame(); f.setObjectName("light")
    g = QVBoxLayout(f); g.setContentsMargins(4, 4, 4, 4); g.setSpacing(1)
    k = QLabel(key); k.setObjectName("lightK"); k.setAlignment(Qt.AlignmentFlag.AlignCenter)
    v = QLabel("—"); v.setAlignment(Qt.AlignmentFlag.AlignCenter); v.setStyleSheet("font-weight:700; font-size:12px;")
    g.addWidget(k); g.addWidget(v)
    return f, v


def _scroll(inner: QWidget, width: int) -> QScrollArea:
    """把一列内容包进竖向滚动区，避免加了命令头后底部卡片在小窗口被裁切。"""
    sa = QScrollArea(); sa.setObjectName("colScroll")
    sa.setWidget(inner); sa.setWidgetResizable(True); sa.setFixedWidth(width)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    sa.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return sa


class ScaraControlWidget(QWidget):
    """SCARA 主控制界面。"""

    def __init__(self, parent: Optional[QWidget] = None,
                 config: Optional[ScaraConfig] = None,
                 controller: Optional[ScaraController] = None,
                 owns_controller: bool = True):
        super().__init__(parent)
        self.setObjectName("scaraRoot")
        self.setStyleSheet(T.APP_STYLESHEET + SUPPLEMENT)
        self._cfg = config or load_scara_config()
        self._owns = owns_controller
        self._ctrl = controller or ScaraController(self._cfg)
        self._cam: Optional[ScaraCameraThread] = None

        self._joint_v: list[QLabel] = []
        self._joint_bar: list[QProgressBar] = []
        self._pose_v: list[QLabel] = []
        self._light: dict[str, QLabel] = {}
        self._io_cells: list[QFrame] = []

        self._build()
        self._bind()
        for b in self.findChildren(QPushButton):   # 所有按钮去掉点击后的焦点虚线框
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._on_conn(False)                        # 初始未连接：按钮按实际状态置灰

    @property
    def controller(self) -> ScaraController:
        return self._ctrl

    def is_device_connected(self) -> bool:
        return self._ctrl.is_connected()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        outer = QVBoxLayout(self); outer.setContentsMargins(12, 12, 12, 12); outer.setSpacing(10)
        row = QHBoxLayout(); row.setSpacing(10)
        row.addWidget(self._left(), 0)
        row.addWidget(self._center(), 1)
        row.addWidget(self._right(), 0)
        outer.addLayout(row, 1)
        # 急停：页面底部右下角（顶部不好够到，用户要求移到下面 + 缩小），永远醒目红。
        estop_row = QHBoxLayout(); estop_row.setContentsMargins(0, 0, 0, 0); estop_row.addStretch(1)
        self._btn_estop = QPushButton("■ 急 停"); self._btn_estop.setObjectName("estop")
        self._btn_estop.setFixedHeight(30); self._btn_estop.setMinimumWidth(120)
        estop_row.addWidget(self._btn_estop)
        outer.addLayout(estop_row)

    def _jog_grid(self, g: QVBoxLayout, axes) -> None:
        """每轴一行两颗按钮：「name −」「name +」，大字号清晰可辨。"""
        grid = QGridLayout(); grid.setSpacing(6); grid.setContentsMargins(0, 2, 0, 0)
        grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
        for i, (name, axis) in enumerate(axes):
            bm = _btn(f"{name}  −", "jog"); bp = _btn(f"{name}  +", "jog")
            bm.setMinimumWidth(92); bp.setMinimumWidth(92)
            bp.pressed.connect(lambda a=axis: self._jog_press(a, "+"))
            bp.released.connect(lambda a=axis: self._jog_release(a))
            bp.clicked.connect(lambda _, a=axis: self._step(a, +1))
            bm.pressed.connect(lambda a=axis: self._jog_press(a, "-"))
            bm.released.connect(lambda a=axis: self._jog_release(a))
            bm.clicked.connect(lambda _, a=axis: self._step(a, -1))
            grid.addWidget(bm, i, 0); grid.addWidget(bp, i, 1)
        g.addLayout(grid)

    def _left(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 4, 0); v.setSpacing(9)

        f, g = _card("连接")
        row = QHBoxLayout(); row.addWidget(QLabel("控制器"))
        self._ip = QLineEdit(f"{self._cfg.controller_ip}:{self._cfg.controller_port}"); self._ip.setReadOnly(True)
        row.addWidget(self._ip, 1); g.addLayout(row)
        row = QHBoxLayout()
        self._btn_conn = _btn("连接", "primary"); self._btn_disc = _btn("断开")
        self._conn_chip = QLabel("● 未连接"); self._conn_chip.setObjectName("chip")
        self._conn_chip.setStyleSheet(f"color:{T.TEXT_MUTED}; background:#EAF0F7;")
        row.addWidget(self._btn_conn); row.addWidget(self._btn_disc); row.addStretch(1); row.addWidget(self._conn_chip)
        g.addLayout(row); v.addWidget(f)

        f, g = _card("伺服 / 安全")
        row = QHBoxLayout()
        self._btn_en = _btn("使能", "success"); self._btn_dis = _btn("去使能")
        self._btn_clr = _btn("清报警"); self._btn_home = _btn("回零")
        for b in (self._btn_en, self._btn_dis, self._btn_clr, self._btn_home):
            row.addWidget(b)
        g.addLayout(row)
        v.addWidget(f)

        f, g = _card("模式 / 速度")
        seg = QHBoxLayout(); seg.setSpacing(0); self._mode_grp = QButtonGroup(self)
        for i, name in enumerate(["示教 T1", "示教 T2", "执行"]):
            b = QPushButton(name); b.setObjectName("seg"); b.setCheckable(True)
            if i == 0: b.setChecked(True)
            if i == 1 and not self._cfg.allow_t2_mode: b.setEnabled(False)
            self._mode_grp.addButton(b, i); seg.addWidget(b)
        g.addLayout(seg)
        row = QHBoxLayout(); row.addWidget(QLabel("速度"))
        self._speed = QSpinBox(); self._speed.setRange(1, 100); self._speed.setSuffix(" %")
        self._speed.setValue(self._cfg.default_speed_percent)
        self._speed_bar = QProgressBar(); self._speed_bar.setObjectName("posbar")
        self._speed_bar.setRange(0, 100); self._speed_bar.setValue(self._cfg.default_speed_percent); self._speed_bar.setTextVisible(False)
        row.addWidget(self._speed_bar, 1); row.addWidget(self._speed)
        g.addLayout(row); v.addWidget(f)

        f, g = _card("关节点动")
        self._jog_grid(g, list(zip(["J1", "J2", "J3/Z", "J4"], [1, 2, 3, 4])))
        row = QHBoxLayout(); row.addWidget(QLabel("步长"))
        self._jstep = QDoubleSpinBox(); self._jstep.setRange(0.1, 90.0)
        self._jstep.setValue(self._cfg.default_joint_step_deg); self._jstep.setSingleStep(0.5)
        self._cont = QCheckBox("连续(按住)")
        u = QLabel("度/mm"); u.setObjectName("muted")
        row.addWidget(self._jstep); row.addWidget(u); row.addStretch(1); row.addWidget(self._cont)
        g.addLayout(row); v.addWidget(f)

        f, g = _card("笛卡尔点动 (World)")
        self._jog_grid(g, list(zip(["X", "Y", "Z"], ["X", "Y", "Z"])))
        row = QHBoxLayout(); row.addWidget(QLabel("步长"))
        self._cstep = QDoubleSpinBox(); self._cstep.setRange(0.1, 100.0)
        self._cstep.setValue(self._cfg.default_cart_step_mm); self._cstep.setSingleStep(1.0)
        u = QLabel("mm"); u.setObjectName("muted")
        row.addWidget(self._cstep); row.addWidget(u); row.addStretch(1)
        g.addLayout(row); v.addWidget(f)

        f, g = _card("预设点")
        row = QHBoxLayout()
        self._preset_name = QLineEdit(); self._preset_name.setPlaceholderText("名称")
        self._btn_save_preset = _btn("保存当前")
        row.addWidget(self._preset_name, 1); row.addWidget(self._btn_save_preset)
        g.addLayout(row)
        row = QHBoxLayout()
        self._preset_combo = QComboBox()
        self._btn_goto = _btn("前往", "primary"); self._btn_del_preset = _btn("删除")
        row.addWidget(self._preset_combo, 1); row.addWidget(self._btn_goto); row.addWidget(self._btn_del_preset)
        g.addLayout(row); v.addWidget(f)

        f, g = _card("泵 / DO（scara_do）")
        row = QHBoxLayout(); row.addWidget(QLabel("UO 通道"))
        self._do_idx = QSpinBox(); self._do_idx.setRange(1, 4096); self._do_idx.setValue(1)
        b1 = _btn("开泵", "success"); b0 = _btn("停泵")
        b1.clicked.connect(lambda: self._ctrl.cmd_set_do(self._do_idx.value(), True))
        b0.clicked.connect(lambda: self._ctrl.cmd_set_do(self._do_idx.value(), False))
        row.addWidget(self._do_idx); row.addWidget(b1); row.addWidget(b0); row.addStretch(1)
        g.addLayout(row)
        tip = QLabel("写 DO 会另连控制器；机械臂已连接时可能失败，测泵请先断开机械臂。")
        tip.setObjectName("muted"); tip.setWordWrap(True)
        g.addWidget(tip)
        v.addWidget(f)

        v.addStretch(1)
        return _scroll(w, 318)

    def _center(self) -> QWidget:
        f, g = _card("相机画面（SCARA 工位）")
        self._cam_lbl = QLabel("相机未连接 — 点击「连接相机」打开 SCARA 工位相机")
        self._cam_lbl.setObjectName("cam"); self._cam_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._cam_lbl.setMinimumHeight(300)
        g.addWidget(self._cam_lbl, 1)
        bar = QHBoxLayout()
        self._btn_cam = QPushButton("连接相机"); self._btn_cam.setObjectName("cambtn")
        self._btn_snap = QPushButton("快照"); self._btn_snap.setObjectName("cambtn")
        self._cam_idx = QSpinBox(); self._cam_idx.setRange(0, 8); self._cam_idx.setValue(self._default_cam_index()); self._cam_idx.setPrefix("源#")
        bar.addWidget(self._btn_cam); bar.addWidget(self._btn_snap); bar.addWidget(self._cam_idx); bar.addStretch(1)
        g.addLayout(bar)
        lights = QGridLayout(); lights.setSpacing(6)
        for i, key in enumerate(["使能", "运行状态", "急停", "模式", "循环", "机械锁"]):
            box, lbl = _light(key); self._light[key] = lbl
            lights.addWidget(box, i // 6, i % 6)
        g.addLayout(lights)
        return f

    @staticmethod
    def _default_cam_index() -> int:
        """相机源默认值按 station_map.json 角色解析（交接 §9：index 不写死，权威源唯一）：
        优先 `scara_j3`（J3 下移相机，装上登记后自动跟随），未登记退 `right_tray`，再退 0。
        刻意不抛异常：映射错了顶多画面串一串，不该让整个 SCARA 页起不来。"""
        try:
            from orchestrator.ui.peel_camera_view import cam_index_of
            for role in ("scara_j3", "right_tray"):
                idx = cam_index_of(role)
                if idx is not None:
                    return int(idx)
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _kv_row(self, g: QVBoxLayout, key: str) -> QLabel:
        row = QHBoxLayout(); row.setSpacing(6)
        k = QLabel(key); k.setObjectName("kv")
        val = QLabel("—"); val.setObjectName("val"); val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(k); row.addStretch(1); row.addWidget(val)
        g.addLayout(row)
        return val

    def _right(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w); v.setContentsMargins(0, 0, 4, 0); v.setSpacing(9)

        # 关节角度：标签 + 渐变条 + 数值
        f, g = _card("关节角度（实时）")
        for i in range(4):
            row = QHBoxLayout(); row.setSpacing(7)
            lab = QLabel(JOINT_NAME[i]); lab.setObjectName("jname"); lab.setFixedWidth(28)
            bar = QProgressBar(); bar.setObjectName("posbar"); bar.setRange(0, 100); bar.setValue(50); bar.setTextVisible(False)
            self._joint_bar.append(bar)
            val = QLabel("—"); val.setObjectName("val"); val.setFixedWidth(72)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self._joint_v.append(val)
            row.addWidget(lab); row.addWidget(bar, 1); row.addWidget(val)
            g.addLayout(row)
        v.addWidget(f)

        # 末端位姿
        f, g = _card("末端位姿 (TCP)")
        for i in range(6):
            self._pose_v.append(self._kv_row(g, f"{POSE_LABELS[i]} ({POSE_UNIT[i]})"))
        v.addWidget(f)

        # IO 状态
        f, g = _card("IO 状态  (DI 1500 · DO 4096)")
        self._io_lbl = QLabel("DI ON: —    DO ON: —"); self._io_lbl.setObjectName("kv"); g.addWidget(self._io_lbl)
        grid = QGridLayout(); grid.setSpacing(4)
        for i in range(32):
            cell = QFrame(); cell.setObjectName("io"); self._io_cells.append(cell)
            grid.addWidget(cell, i // 16, i % 16)
        g.addLayout(grid); v.addWidget(f)

        # 报警日志
        f, g = _card("报警 / 日志")
        self._log = QPlainTextEdit(); self._log.setObjectName("logbox"); self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(200); self._log.setMinimumHeight(110)
        g.addWidget(self._log); v.addWidget(f, 1)
        return w

    # ------------------------------------------------------------------ #
    def _bind(self) -> None:
        self._btn_conn.clicked.connect(self._on_connect_clicked)
        self._btn_disc.clicked.connect(self._ctrl.disconnect)
        self._btn_en.clicked.connect(self._ctrl.cmd_enable)
        self._btn_dis.clicked.connect(self._ctrl.cmd_disable)
        self._btn_clr.clicked.connect(self._ctrl.cmd_clear_alarm)
        self._btn_home.clicked.connect(self._ctrl.cmd_home)
        self._btn_estop.clicked.connect(self._ctrl.emergency_stop)
        self._mode_grp.idClicked.connect(lambda i: self._ctrl.cmd_set_mode(["T1", "T2", "Execute"][i]))
        self._speed.valueChanged.connect(lambda val: self._ctrl.cmd_set_speed(val))
        self._btn_save_preset.clicked.connect(self._on_save_preset)
        self._btn_goto.clicked.connect(lambda: self._ctrl.cmd_goto_preset(self._preset_combo.currentText()))
        self._btn_del_preset.clicked.connect(lambda: self._ctrl.delete_preset(self._preset_combo.currentText()))
        self._btn_cam.clicked.connect(self._toggle_camera)
        self._btn_snap.clicked.connect(self._snapshot)
        self._ctrl.connection_changed.connect(self._on_conn)
        self._ctrl.status_updated.connect(self._on_status)
        self._ctrl.presets_changed.connect(self._on_presets)
        self._ctrl.error_occurred.connect(lambda m: self._append("错误", m, T.ERROR_COLOR))
        self._ctrl.warning_occurred.connect(lambda m: self._append("警告", m, T.WARNING_COLOR))
        self._ctrl.info_occurred.connect(lambda m: self._append("信息", m, T.TEXT_SECONDARY))
        self._ctrl.command_finished.connect(
            lambda name, ok, msg: self._append("OK" if ok else "失败", name, T.SUCCESS_COLOR if ok else T.ERROR_COLOR))

    def _jog_press(self, axis, direction: str) -> None:
        if self._cont.isChecked():
            self._ctrl.jog_start(axis, direction, world=isinstance(axis, str))

    def _jog_release(self, axis) -> None:
        if self._cont.isChecked():
            self._ctrl.jog_stop(axis)

    def _step(self, axis, sign: int) -> None:
        if self._cont.isChecked():
            return
        if isinstance(axis, str):
            self._ctrl.cmd_cart_step(axis, sign * self._cstep.value())
        else:
            self._ctrl.cmd_move_joint(axis, sign * self._jstep.value())

    def _on_save_preset(self) -> None:
        name = self._preset_name.text().strip()
        if name:
            self._ctrl.save_preset(name); self._preset_name.clear()

    def _on_presets(self, names: list) -> None:
        cur = self._preset_combo.currentText()
        self._preset_combo.clear(); self._preset_combo.addItems(names)
        if cur in names:
            self._preset_combo.setCurrentText(cur)

    def _toggle_camera(self) -> None:
        if self._cam is not None:
            self._cam.stop(); self._cam = None
            self._btn_cam.setText("连接相机"); self._cam_lbl.setText("相机已断开")
            return
        self._cam = ScaraCameraThread(index=self._cam_idx.value())
        self._cam.frame_ready.connect(self._on_frame)
        self._cam.error.connect(lambda m: (self._append("相机", m, T.ERROR_COLOR), setattr(self, "_cam", None), self._btn_cam.setText("连接相机")))
        self._cam.start(); self._btn_cam.setText("断开相机")

    def _on_frame(self, img: QImage) -> None:
        self._cam_lbl.setPixmap(QPixmap.fromImage(img).scaled(
            self._cam_lbl.width(), self._cam_lbl.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _snapshot(self) -> None:
        if self._cam is not None:
            path = datetime.now().strftime("scara_snap_%Y%m%d_%H%M%S.jpg")
            if self._cam.snapshot(path):
                self._append("相机", f"快照已保存 {path}", T.SUCCESS_COLOR)

    def _set_css(self, btn: QPushButton, css: str) -> None:
        """运行时改按钮 cssClass 并重新应用样式。"""
        if (btn.property("cssClass") or "") == css:
            return
        btn.setProperty("cssClass", css)
        btn.style().unpolish(btn); btn.style().polish(btn)

    def _on_connect_clicked(self) -> None:
        # 连接握手（启动 snrobot.exe + 读状态）是阻塞 I/O：若在 GUI 线程直接跑，
        # exe 卡住（如控制器上电未就绪/网络挂起）会冻结整个界面，连急停都点不动。
        # 改用 connect_async 放后台线程；结果经 connection_changed 信号回 UI。
        self._btn_conn.setEnabled(False)
        self._ctrl.connect_async()

    def _on_conn(self, ok: bool) -> None:
        self._conn_chip.setText("● 已连接" if ok else "● 未连接")
        self._conn_chip.setStyleSheet(
            f"color:{T.SUCCESS_COLOR}; background:#E7F6EF;" if ok else f"color:{T.TEXT_MUTED}; background:#EAF0F7;")
        # 连接/断开 按实际连接状态互斥
        self._btn_conn.setEnabled(not ok)
        self._btn_disc.setEnabled(ok)
        # 未连接时控制类按钮全部置灰
        for b in (self._btn_en, self._btn_dis, self._btn_clr, self._btn_home, self._btn_estop):
            b.setEnabled(ok)
        if not ok:
            self._set_css(self._btn_en, "")     # 断开时"使能"不亮绿
            self._btn_dis.setEnabled(False)
            for k in self._light:               # 状态灯复位
                self._set_light(k, "—", True) if False else self._light[k].setText("—")

    def _on_status(self, st: dict) -> None:
        # —— 按钮状态跟随真实状态 —— #
        en = int(st.get("enable", 0))
        self._set_css(self._btn_en, "success" if en == 1 else "")  # 绿色只在已使能时亮
        self._btn_en.setEnabled(True)
        self._btn_dis.setEnabled(en == 1)       # 只有已使能才能"去使能"
        # 模式段跟随实际模式
        idx = {"T1": 0, "T2": 1, "Execute": 2}.get(st.get("mode", ""), None)
        if idx is not None:
            b = self._mode_grp.button(idx)
            if b is not None and not b.isChecked():
                self._mode_grp.blockSignals(True); b.setChecked(True); self._mode_grp.blockSignals(False)

        for i in range(4):
            self._joint_v[i].setText(f"{st['joints'][i]:.2f}")
            lo, hi = JOINT_RANGE[i]
            self._joint_bar[i].setValue(int(max(0, min(100, (st['joints'][i] - lo) / (hi - lo) * 100))))
        for i in range(6):
            self._pose_v[i].setText(f"{st['pose'][i]:.2f}")
        self._speed_bar.setValue(int(round(st.get("speed", 0))))
        en = st.get("enable", 0); warn = st.get("warn", 0)
        self._set_light("使能", "ON" if en == 1 else "OFF", en == 1)
        self._set_light("运行状态", "报警" if warn else "正常", warn == 0)
        self._set_light("急停", "未触发", True)
        self._set_light("模式", st.get("mode", "—"), True, info=True)
        self._set_light("循环", "单循环", True, info=True)
        self._set_light("机械锁", "ON" if st.get("mechlock") else "OFF", st.get("mechlock") == 0)
        self._io_lbl.setText(f"DI ON: {st.get('di_on',0)}    DO ON: {st.get('do_on',0)}")
        do_list = set(st.get("do_list", []))
        for i, cell in enumerate(self._io_cells):
            name = "ioOn" if (i + 1) in do_list else "io"
            if cell.objectName() != name:
                cell.setObjectName(name); cell.style().unpolish(cell); cell.style().polish(cell)

    def _set_light(self, key: str, text: str, good: bool, info: bool = False) -> None:
        lbl = self._light.get(key)
        if not lbl:
            return
        lbl.setText(text)
        color = T.PRIMARY_COLOR if info else (T.SUCCESS_COLOR if good else T.ERROR_COLOR)
        lbl.setStyleSheet(f"font-weight:700; font-size:12px; color:{color};")

    def _append(self, tag: str, msg: str, color: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.appendHtml(f'<span style="color:{T.TEXT_MUTED}">{ts}</span> '
                             f'<span style="color:{color}">[{tag}]</span> {msg}')

    def cleanup(self) -> None:
        try:
            if self._cam is not None:
                self._cam.stop(); self._cam = None
            if self._owns:
                self._ctrl.cleanup()
        except Exception as exc:  # pragma: no cover
            logger.warning("SCARA 控制界面清理失败: %s", exc)
