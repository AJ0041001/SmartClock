"""测试包。

这里做一件很重要的事：**把"保存配置文件"的默认目标重定向到 /dev/null**。

原因是一次真实事故：某些测试没显式指定 ``config_path``，于是 RoiEditor 用了
默认的 ``config.yaml``。只要测试里碰到一次"保存"动作，就会把用户现场辛苦
标定好的 A/B 两个 ROI 覆盖成测试用的假数据 —— 而且用户下次启动才会发现。

有了这道保护，忘记传 ``config_path`` 的测试最多只是"没测到保存"，绝不会
污染真实配置。要测真正的落盘行为，显式传一个临时路径即可。
"""

import os

# setdefault：不覆盖外部已经设好的值，方便个别测试自己指定
os.environ.setdefault("SMARTCLOCK_CONFIG", os.devnull)
