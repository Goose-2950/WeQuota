"""打包配置与发布构建脚本（PyInstaller）。

目录命名 installer/ 而非 packaging/ —— 避免与 PyPI 的 packaging 库撞名
（PyInstaller 自身依赖它，同名目录会把它遮蔽导致打包崩溃）。
"""
