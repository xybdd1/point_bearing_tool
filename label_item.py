# -*- coding: utf-8 -*-
"""
label_item.py - 在画布上绘制测量结果标签

用 QgsMapCanvasItem 实现：标签锚定在地图坐标上，
缩放/平移地图时会自动跟随，不会漂移。
"""

from qgis.core import QgsPointXY
from qgis.gui import QgsMapCanvasItem
from qgis.PyQt.QtCore import QRectF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPen


class MeasureLabelItem(QgsMapCanvasItem):
    """锚定在地图坐标上的多行文字标签"""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.map_point = QgsPointXY()
        self.lines = []
        self.has_content = False
        self.align_above = True

        self.font = QFont('Microsoft YaHei', 10)
        self.font.setBold(True)
        self.fg_color = QColor('#ffffff')
        self.bg_color = QColor(0, 0, 0, 175)
        self.border_color = QColor('#ff8c00')

        self.pad_x = 8.0
        self.pad_y = 5.0
        self.gap = 12.0          # 与锚点之间的距离

        self.setZValue(10000)

    # ---------- 内容 ----------
    def set_label(self, map_point, lines, align_above=True):
        self.map_point = QgsPointXY(map_point)
        self.lines = [str(t) for t in lines]
        self.has_content = bool(self.lines)
        self.align_above = align_above
        self.prepareGeometryChange()
        self.updatePosition()
        self.update()

    def clear(self):
        self.lines = []
        self.has_content = False
        self.update()

    def updatePosition(self):
        """把地图坐标同步到画布像素坐标

        注意：QgsMapCanvasItem 的 toCanvasCoordinates 依赖 canvas 已完成布局，
        且画布有实际尺寸；canvas 尺寸为 0 时会返回 (0,0)，导致标签卡在左上角。
        此时不更新，等画布重绘信号再次触发时补上。
        """
        try:
            if self.canvas is None:
                return
            size = self.canvas.size()
            if size.width() <= 0 or size.height() <= 0:
                return
            pos = self.toCanvasCoordinates(self.map_point)
            if pos.isNull() and not self.map_point.isNull():
                # 极少数情况下转换结果仍是 0,0，跳过以免标签跑到左上角
                pass
            self.setPos(pos)
        except (RuntimeError, AttributeError):
            pass

    # ---------- 几何 ----------
    def _metrics(self):
        fm = QFontMetricsF(self.font)
        line_h = fm.height()
        width = max((fm.horizontalAdvance(t) for t in self.lines), default=0.0)
        box_w = width + self.pad_x * 2
        box_h = line_h * max(len(self.lines), 1) + self.pad_y * 2
        return fm, line_h, box_w, box_h

    def boundingRect(self):
        _, _, box_w, box_h = self._metrics()
        # 本地坐标原点 = 锚点，向外扩一圈留出边框和箭头
        return QRectF(-box_w / 2 - 4, -box_h - self.gap - 4,
                      box_w + 8, box_h + self.gap + 8)

    def itemChange(self, change, value):
        """画布重绘/变换后重新同步位置，保证平移缩放时标签贴住锚点"""
        if change == QgsMapCanvasItem.GraphicsItemChange.ItemSceneHasChanged:
            self.updatePosition()
        return super().itemChange(change, value)

    def paint(self, painter, option=None, widget=None):
        if not self.has_content:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(self.font)

        fm, line_h, box_w, box_h = self._metrics()

        if self.align_above:
            rect = QRectF(-box_w / 2, -box_h - self.gap, box_w, box_h)
            tip_y = 0.0
        else:
            rect = QRectF(-box_w / 2, self.gap, box_w, box_h)
            tip_y = 0.0

        # 指向锚点的小三角
        triangle = [
            _pt(0, tip_y),
            _pt(-6, tip_y - self.gap if self.align_above else tip_y + self.gap),
            _pt(6, tip_y - self.gap if self.align_above else tip_y + self.gap),
        ]

        painter.setPen(QPen(self.border_color, 1.3))
        painter.setBrush(QBrush(self.bg_color))
        painter.drawRoundedRect(rect, 4, 4)

        painter.setBrush(QBrush(self.bg_color))
        painter.setPen(QPen(self.border_color, 1.0))
        painter.drawPolygon(*triangle)

        painter.setPen(QPen(self.fg_color))
        y = rect.top() + self.pad_y
        for line in self.lines:
            painter.drawText(
                QRectF(rect.left() + self.pad_x, y, box_w - self.pad_x * 2, line_h),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                line)
            y += line_h


def _pt(x, y):
    from qgis.PyQt.QtCore import QPointF
    return QPointF(x, y)
