"""PyInstaller 打包入口： frozen 后直接调 CLI main。

frozen 模式下 sys.executable 指向打包产物本身，default_launch_command()
与 resources.data_dir() 都靠 sys.frozen / sys._MEIPASS 识别。
"""

from wequota.cli import main

if __name__ == "__main__":
    main()
