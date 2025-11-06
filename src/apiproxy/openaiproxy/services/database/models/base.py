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

import orjson

def orjson_dumps(v, *, default=None, sort_keys=False, indent_2=True):
    option = orjson.OPT_SORT_KEYS if sort_keys else None
    if indent_2:
        # orjson.dumps returns bytes, to match standard json.dumps we need to decode
        # option
        # To modify how data is serialized, specify option. Each option is an integer constant in orjson.
        # To specify multiple options, mask them together, e.g., option=orjson.OPT_STRICT_INTEGER | orjson.OPT_NAIVE_UTC
        if option is None:
            option = orjson.OPT_INDENT_2
        else:
            option |= orjson.OPT_INDENT_2
    try:
        if default is None:
            return orjson.dumps(v, option=option).decode()
        return orjson.dumps(v, default=default, option=option).decode()
    except Exception as e:
        raise ValueError(f"序列化时出错了: {str(e)}") from e