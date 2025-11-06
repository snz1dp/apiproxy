# /*********************************************
#                    _ooOoo_
#                   o8888888o
#                   88" . "88
#                   (| -_- |)
#                   O\  =  /O
#                ____/`---'\____
#              .'  \\|     |//  `.
#             /  \\|||  :  |||//  \
#            /  _||||| -:- |||||-  \
#            |   | \\\  -  /// |   |
#            | \_|  ''\---/''  |   |
#            \  .-\__  `-`  ___/-. /
#          ___`. .'  /--.--\  `. . __
#       ."" '<  `.___\_<|>_/___.'  >'"".
#      | | :  `- \`.;`\ _ /`;.`/ - ` : | |
#      \  \ `-.   \_ __\ /__ _/   .-` /  /
# ======`-.____`-.___\_____/___.-`____.-'======
#                    `=---='

# ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#            佛祖保佑       永无BUG
#            心外无法       法外无心
#            三宝弟子       三德子宏愿
# *********************************************/

import asyncio
import threading
from multiprocess import cpu_count

# 运行异步任务并等待完成
def run_until_complete(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Run the coroutine in a separate event loop in a new thread
            return run_in_thread(coro)
        return loop.run_until_complete(coro)
    except RuntimeError:
        # If there's no event loop, create a new one and run the coroutine
        return asyncio.run(coro)

# 在新线程中运行协程
def run_in_thread(coro):
    result = None
    exception = None

    def target() -> None:
        nonlocal result, exception
        try:
            result = asyncio.run(coro)
        except BaseException as e:  # noqa: BLE001
            exception = e

    thread = threading.Thread(target=target)
    thread.start()

    try:
        thread.join()
    except Exception as e:
        pass

    if exception:
        raise exception
    return result

def get_number_of_workers(workers=None):
    if workers == -1 or workers is None:
        workers = (cpu_count() * 2) + 1
    return workers
