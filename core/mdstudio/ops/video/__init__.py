"""视频 -> 动画. 依赖重 (opencv / rembg / 可选 Real-ESRGAN), 全部在函数里懒加载

pip install mdstudio[video]          抠图
pip install mdstudio[video,sharp]    + 超分 (要 CUDA, 设 MDSTUDIO_ESRGAN 指向 anime_6B 权重)
"""
