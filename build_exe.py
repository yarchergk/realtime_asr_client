"""
语音转文字应用打包脚本
使用 PyInstaller 将 realtime_asr_client.py 打包成独立可执行文件

使用方法:
    pip install pyinstaller
    python build_exe.py
"""

import PyInstaller.__main__
import os

# 获取脚本所在目录(TTSV2文件夹)
current_dir = os.path.dirname(os.path.abspath(__file__))

# 主脚本的绝对路径
main_script = os.path.join(current_dir, 'realtime_asr_client.py')

# 检查文件是否存在
if not os.path.exists(main_script):
    print(f"错误: 找不到文件 {main_script}")
    print(f"当前目录: {current_dir}")
    exit(1)

# .env.example 文件路径
env_example = os.path.join(current_dir, '.env.example')
add_data_arg = f'--add-data={env_example};.' if os.path.exists(env_example) else None

# 构建参数列表
args = [
    main_script,                         # 主脚本(使用绝对路径)
    '--name=语音转文字',                 # 可执行文件名称
    '--windowed',                        # GUI模式,不显示控制台窗口
    '--onefile',                         # 打包成单个exe文件
    '--clean',                           # 清理临时文件
    '--noconfirm',                       # 覆盖输出目录时不询问
]

# 添加数据文件(如果存在)
if add_data_arg:
    args.append(add_data_arg)

# 添加隐藏导入
args.extend([
    '--hidden-import=pyaudiowpatch',
    '--hidden-import=pyaudio',
    '--hidden-import=aiohttp',
    '--hidden-import=dotenv',
])

# 添加输出目录配置
args.extend([
    f'--distpath={os.path.join(current_dir, "dist")}',
    f'--workpath={os.path.join(current_dir, "build")}',
    f'--specpath={current_dir}',
])

print(f"开始打包...")
print(f"主脚本: {main_script}")
print(f"输出目录: {os.path.join(current_dir, 'dist')}")

# 运行PyInstaller
PyInstaller.__main__.run(args)

print("\n打包完成!")
print(f"可执行文件位置: {os.path.join(current_dir, 'dist', '语音转文字.exe')}")
print("\n使用说明:")
print("1. 将 .env 文件复制到 exe 文件同目录")
print("2. 双击运行 语音转文字.exe")
