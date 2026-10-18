"""入口：启动本地服务并自动打开浏览器。用法：python app.py"""
import threading
import webbrowser

import uvicorn

from dedup.server import create_app

HOST = "127.0.0.1"
PORT = 8765


def main() -> None:
    url = f"http://{HOST}:{PORT}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"照片/视频去重工具已启动：{url}（关闭本窗口或按 Ctrl+C 退出）")
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
