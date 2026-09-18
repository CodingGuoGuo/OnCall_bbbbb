"""日志配置模块

使用 Loguru 配置应用日志
"""

import sys
from loguru import logger
from app.config import config


def _show_console_window() -> None:
    """Windows 专用：把「被隐藏的控制台窗口」显示出来。

    进程被后台/无窗口方式启动时（IDE、脚本、计划任务、start /min 等），
    控制台窗口的 IsWindowVisible 为 False —— 日志其实一直在往里写，只是没人看得见，
    表现就是「日志只出现在 log 文件里、控制台毫无反应」。这里显式把它显示出来。

    可用 .env 里的 SHOW_CONSOLE_WINDOW=false 关掉这个行为。
    """
    if sys.platform != "win32" or not config.show_console_window:
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        kernel32.GetConsoleWindow.restype = ctypes.c_void_p
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]

        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return  # 压根没有控制台（pythonw / DETACHED_PROCESS），无从显示
        if not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetWindowTextW(hwnd, f"{config.app_name} 运行日志")
    except Exception:
        pass


def setup_logger():
    """配置日志系统

    按照 Loguru 最佳实践配置全局 logger：
    1. 移除默认处理器
    2. 添加控制台输出（带颜色）
    3. 添加文件输出（按天轮转，自动压缩，异步写入）
    """
    # 先把隐藏的控制台窗口显示出来，否则下面的控制台输出等于石沉大海
    _show_console_window()

    # 移除默认处理器
    logger.remove()

    # 控制台输出（带颜色格式）
    # 用 sys.__stdout__（真正的终端句柄）而不是 sys.stdout：
    #   - 即使有库（uvicorn 等）替换/包装了 sys.stdout，终端照样能收到日志；
    #   - loguru 在 Windows 上只有目标是 sys.__stdout__/__stderr__ 时才会启用 colorama
    #     包装，cmd.exe 里的颜色才不会变成乱码。
    console_sink = sys.__stdout__ if sys.__stdout__ is not None else sys.stdout
    if console_sink is not None:
        logger.add(
            console_sink,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{module}</cyan>.<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",
            level="DEBUG" if config.debug else "INFO",
            colorize=True,
            backtrace=True,  # 显示完整异常栈信息
            diagnose=config.debug,  # Debug 模式下显示变量值
        )

    # 添加文件输出（按天轮转，自动压缩）
    logger.add(
        "logs/app_{time:YYYY-MM-DD}.log",
        rotation="00:00",  # 每天0点自动切割新日志文件
        retention="7 days",  # 仅保留最近7天的日志
        compression="zip",  # 过期日志自动压缩为zip
        encoding="utf-8",  # 解决中文乱码
        enqueue=True,  # 异步写入，提升性能（避免IO阻塞）
        backtrace=True,  # 显示完整异常栈信息
        diagnose=True,  # 显示变量值，便于调试
        level="INFO",  # 文件里只留 INFO 及以上（DEBUG 级只进控制台，避免日志文件过大）
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {module}.{function}:{line} | {message}",
    )

setup_logger()
