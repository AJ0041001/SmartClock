# -*- coding: utf-8 -*-
"""把 Roboflow 多边形(分割)标签转成 YOLO 检测矩形框，并生成干净数据集。"""
import os
import shutil

SRC = "D:/桌面/redcard.yolo26(1)"
DST = "C:/Users/ROG/card_dataset"

splits = ["train", "valid", "test"]

for split in splits:
    src_img = os.path.join(SRC, split, "images")
    src_lbl = os.path.join(SRC, split, "labels")
    dst_img = os.path.join(DST, split, "images")
    dst_lbl = os.path.join(DST, split, "labels")
    os.makedirs(dst_img, exist_ok=True)
    os.makedirs(dst_lbl, exist_ok=True)

    # 复制图片
    n_img = 0
    for f in os.listdir(src_img):
        shutil.copy2(os.path.join(src_img, f), os.path.join(dst_img, f))
        n_img += 1

    # 转换标签
    n_lbl = 0
    for f in os.listdir(src_lbl):
        with open(os.path.join(src_lbl, f), encoding="utf-8") as fp:
            lines = fp.readlines()
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            t = line.split()
            cls = t[0]
            vals = [float(x) for x in t[1:]]
            if len(vals) == 4:
                # 已经是矩形框，原样保留
                out.append(cls + " " + " ".join(f"{v:.6f}" for v in vals))
            else:
                # 多边形 -> 外接矩形框
                xs = vals[0::2]
                ys = vals[1::2]
                xmin, xmax = min(xs), max(xs)
                ymin, ymax = min(ys), max(ys)
                cx = (xmin + xmax) / 2.0
                cy = (ymin + ymax) / 2.0
                w = xmax - xmin
                h = ymax - ymin
                out.append(f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        with open(os.path.join(dst_lbl, f), "w", encoding="utf-8") as fp:
            fp.write("\n".join(out) + "\n")
        n_lbl += 1

    print(f"{split}: images={n_img}, labels={n_lbl}")

# 生成 data.yaml（绝对路径，避免中文/相对路径问题）
yaml = """train: C:/Users/ROG/card_dataset/train/images
val: C:/Users/ROG/card_dataset/valid/images
test: C:/Users/ROG/card_dataset/test/images

nc: 1
names: ['card']
"""
with open(os.path.join(DST, "data.yaml"), "w", encoding="utf-8") as fp:
    fp.write(yaml)

print("done ->", DST)
