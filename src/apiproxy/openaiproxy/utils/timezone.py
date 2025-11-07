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

from datetime import timedelta, timezone, datetime
import os

_timezone_map = {
	"Asia/Shanghai": timezone(timedelta(hours=8)),
}

def current_timezone():
    zone_name = os.getenv("TZ", "Asia/Shanghai")
    return _timezone_map[zone_name] if _timezone_map[zone_name] else current_timezone()

def current_time_in_timezone():
    return datetime.now(tz=current_timezone())

# ISO8601格式化时间
def iso8601_date_format(date: datetime):
    if date is None:
        return None
    off = date.utcoffset()
    s = ''
    if off is not None:
        if off.days < 0:
            sign = "-"
            off = -off
        else:
            sign = "+"
        hh, mm = divmod(off, timedelta(hours=1))
        mm, ss = divmod(mm, timedelta(minutes=1))
        s += "%s%02d%02d" % (sign, hh, mm)
    else:
        s += "+0000"
    return date.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03}".format(date.microsecond // 1000) + s
