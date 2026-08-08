"""
SCARA 工位相机取帧线程（OpenCV / DirectShow）

现有相机模块面向海康 MVS / 图漫工业相机；SCARA 工位接的是 USB 相机，
故用 OpenCV VideoCapture 取帧，复用「numpy 帧 → QImage → QLabel」显示方式。
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

from utils import get_logger

logger = get_logger("scara.camera")


class ScaraCameraThread(QThread):
    """后台采集 USB 相机帧并转 QImage 发出。"""

    frame_ready = pyqtSignal(QImage)
    error = pyqtSignal(str)

    def __init__(self, index: int = 0, width: int = 1280, height: int = 720, parent=None):
        super().__init__(parent)
        self._index = index
        self._w, self._h = width, height
        self._running = False
        self._last_frame = None

    def run(self) -> None:
        try:
            import cv2
        except Exception as exc:
            self.error.emit(f"未安装 opencv-python: {exc}")
            return
        cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.error.emit(f"无法打开相机 index={self._index}")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._h)
        self._running = True
        while self._running:
            ok, frame = cap.read()
            if not ok:
                self.msleep(30); continue
            self._last_frame = frame
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            img = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy()
            self.frame_ready.emit(img)
            self.msleep(33)   # ~30fps
        cap.release()

    def stop(self) -> None:
        self._running = False
        self.wait(1500)

    def snapshot(self, path: str) -> bool:
        if self._last_frame is None:
            return False
        try:
            import cv2
            cv2.imwrite(path, self._last_frame)
            return True
        except Exception:
            return False
