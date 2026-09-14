# -*- coding: utf-8 -*-
"""把 best.onnx 转成 best.rknn（鲁班猫4 / RK3588，FP16 先求稳）。"""
from rknn.api import RKNN

rknn = RKNN(verbose=True)

# 预处理：YOLO 是 /255 归一化，不做 mean 减
rknn.config(
    mean_values=[[0, 0, 0]],
    std_values=[[255, 255, 255]],
    target_platform='rk3588',
)

# 加载 ONNX（输入固定 640x640）
ret = rknn.load_onnx(model='best.onnx')
if ret != 0:
    print('load_onnx failed! ret =', ret)
    exit(ret)

# 构建：先 FP16（do_quantization=False），稳定；想加速再改 INT8
ret = rknn.build(do_quantization=False)
if ret != 0:
    print('build failed! ret =', ret)
    exit(ret)

# 导出 rknn
ret = rknn.export_rknn('best.rknn')
if ret != 0:
    print('export_rknn failed! ret =', ret)
    exit(ret)

print('done -> best.rknn')
rknn.release()
