# -*- coding: utf-8 -*-
"""分析 debug_window.png 的区域像素构成,推断页面显示状态。"""
import numpy as np
from PIL import Image

img = Image.open("debug_window.png")
arr = np.asarray(img.convert("RGB"), dtype=np.int16)
h, w = arr.shape[:2]
print(f"图片 {w}x{h}")


def region_stat(name, y1, y2, x1=100, x2=1000):
    r = arr[y1:y2, x1:x2]
    gray = r.mean(axis=2)
    dark = (gray < 150)           # 文字像素
    blue = ((r[:, :, 2] > 120) & (r[:, :, 2] - r[:, :, 0] > 50)
            & (r[:, :, 2] - r[:, :, 1] > 25))   # 选中蓝
    print(f"{name}: y{y1}~{y2}  均值={gray.mean():.0f}  深色像素={dark.sum()}"
          f"({dark.mean()*100:.2f}%)  蓝色像素={blue.sum()}")
    # 深色像素的行分布(每行数量,采样打印)
    rows = dark.sum(axis=1)
    nz = [(i + y1, int(c)) for i, c in enumerate(rows) if c > 5]
    if nz:
        print(f"   文字行: {nz[:20]}")
    else:
        print("   (无文字行)")


# Q1 题干下方选项区(当前页面为顶部视图)
region_stat("Q1题干行", 355, 385)
region_stat("Q1选项区", 390, 645)
# Q2 题干下方(贴近视口底部)
region_stat("Q2题干下", 690, 750)
