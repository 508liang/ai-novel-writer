"""
AI 网文写作系统 打包脚本

使用方法：
    conda activate python_class
    pip install pyinstaller
    python build.py

生成文件：
    dist/AI网文写作系统.exe
"""

import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent


def main():
    print("=" * 60)
    print("AI 网文写作系统 打包工具")
    print("=" * 60)

    # 检查 PyInstaller
    try:
        import PyInstaller
        print(f"[OK] PyInstaller 版本：{PyInstaller.__version__}")
    except ImportError:
        print("[!] PyInstaller 未安装，正在安装...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        print("[OK] PyInstaller 安装完成")

    # 打包参数
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",                    # 单文件
        "--windowed",                   # 无控制台窗口
        "--name", "AI网文写作系统",     # exe 名称
        "--clean",                      # 清理临时文件
        "--noconfirm",                  # 覆盖旧打包缓存时不询问
        # 隐藏导入
        "--hidden-import", "tkinter",
        "--hidden-import", "json",
        "--hidden-import", "logging",
        "--hidden-import", "threading",
        "--hidden-import", "urllib.request",
        "--hidden-import", "concurrent.futures",
        "--hidden-import", "project_store",
        "--hidden-import", "memory_index",
        # 入口文件
        str(PROJECT_DIR / "gui.py"),
    ]

    # 如果有图标文件，添加图标
    icon_path = PROJECT_DIR / "icon.ico"
    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])

    print(f"\n[i] 开始打包...")
    print(f"    命令：{' '.join(cmd)}\n")

    try:
        result = subprocess.run(cmd, cwd=str(PROJECT_DIR), check=True)

        exe_path = PROJECT_DIR / "dist" / "AI网文写作系统.exe"
        if exe_path.exists():
            size_mb = exe_path.stat().st_size / (1024 * 1024)
            print(f"\n{'=' * 60}")
            print(f"[OK] 打包成功！")
            print(f"    输出：{exe_path}")
            print(f"    大小：{size_mb:.1f} MB")
            print(f"{'=' * 60}")
            print(f"\n注意：")
            print(f"  1. 首次运行会在 data/projects/默认项目/ 生成项目配置")
            print(f"  2. 故事素材文件(story_plan.md等)会在项目目录中生成")
            print(f"  3. 所有项目数据位于 exe 同目录的 data/projects/ 文件夹")
        else:
            print("[X] 打包完成但未找到 exe 文件")

    except subprocess.CalledProcessError as e:
        print(f"[X] 打包失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
