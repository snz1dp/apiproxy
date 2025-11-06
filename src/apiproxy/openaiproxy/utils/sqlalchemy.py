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

def parse_orderby_column(cls, orderby, default_orderby = None):
    if not orderby:
        if isinstance(default_orderby, str):
            orderby = default_orderby
        else:
            return default_orderby
    orderby_expr = str.split(orderby, " ")
    col = getattr(cls, orderby_expr[0])
    if len(orderby_expr) > 1 and str.lower(orderby_expr[1]) == "desc":
        col = col.desc()
    elif len(orderby_expr) > 1 and str.lower(orderby_expr[1]) == "asc":
        col = col.asc()
    return col
