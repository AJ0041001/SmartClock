# -*- coding: utf-8 -*-
"""在鲁班猫4(RK3588)上跑 best.rknn 做卡片检测。
用法:  python3 rknn_card_infer.py 图片.jpg
输出:  保存 result.jpg（画框后的图），并在终端打印每个框的置信度。
"""
import sys
import cv2
import numpy as np
from rknnlite.api import RKNNLite

MODEL = "best.rknn"
IMG_SIZE = 640
CONF = 0.5      # 置信度阈值，漏检就调低
NMS = 0.45      # 重叠框去重阈值
CLASSES = ["card"]


def letterbox(img, size=IMG_SIZE):
    """等比缩放到 size，四周灰边补齐（和训练时的 ultralytics 预处理一致）。"""
    h, w = img.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    resized = cv2.resize(img, (nw, nh))
    top = (size - nh) // 2
    left = (size - nw) // 2
    canvas = cv2.copyMakeBorder(resized, top, size - nh - top,
                                left, size - nw - left,
                                cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return canvas, scale, top, left


def main(path):
    rknn = RKNNLite()
    rknn.load_rknn(MODEL)
    rknn.init_runtime(core_mask=RKNNLite.NPU_CORE_0_1_2)  # 3 核 NPU

    img0 = cv2.imread(path)                       # BGR
    img, scale, top, left = letterbox(img0)
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)    # 转 RGB, NHWC uint8

    out = rknn.inference(inputs=[img[None, ...]], data_format="nhwc")[0]
    # 输出 shape: (1, 5, 8400) = 4 个框坐标(x1,y1,x2,y2) + 1 个类别分
    boxes = out[0, :4, :].T          # (8400, 4)
    scores = out[0, 4, :]            # (8400,)

    keep = scores > CONF
    boxes, scores = boxes[keep], scores[keep]
    if len(boxes) == 0:
        print("没检测到卡片（可调低 CONF）")
        return

    # 框坐标从 640 图还原到原图
    boxes = (boxes - np.array([left, top, left, top])) / scale

    idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), CONF, NMS)
    if len(idx):
        idx = idx.flatten()
        boxes, scores = boxes[idx], scores[idx]

    for b, s in zip(boxes, scores):
        x1, y1, x2, y2 = [int(v) for v in b]
        cv2.rectangle(img0, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(img0, f"card {s:.2f}", (x1, max(0, y1 - 5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        print(f"card  {s:.3f}  [{x1},{y1},{x2},{y2}]")

    cv2.imwrite("result.jpg", img0)
    print("已保存 result.jpg")
    rknn.release()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 rknn_card_infer.py 图片.jpg")
        sys.exit(1)
    main(sys.argv[1])
