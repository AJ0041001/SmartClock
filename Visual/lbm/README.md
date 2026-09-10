# 鲁班猫4 卡片识别（YOLO26 → RKNN）

把 YOLO26 训练出来的卡片检测模型，转换成鲁班猫4（RK3588S）NPU 能跑的 `best.rknn` 并推理。

## 文件说明

| 文件 | 作用 | 运行环境 |
|------|------|----------|
| `convert_cards.py` | Roboflow 多边形标签 → YOLO 矩形框，生成数据集 | Windows（Python） |
| `convert_rknn.py` | ONNX → RKNN（FP16，target= rk3588） | WSL Ubuntu，rknn-toolkit2 2.3.2 |
| `rknn_card_infer.py` | 板子端推理脚本（检测 + 画框 + 存图） | 鲁班猫4，rknnlite2 |

## 完整流程

1. **训练**（Windows，yolo8 环境，ultralytics 8.4.x）
   ```bash
   yolo detect train model=yolo26n.pt data=data.yaml epochs=150 imgsz=640
   ```
2. **导出 ONNX**：`yolo export model=best.pt format=onnx imgsz=640`
3. **转 RKNN**（WSL，`toolkit2` 环境，Python 3.11）
   ```bash
   python convert_rknn.py   # 生成 best.rknn（FP16）
   ```
4. **板子部署**（鲁班猫4）
   ```bash
   sudo cp librknnrt.so librknn_api.so /usr/lib/ && sudo ldconfig
   pip3 install rknn_toolkit_lite2-2.3.2-*.whl
   python3 rknn_card_infer.py 图片.jpg   # 输出 result.jpg
   ```

## 关键参数

- 输入 `640x640`，预处理 `/255`（mean=0, std=255），RGB NHWC
- 模型输出 `[1, 5, 8400]` = 4 框坐标(xyxy) + 1 类别分（单类 `card`）
- 推理脚本里 `CONF=0.5`（漏检调低）、`NMS=0.45`
- 3 核 NPU：`core_mask=NPU_CORE_0_1_2`
