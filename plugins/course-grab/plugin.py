"""教务抢课插件:持续轮询北师大教务系统课程名额并自动提交。

配置示例:
{
  "courses": {"GEN06122": ["09","10"], "GEN01103": ["27"]},
  "course_order": [
    {"code": "GEN06122", "class_num": "09"},
    {"code": "GEN01103", "class_num": "27"}
  ],
  "poll_interval": 3,
  "headless": true,
  "username": "", "password": ""
}

task 配置项由主程序 API 注入,用于抓取任务(与正常抢课互斥,复用同一进程槽位):
- "catalog": 枚举 培养层次×年级×院(系)/部, 抓取全部专业目录
             → majors.json(全局共用一份)
- "courses": 抓取指定专业(specialty 配置)的开课课程列表
             → courses/<key>.json(每个用户通常只需自己专业的一份缓存)
- 其他/缺省: 持续轮询抢课流程
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from _sdk.sdk import (  # noqa: E402
    PluginContext, make_driver, find_strict, sleepran,
)
from app.core.paths import PLUGIN_DATA_DIR  # noqa: E402
from selenium.common.exceptions import (  # noqa: E402
    StaleElementReferenceException, WebDriverException,
)
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import Select, WebDriverWait  # noqa: E402

LOGIN_URL = "http://" + "zy" + "fw.bnu.edu.cn/"

# 课程安排明细入口: frmDesk 内可点击的是 module1974 内的 title1974 元素
# (module1974 本身不可点击); 点击后出现 sel_specialty / kkdw_range_all / frmReport
DESK_MENU_XPATH = '//*[@id="title1974"]'
SPECIALTY_SPAN_XPATH = '//*[@id="sel_specialty"]'
ALL_RANGE_XPATH = '//*[@id="kkdw_range_all"]'
REPORT_FRAME_XPATH = '//*[@id="frmReport"]'
DESK_FRAME_XPATH = '//*[@id="frmDesk"]'
COURSE_TASK_STEPS = [
    {"key": "login", "label": "登录教务系统"},
    {"key": "query", "label": "检索专业课程"},
    {"key": "pages", "label": "读取课程分页"},
    {"key": "save", "label": "保存课程缓存"},
]

# frmReport 内的"加载中"提示(出现表示查询进行中, 消失表示本页数据就绪)
LOADING_XPATH = '//*[@id="keywords"]/tbody/div/span'
TOTAL_PAGE_XPATH = '//*[@id="totalPage"]'
CURRENT_PAGE_XPATH = '//*[@id="currentPage"]'
NEXT_XPATH = '//*[@id="next"]'
KEYWORDS_XPATH = '//*[@id="keywords"]'

# 课程名单元格中提取课程代码(如 GEN06122 / PHY12003 / GE610185371)
CODE_RE = re.compile(r"[A-Za-z]{1,10}\d{3,12}")

COURSE_HEADERS = {
    "课程": "name",
    "学分": "credits",
    "课程类别": "category",
    "上课班号": "class_num",
    "限选人数": "limit",
    "可选人数": "available",
    "周次": "weeks",
    "任课教师": "teacher",
    "上课时间": "time",
    "上课地点": "location",
}
CODE_PREFIX_RE = re.compile(
    r"^\s*\[\s*([A-Za-z]{1,10}\d{3,12})\s*\]\s*", re.IGNORECASE)

# 选项过滤: 站点"请选择"占位项实际是空标签 <option value=""></option>(首项且默认),
# 空 value/空文本已在 _read_options 中过滤; 这里再兜底过滤个别带文字的占位项
_PLACEHOLDER_TEXTS = {"请选择", "--请选择--", "全部"}

# 培养层次/年级/院(系)/部 三个下拉相互独立(改上级不会刷新下级), 只有"专业"
# 下拉依赖前三项的组合; 选定前三项后专业列表刷新极快, 等 0.2s 即可
MAJOR_SLEEP = 0.2

# 目录抓取检查点间隔: 每完成 50 个 培养层次×年级×院系 组合原子落盘一次,
# 网络中断/进程被杀时最多损失 50 个组合的进度
CHECKPOINT_EVERY = 50

# 任务级自动重启上限: 瞬时异常(网络抖动/会话失效/stale 残留)后丢弃浏览器会话、
# 重新登录并从检查点续抓; 超过后任务失败, 已抓组合靠检查点保留可手动重试
CATALOG_MAX_RESTARTS = 5

CATALOG_FILE = PLUGIN_DATA_DIR / "course-grab" / "majors.json"


def course_key(level: str, grade: str, dept: str, major: str) -> str:
    """课程列表缓存文件键: sha1("level|grade|dept|major") 前 16 位。

    注意: app/core/course_cache.py 有一份相同实现, 修改时两处必须同步。
    """
    raw = "|".join(str(x) for x in (level, grade, dept, major))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _save_json_atomic(path: Path, data: dict) -> None:
    """先写临时文件再原子替换, 避免抓取中途被杀导致缓存文件损坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_partial_catalog() -> dict | None:
    """读取本地目录缓存供断点续抓; 缺失/损坏/已完整时返回 None(全量重抓)。

    只认 partial=true 的目录: 已完整(partial=false)时不续抓——完整目录没有
    "重新抓取"入口, 若将来开放刷新入口, 应显式清空缓存后再抓。
    """
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (not isinstance(data, dict) or data.get("partial") is not True
            or not isinstance(data.get("levels"), list)):
        return None
    return data


# ---------------- 登录与页面导航 ----------------

def _open_desk_menu(browser, desk_xpath: str) -> None:
    """直接在 frmDesk 中打开目标模块，避开流程图的透明遮罩层。

    站点流程图的实际点击事件绑定在 module 容器上，而 title 节点处于隐藏
    图层。直接调用菜单对象的 linkfile 可以稳定打开同一模块，并保留当前
    登录会话。找不到菜单元数据时仍回退到常规点击。
    """
    browser.switch_to.frame(find_strict(browser, DESK_FRAME_XPATH))
    target = find_strict(browser, desk_xpath)
    module = target.find_element(By.XPATH, "..")
    menu_code = (module.get_attribute("menuid") or "").strip()
    opened = False
    if menu_code:
        opened = bool(browser.execute_script(
            """
            const code = arguments[0];
            const host = window.parent;
            try {
              const item = host.kingo_usermenubean.getMenuItemByMenucode(code);
              const link = item && item.getLinkfile();
              if (!link) return false;
              const frame = host.document.getElementById('frmDesk');
              let url = new URL(link, host.location.href).href;
              url += (url.includes('?') ? '&' : '?') + 'menucode=' + code;
              frame.src = url;
              return true;
            } catch (error) {
              return false;
            }
            """,
            menu_code,
        ))
    if not opened:
        try:
            target.click()
        except WebDriverException:
            browser.execute_script("arguments[0].click();", module)

    browser.switch_to.default_content()
    WebDriverWait(browser, 30).until(
        lambda d: d.execute_script(
            """
            const frame = document.getElementById('frmDesk');
            try {
              return !!(frame && frame.contentDocument
                && frame.contentDocument.getElementById('sel_specialty'));
            } catch (error) {
              return false;
            }
            """
        )
    )
    browser.switch_to.frame(find_strict(browser, DESK_FRAME_XPATH))


def login(ctx, browser, desk_xpath: str = '//*[@id="title1785"]') -> None:
    """统一身份认证登录, 并进入 frmDesk 桌面后点击 desk_xpath 指定的功能入口。

    desk_xpath 默认是原抢课流程的选课入口 title1785;
    抓取任务传 DESK_MENU_XPATH(课程安排明细入口 title1974)。
    """
    browser.get(LOGIN_URL)
    usr = find_strict(browser, '//*[@id="loginForm"]/div[1]/div/input')
    usr.send_keys(ctx.username)
    time.sleep(0.1)
    psw = find_strict(browser, '//*[@id="loginForm"]/div[2]/div/input')
    psw.send_keys(ctx.password)
    time.sleep(0.1)
    psw.send_keys(Keys.RETURN)
    browser.switch_to.window(browser.window_handles[-1])
    find_strict(browser, '//*[@id="collapseJW93"]/div/ul/li[5]').click()
    _open_desk_menu(browser, desk_xpath)
    sleepran(1)


def goto_course_frame(browser):
    """从任意状态回到: window -> frmDesk -> frmReport 的选课列表。"""
    browser.switch_to.window(browser.window_handles[-1])
    browser.switch_to.frame(find_strict(browser, DESK_FRAME_XPATH))
    find_strict(browser, '//*[@id="btnQry"]').click()
    sleepran(1)
    browser.switch_to.frame(find_strict(browser, REPORT_FRAME_XPATH))


# ---------------- 专业下拉工具 ----------------

def _specialty_selects(browser) -> tuple:
    """返回 sel_specialty 下的四个下拉: (培养层次, 年级, 院系, 专业)。"""
    span = find_strict(browser, SPECIALTY_SPAN_XPATH)
    raw = span.find_elements(By.TAG_NAME, "select")
    if len(raw) < 4:
        raise RuntimeError(f"专业下拉数量异常: 期望 4 个, 实际 {len(raw)} 个")
    return tuple(Select(s) for s in raw[:4])


def _read_options(sel: Select, attempts: int = 4) -> list[dict]:
    """读取下拉的有效选项, 返回 [{"v": value, "t": 显示文本}, ...]。

    站点占位项是空标签 <option value=""></option>(首项且默认), 空 value/空文本
    在此过滤; 另兜底过滤"请选择/全部"等带文字的占位项。
    返回空列表是正常情况(如"夜大"培养层次下"武装部"这类无专业的冗余组合)。

    站点会异步重渲染下拉: 在"枚举 option 引用"与"逐个读文本"之间内容被替换
    会抛 StaleElementReferenceException(抓取中期偶发)。此时等站点本次刷新
    结束后重新获取 options 再读, 最多 attempts 次; 仍失败则向上抛, 由
    run_catalog 的任务级自动重启兜底。
    """
    last_err: Exception | None = None
    for i in range(attempts):
        if i:
            time.sleep(0.5 * i)   # 0.5s/1s/1.5s 递增, 等本次异步刷新结束
        try:
            out = []
            for o in sel.options:
                v = (o.get_attribute("value") or "").strip()
                t = (o.text or "").strip()
                if not v or not t or t in _PLACEHOLDER_TEXTS:
                    continue
                out.append({"v": v, "t": t})
            return out
        except StaleElementReferenceException as e:
            last_err = e
    raise last_err   # 循环耗尽必有 last_err(仅 stale 会走到这里)


def _select(sel: Select, value: str) -> None:
    """选中下拉项。

    培养层次/年级/院(系)/部三者相互独立(改上级不会刷新下级), 因此选中后
    无需等待任何联动; 值已是当前选中项时也不会触发 change, 状态本就一致。
    """
    sel.select_by_value(value)


def wait_query_done(browser, appear_timeout: float = 8.0,
                    gone_timeout: float = 180.0) -> None:
    """等待一次课程查询完成: 等"加载中"提示出现, 再等它消失。

    必须先等提示出现再等消失: 全量查询很慢, 若直接轮询"提示不存在",
    可能在查询发起前就误判为已完成, 读到旧表格。
    若 appear_timeout 内提示从未出现, 视为查询即时完成(如缓存命中)。
    """
    deadline = time.time() + appear_timeout
    seen = bool(browser.find_elements(By.XPATH, LOADING_XPATH))
    while not seen and time.time() < deadline:
        seen = bool(browser.find_elements(By.XPATH, LOADING_XPATH))
        time.sleep(0.15)
    if seen:
        deadline = time.time() + gone_timeout
        while time.time() < deadline:
            if not browser.find_elements(By.XPATH, LOADING_XPATH):
                break
            time.sleep(0.4)
        else:
            raise TimeoutError("课程列表加载超时(加载中提示未消失)")
    time.sleep(0.5)  # 渲染稳定


def wait_quiet(browser, quiet: float = 1.5, timeout: float = 60.0) -> None:
    """等待 frmReport 连续 quiet 秒无"加载中"提示(之前的查询全部结束)。

    用于勾选"全部开课范围"之前: 确认选择专业触发的查询已完成,
    避免新查询与旧查询的加载提示混淆导致读到旧表格。
    """
    deadline = time.time() + timeout
    last_seen = 0.0
    while time.time() < deadline:
        if browser.find_elements(By.XPATH, LOADING_XPATH):
            last_seen = time.time()
        elif time.time() - last_seen >= quiet and last_seen > 0:
            return
        elif last_seen == 0.0 and time.time() > deadline - timeout + quiet:
            return  # 从头到尾都没出现过提示, 视为安静
        time.sleep(0.2)


def _elem_int(browser, xpath: str) -> int:
    """读取 totalPage/currentPage 的数字; 兼容 input(value) 与文本节点。"""
    el = find_strict(browser, xpath)
    raw = (el.get_attribute("value") or el.text or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 1


def _dialog_candidates(dialog_rows, class_nums: list[str]) -> list[tuple[list, int]]:
    """Return matching class rows and their advertised free-seat counts."""
    candidates = []
    for row in dialog_rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if not cells or len(cells) <= 11:
            continue
        class_num = (cells[0].text or "").strip()
        if class_nums and class_num not in class_nums:
            continue
        raw_free = (cells[6].text or "").strip()
        free = int(raw_free) if raw_free.isdigit() else 0
        candidates.append((cells, free))
    return candidates


def _normalize_header(text: str) -> str:
    return "".join((text or "").split())


def _course_record(texts: list[str], indexes: dict[str, int]) -> dict | None:
    """Read one table row into the compact intermediate course record."""
    def value(field: str) -> str:
        index = indexes[field]
        return texts[index].strip() if index < len(texts) else ""

    raw_name = value("name")
    weeks = value("weeks")
    time_text = value("time")
    schedule = {
        "teacher": value("teacher"),
        "time": " ".join(part for part in (weeks, time_text) if part),
        "location": value("location"),
    }
    has_schedule = any(schedule.values())
    if not raw_name:
        return {"orphan": True, "schedule": [schedule]} if has_schedule else None

    match = CODE_PREFIX_RE.match(raw_name) or CODE_RE.search(raw_name)
    if not match:
        return None
    code = (match.group(1) if match.re is CODE_PREFIX_RE else match.group(0)).upper()
    name = CODE_PREFIX_RE.sub("", raw_name, count=1).strip()
    return {
        "code": code,
        "name": name or raw_name,
        "credits": value("credits"),
        "category": value("category"),
        "class_num": value("class_num"),
        "available": value("available"),
        "limit": value("limit"),
        "schedule": [schedule] if has_schedule else [],
    }


def read_course_table(browser) -> tuple[list[dict], int, int]:
    """读取 frmReport 当前页课程表格, 合并子课程行。

    站点同时输出隐藏列和空管理字段, 不能按固定列尾猜测字段。这里按表头
    定位课程名、学分、类别、班号、余量和排课字段, 并保留班级下的每条
    并列子课程。

    Returns:
        (courses, current_page, total_page)
        courses: [{"code", "name", "credits", "category", "class_num",
                  "available", "limit", "schedule": [...]}]
    """
    table = find_strict(browser, KEYWORDS_XPATH)
    rows = table.find_elements(By.TAG_NAME, "tr")
    if not rows:
        raise RuntimeError("课程表为空")

    header_cells = rows[0].find_elements(By.TAG_NAME, "td")
    header_indexes: dict[str, int] = {}
    for index, cell in enumerate(header_cells):
        label = _normalize_header(cell.text)
        if not label or not cell.is_displayed():
            continue
        for header, field in COURSE_HEADERS.items():
            if label == _normalize_header(header):
                header_indexes[field] = index
                break
    if len(header_indexes) != len(COURSE_HEADERS):
        raise RuntimeError("课程表表头结构异常")

    courses: list[dict] = []
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) < len(header_cells):  # 表头或异常短行
            continue
        texts = [c.text.strip() for c in cells]
        record = _course_record(texts, header_indexes)
        if record:
            courses.append(record)
    return courses, _elem_int(browser, CURRENT_PAGE_XPATH), _elem_int(browser, TOTAL_PAGE_XPATH)


def group_course_records(records: list[dict]) -> dict[str, list]:
    """Group rows by course code, then by class, preserving parallel schedules.

    Compact cache format:
        code: [name, credits, category, [
            [class_num, "available/limit", [
                [teacher, "weeks time", location], ...
            ]], ...
        ]]
    """
    courses: dict[str, list] = {}
    last_class: list | None = None

    def append_schedules(class_row: list, schedule_rows: list[dict]) -> None:
        for schedule in schedule_rows:
            compact = [
                str(schedule.get("teacher") or "").strip(),
                str(schedule.get("time") or "").strip(),
                str(schedule.get("location") or "").strip(),
            ]
            if any(compact):
                class_row[2].append(compact)

    for record in records:
        schedule_rows = record.get("schedule") or []
        if record.get("orphan"):
            if last_class is not None:
                append_schedules(last_class, schedule_rows)
            continue
        code = str(record.get("code") or "").strip().upper()
        if not code:
            continue
        course = courses.get(code)
        if course is None:
            course = [
                str(record.get("name") or code).strip(),
                str(record.get("credits") or "").strip(),
                str(record.get("category") or "").strip(),
                [],
            ]
            courses[code] = course
        class_num = str(record.get("class_num") or "").strip()
        class_row = next(
            (item for item in course[3] if item[0] == class_num), None)
        if class_row is None:
            available = str(record.get("available") or "").strip()
            limit = str(record.get("limit") or "").strip()
            class_row = [class_num, f"{available}/{limit}", []]
            course[3].append(class_row)
        append_schedules(class_row, schedule_rows)
        last_class = class_row
    return courses


# ---------------- 抓取任务: 专业目录 ----------------

def run_catalog(ctx) -> None:
    """枚举 培养层次×年级×院(系)/部 的全部组合, 记录每个组合下的专业列表。

    站点特性(实测):
    - 培养层次/年级/院(系)/部 三个下拉相互独立: 改上级不会刷新下级,
      因此三者各读一次即可得到全集, 无需逐级联动等待;
    - "专业"下拉依赖前三项的组合, 选定后刷新极快(约 0.2s, 见 MAJOR_SLEEP);
    - 学校把所有单位都设为开课单位(如"夜大"下"武装部"), 大量组合下专业列表
      为空(只剩占位项), 属正常现象, 照常记录空列表。

    产出 majors.json(与 app/core/course_cache.CATALOG_FILE 同路径), 结构:
    {"updated_at", "partial", "levels": [
        {"v","t","grades": [{"v","t","depts": [{"v","t","majors": [{"v","t"}]}]}]}]}
    节点数 = 培养层次 × 年级 × 院系数 = 组合数, 无冗余。
    全部专业全部课程太多, 这里只抓"专业"本身; 课程列表等用户选定专业后再抓。

    断点续抓: 启动时若本地已有未完成(partial=true)的目录, 自动跳过其中已抓取
    的组合(含专业列表为空的组合——那是正常抓取结果), 只抓剩余部分并合并缓存。

    自动重启: 网络抖动/页面会话失效/stale 残留等瞬时异常不会终止任务——丢弃
    当前浏览器会话、重新登录并从 majors.json 检查点续抓, 最多 CATALOG_MAX_RESTARTS
    次; 超过后向上抛出由用户手动重试(断点续抓保证已抓组合不丢)。
    """
    browser = None
    try:
        restarts = 0
        while True:
            try:
                if browser is None:
                    browser = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
                    ctx.status("正在登录并打开课程安排明细…")
                    login(ctx, browser, desk_xpath=DESK_MENU_XPATH)
                _enumerate_catalog(ctx, browser)
                return
            except Exception as e:
                # 会话可能已损坏(超时/断连/页面失效), 丢弃重建;
                # 已抓组合靠检查点落盘保留, 重启后自动从 majors.json 续抓
                if browser is not None:
                    try:
                        browser.quit()
                    except Exception:
                        pass
                    browser = None
                restarts += 1
                if restarts > CATALOG_MAX_RESTARTS:
                    raise
                wait = min(30, 5 * restarts)
                ctx.log(f"抓取异常: {e!r}")
                ctx.status(f"抓取中断, {wait} 秒后自动重启续抓"
                           f"(第 {restarts}/{CATALOG_MAX_RESTARTS} 次)")
                time.sleep(wait)
    finally:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass


def _enumerate_catalog(ctx, browser) -> None:
    """单轮目录枚举(run_catalog 自动重启时会被多次调用): 每次从 majors.json
    检查点续抓, 完成时写入完整(partial=false)目录。"""
    level_sel, grade_sel, dept_sel, major_sel = _specialty_selects(browser)

    # 三个独立下拉各读一次全集
    levels = _read_options(level_sel)
    grades = _read_options(grade_sel)
    depts = _read_options(dept_sel)
    total = max(1, len(levels) * len(grades) * len(depts))

    # 断点续抓: 已有未完成(partial=true)目录时, 跳过其中已抓取的组合
    # (专业列表为空的组合也是有效抓取结果, 同样跳过), 只抓剩余部分
    cached: dict = {}
    done_before = 0
    prev = _load_partial_catalog()
    if prev:
        for l in prev["levels"]:
            lv_cache = cached.setdefault(str(l.get("v")), {})
            for g in l.get("grades") or []:
                gr_cache = lv_cache.setdefault(str(g.get("v")), {})
                for d in g.get("depts") or []:
                    if isinstance(d, dict) and "majors" in d:
                        gr_cache[str(d.get("v"))] = d.get("majors") or []
                        done_before += 1
    ctx.progress(stage="catalog", done=0, total=total)
    ctx.log(f"开始抓取专业目录: 培养层次 {len(levels)} × 年级 {len(grades)} "
            f"× 院系 {len(depts)} = {total} 组合")
    if done_before:
        ctx.log(f"检测到未完成的目录缓存: 已抓 {done_before}/{total} 组合, "
                f"自动跳过已抓组合续抓")

    tree: list[dict] = []
    done = 0
    fetched = 0               # 本次会话新抓的组合数(估算剩余时间用)
    new_since_save = 0        # 距上次检查点新抓的组合数(纯缓存回放不落盘)
    last_saved = 0
    last_dept: str | None = None   # 院系下拉当前选中值(强制触发 change 用)
    enum_t0 = time.time()   # 预计剩余时间从组合枚举起算(不含登录阶段)
    for lv in levels:
        lv_cache = cached.get(lv["v"], {})
        # 整个培养层次都已缓存时无需选中, 不触发任何站点查询
        if any(dp["v"] not in lv_cache.get(gr["v"], {})
               for gr in grades for dp in depts):
            _select(level_sel, lv["v"])
        # 先挂载再填充: 任何时刻落盘, tree 快照都包含进行中的年级/院系节点
        level_node = {**lv, "grades": []}
        tree.append(level_node)
        for gr in grades:
            gr_cache = lv_cache.get(gr["v"], {})
            if any(dp["v"] not in gr_cache for dp in depts):
                _select(grade_sel, gr["v"])
            grade_node = {**gr, "depts": []}
            level_node["grades"].append(grade_node)
            for dp in depts:
                if dp["v"] in gr_cache:
                    majors = gr_cache[dp["v"]]    # 缓存命中, 不访问站点
                else:
                    # 目标值与当前选中相同时 select_by_value 不触发 change,
                    # 先选回空占位项再选目标值, 强制专业列表按新组合刷新
                    if dp["v"] == last_dept:
                        _select(dept_sel, "")
                        time.sleep(0.1)
                    _select(dept_sel, dp["v"])
                    last_dept = dp["v"]
                    time.sleep(MAJOR_SLEEP)       # 专业列表刷新极快
                    majors = _read_options(major_sel)
                    fetched += 1
                    new_since_save += 1
                grade_node["depts"].append({**dp, "majors": majors})
                done += 1
                # 预计剩余时间: 按本次会话的组合平均耗时线性估算
                remaining = total - done
                eta = ((time.time() - enum_t0) / fetched * remaining
                       if fetched and remaining > 0 else 0)
                ctx.progress(stage="catalog", done=done, total=total,
                             eta_seconds=round(eta))
                # 每 CHECKPOINT_EVERY 个组合落盘一次: 网络中断时已抓部分
                # 保留在 majors.json(partial=true), 可直接用于选择专业
                if done - last_saved >= CHECKPOINT_EVERY:
                    if new_since_save:
                        _save_json_atomic(CATALOG_FILE, {
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "partial": True, "levels": tree,
                        })
                        ctx.log(f"已保存检查点: {done}/{total} 组合")
                    last_saved = done
                    new_since_save = 0
        ctx.status(f"{lv['t']} 完成, 累计 {done}/{total} 组合")

    _save_json_atomic(CATALOG_FILE, {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "partial": False, "levels": tree,
    })
    majors_total = sum(
        len(d["majors"])
        for l in tree for g in l["grades"] for d in g["depts"])
    ctx.status(f"专业目录抓取完成: {majors_total} 条专业记录", done=True)


# ---------------- 抓取任务: 指定专业课程列表 ----------------

def _norm_specialty(sp: dict) -> dict:
    """专业配置归一化: 兼容 "值" 与 {"v": 值, "t": 文本} 两种形态。

    配置界面(ui.html)保存的是 {v,t} 对象以便回显; 运行时只需 value。
    返回 {"level","grade","dept","major"} 四个纯字符串值。
    """
    keys = ("level", "grade", "dept", "major")
    vals = {}
    for k in keys:
        raw = (sp or {}).get(k, "")
        if isinstance(raw, dict):
            raw = raw.get("v", "")
        vals[k] = str(raw or "").strip()
    return vals


def run_specialty_courses(ctx, specialty: dict | None = None) -> None:
    """选定专业后勾选"全部开课范围", 翻页读取该专业全部课程并按专业缓存。

    specialty 为空时读配置项(配置界面保存的 {v,t} 对象或纯值均可)。

    产出 courses/<key>.json(与 app/core/course_cache.courses_file 同路径):
    {"updated_at", "specialty": {level/grade/dept/major: {"v","t"}},
     "total_pages", "courses": read_course_table 的返回}
    """
    vals = _norm_specialty(specialty if specialty is not None else ctx.cfg("specialty"))
    if not all(vals.values()):
        ctx.log("specialty 配置不完整(需要 level/grade/dept/major), 退出")
        return

    steps = [dict(step, status="pending") for step in COURSE_TASK_STEPS]
    steps[0]["status"] = "active"
    ctx.progress(
        stage="courses", done=0, total=0, steps=steps,
        overall_percent=0,
    )
    browser = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
    try:
        ctx.status("正在登录并打开课程安排明细…")
        login(ctx, browser, desk_xpath=DESK_MENU_XPATH)
        steps[0]["status"] = "done"
        steps[1]["status"] = "active"
        ctx.progress(
            stage="courses", done=0, total=0, steps=steps,
            overall_percent=5,
        )
        level_sel, grade_sel, dept_sel, major_sel = _specialty_selects(browser)
        # 三个独立下拉直接选中(无联动); 专业下拉依赖前三项组合, 等 0.2s 再选
        _select(level_sel, vals["level"])
        _select(grade_sel, vals["grade"])
        _select(dept_sel, vals["dept"])
        time.sleep(MAJOR_SLEEP)
        _select(major_sel, vals["major"])

        # 记录专业的显示文本。翻页时浏览器上下文会进入 frmReport，不能再用
        # frmDesk 中的 Select 对象读取当前值。
        def _sel_text(sel: Select) -> dict:
            o = sel.first_selected_option
            return {"v": (o.get_attribute("value") or "").strip(),
                    "t": (o.text or "").strip()}

        specialty_text = {
            "level": _sel_text(level_sel),
            "grade": _sel_text(grade_sel),
            "dept": _sel_text(dept_sel),
            "major": _sel_text(major_sel),
        }

        # 等选专业触发的查询全部结束: "加载中"提示在 frmReport 内,
        # 需先进入该帧观察, 再回 frmDesk 勾选"全部开课范围"触发全量查询
        browser.switch_to.frame(find_strict(browser, REPORT_FRAME_XPATH))
        wait_quiet(browser)
        browser.switch_to.parent_frame()
        chk = find_strict(browser, ALL_RANGE_XPATH)
        try:
            if not chk.is_selected():
                chk.click()
        except Exception:
            browser.execute_script("arguments[0].click();", chk)
        ctx.status("正在检索所选专业的课程…")
        query_button = find_strict(browser, '//*[@id="btnQry"]')
        try:
            query_button.click()
        except WebDriverException:
            browser.execute_script("arguments[0].click();", query_button)

        browser.switch_to.frame(find_strict(browser, REPORT_FRAME_XPATH))
        wait_query_done(browser, appear_timeout=3.0)
        find_strict(browser, TOTAL_PAGE_XPATH)
        steps[1]["status"] = "done"
        steps[2]["status"] = "active"
        ctx.progress(
            stage="courses", done=1, total=1, steps=steps,
            overall_percent=15,
        )

        records: list[dict] = []
        total_pages = 1
        while True:
            page_courses, cur, total_pages = read_course_table(browser)
            records.extend(page_courses)
            ctx.progress(
                stage="courses", done=cur, total=total_pages, steps=steps,
                overall_percent=round(
                    15 + 80 * (cur / max(1, total_pages)), 1),
            )
            ctx.status(f"已读取第 {cur}/{total_pages} 页, 累计 {len(records)} 条课程记录")
            if cur >= total_pages:
                break
            find_strict(browser, NEXT_XPATH).click()
            wait_query_done(browser)

        courses = group_course_records(records)
        if not courses:
            raise RuntimeError("检索完成但未读取到任何课程")

        data = {
            "schema_version": 3,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "specialty": specialty_text,
            "total_pages": total_pages,
            "courses": courses,
        }
        out = (Path(ctx.datadir) / "courses"
               / f"{course_key(vals['level'], vals['grade'], vals['dept'], vals['major'])}.json")
        steps[2]["status"] = "done"
        steps[3]["status"] = "active"
        ctx.progress(
            stage="courses", done=total_pages, total=total_pages,
            steps=steps, overall_percent=95,
        )
        _save_json_atomic(out, data)
        steps[3]["status"] = "done"
        ctx.progress(
            stage="finish", done=total_pages, total=total_pages,
            steps=steps, overall_percent=100, eta_s=0,
        )
        ctx.status(f"专业课程抓取完成: {len(courses)} 门课程, 已缓存", done=True)
    finally:
        browser.quit()


# ---------------- 轮询抢课流程 ----------------

def try_grab_course(
        ctx, browser, code: str, class_nums: list[str],
        attempt: int) -> str | None:
    """Try one course group and return the submitted class number.

    class_nums 为空列表表示不限班级: 弹窗中第一个有名额的班级直接提交。
    """
    goto_course_frame(browser)
    rows = find_strict(browser, KEYWORDS_XPATH).find_elements(By.TAG_NAME, "tr")
    for row in rows:
        cells = row.find_elements(By.TAG_NAME, "td")
        if len(cells) <= 13 or code not in cells[1].text:
            continue
        cells[13].click()
        sleepran(1)
        # 进入班级选择弹窗
        browser.switch_to.parent_frame()
        browser.switch_to.frame(find_strict(browser, '//*[@id="dialog-frame"]'))
        browser.switch_to.frame(find_strict(browser, REPORT_FRAME_XPATH))
        dialog_rows = find_strict(browser, KEYWORDS_XPATH).find_elements(By.TAG_NAME, "tr")
        candidates = _dialog_candidates(dialog_rows, class_nums)
        available = next((cells for cells, free in candidates if free > 0), None)
        if available is not None:
            d_cells = available
            selected_class_num = (d_cells[0].text or "").strip()
            d_cells[11].click()
            sleepran(1)
            browser.switch_to.parent_frame()
            find_strict(browser, '//*[@id="btnSubmit"]').click()
            ctx.status(
                f"已提交选课: {code} 班级 {selected_class_num}",
                course=code, class_num=selected_class_num,
            )
            time.sleep(2)
            return selected_class_num
        # 所有候选班级都无名额: 关闭弹窗, 等下一轮继续检查
        browser.switch_to.parent_frame()
        find_strict(browser, '//*[@id="btnReturn"]').click()
        if candidates:
            ctx.status(f"{code} 候选班级均无名额", course=code, free=0)
        else:
            ctx.status(f"{code} 候选班级均未找到", course=code)
        return None
    ctx.status(f"{code} 未在列表中找到", course=code)
    return None


def _compatible_class_nums(item: dict, occupied: list[list]) -> list[str]:
    """Return candidate classes that do not overlap already-selected courses."""
    schedules = item.get("schedules") or {}
    class_nums = list(item.get("class_nums") or [])
    if not schedules:
        return class_nums
    if not class_nums:
        class_nums = list(schedules)
    compatible = []
    for class_num in class_nums:
        segments = schedules.get(class_num, [])
        if all(
            not _segments_overlap(segments, selected)
            for selected in occupied
        ):
            compatible.append(class_num)
    return compatible


def run_polling(ctx, plan: list[dict], poll_interval: float) -> None:
    m = 0   # 连续异常次数
    n = 1   # 轮询轮次
    browser = None
    pending = list(plan)
    occupied: list[list] = []
    while pending:
        try:
            if browser is None:
                browser = make_driver(ctx, headless=bool(ctx.cfg("headless", True)))
                login(ctx, browser)
            index = 0
            while index < len(pending):
                item = pending[index]
                code = item["code"]
                class_nums = _compatible_class_nums(item, occupied)
                if item.get("schedules") and not class_nums:
                    pending.pop(index)
                    ctx.status(
                        f"{code} 已跳过: 候选班级均与已选课程冲突",
                        skipped=[code],
                    )
                    continue
                selected_class = try_grab_course(
                    ctx, browser, code, class_nums, attempt=n)
                if not selected_class:
                    index += 1
                    continue
                pending.pop(index)
                selected_segments = (
                    item.get("schedules", {}).get(selected_class, []))
                if selected_segments:
                    occupied.append(selected_segments)
                skipped: list[str] = []
                kept = []
                for candidate in pending:
                    if (
                        candidate.get("schedules")
                        and not _compatible_class_nums(candidate, occupied)
                    ):
                        skipped.append(candidate["code"])
                    else:
                        kept.append(candidate)
                pending = kept
                ctx.status(
                    f"{code} 已选择班级 {selected_class},"
                    f"剩余 {len(pending)} 门课程",
                    done=not pending,
                    skipped=skipped,
                )
                if skipped:
                    ctx.log(
                        f"{code} 已成功选课,自动跳过时间冲突课程: "
                        + ", ".join(skipped))
            if not pending:
                ctx.status("抢课流程完成", done=True)
                break
            n += 1
            ctx.progress(round=n, fatal=m, polled=n)
        except Exception as e:
            m += 1
            ctx.log(f"第 {n} 轮异常: {e!r}")
            ctx.progress(round=n, fatal=m)
            try:
                if browser:
                    browser.quit()
            except Exception:
                pass
            browser = None
            time.sleep(min(30, 2 ** m))
        time.sleep(poll_interval * (random.random() / 2 + 1))


def _normalize_courses(raw) -> dict[str, list[str]]:
    """Normalize string and list class selections into string lists."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for raw_code, raw_classes in raw.items():
        code = str(raw_code or "").strip().upper()
        if not code:
            continue
        if isinstance(raw_classes, str):
            values = re.split(r"[,，\s]+", raw_classes)
        elif isinstance(raw_classes, (list, tuple)):
            values = [str(v) for v in raw_classes]
        else:
            values = []
        out[code] = [v.strip() for v in values if v.strip()]
    return out


def _normalize_course_plan(raw_order, raw_courses) -> list[dict]:
    """Build an ordered plan with one group of alternative classes per course.

    Selecting several classes of the same course means "choose any one of
    these classes". Different course codes remain separate pending groups and
    each must be submitted successfully.
    """
    courses = _normalize_courses(raw_courses)
    plan: list[dict] = []
    by_code: dict[str, dict] = {}

    def add(code: str, class_num: str = "", class_nums=None) -> None:
        code = str(code or "").strip().upper()
        if not code:
            return
        if code not in by_code:
            entry = {"code": code, "class_nums": []}
            by_code[code] = entry
            plan.append(entry)
        values = []
        if class_num:
            values.append(class_num)
        if isinstance(class_nums, (list, tuple)):
            values.extend(class_nums)
        for value in values:
            value = str(value or "").strip()
            if value and value not in by_code[code]["class_nums"]:
                by_code[code]["class_nums"].append(value)

    if isinstance(raw_order, list):
        for item in raw_order:
            if not isinstance(item, dict):
                continue
            add(
                item.get("code"),
                item.get("class_num"),
                item.get("class_nums"),
            )
    for code, class_nums in courses.items():
        if class_nums:
            for class_num in class_nums:
                add(code, class_num)
        elif code not in by_code:
            add(code, "")
    return plan


_WEEKDAY_VALUES = {
    "一": 0, "二": 1, "三": 2, "四": 3,
    "五": 4, "六": 5, "日": 6, "天": 6,
}


def _schedule_weeks(text: str) -> set[int]:
    """Read week ranges from a schedule string such as ``1-7,9-16 周三``."""
    first_day = re.search(r"周[一二三四五六日天]", text)
    week_text = text[:first_day.start()] if first_day else text
    weeks: set[int] = set()
    parity_hint = next((char for char in week_text if char in "单双"), "")
    for match in re.finditer(
            r"(\d+)(?:\s*-\s*(\d+))?\s*([单双])?", week_text):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        parity = match.group(3) or parity_hint
        for week in range(start, min(30, end) + 1):
            if parity == "单" and week % 2 == 0:
                continue
            if parity == "双" and week % 2 == 1:
                continue
            weeks.add(week)
    return weeks or set(range(1, 31))


def _schedule_segments(schedule: list) -> list[tuple[int, int, set[int]]]:
    """Expand cached meeting times into day/period/week overlap units."""
    if not isinstance(schedule, (list, tuple)) or len(schedule) < 2:
        return []
    text = str(schedule[1] or "")
    weeks = _schedule_weeks(text)
    segments: list[tuple[int, int, set[int]]] = []
    pattern = re.compile(
        r"周([一二三四五六日天])([^周]*?)(\d+)(?:\s*-\s*(\d+))?\s*节")
    for match in pattern.finditer(text):
        day = _WEEKDAY_VALUES[match.group(1)]
        start = int(match.group(3))
        end = int(match.group(4) or start)
        for period in range(start, end + 1):
            if 1 <= period <= 12:
                segments.append((day, period, weeks))
    return segments


def _segments_overlap(left: list, right: list) -> bool:
    return any(
        a_day == b_day and a_period == b_period and bool(a_weeks & b_weeks)
        for a_day, a_period, a_weeks in left
        for b_day, b_period, b_weeks in right
    )


def _annotate_plan_conflicts(ctx, plan: list[dict]) -> list[str]:
    """Attach class schedules to the plan and describe current conflicts.

    Conflicts are advisory: the ordered polling plan decides which course gets
    the first chance, and successful courses suppress incompatible pending
    courses at runtime.
    """
    specialty = ctx.cfg("specialty") or {}

    def specialty_value(value) -> str:
        if isinstance(value, dict):
            return str(value.get("v") or "").strip()
        return str(value or "").strip()

    values = {
        key: specialty_value(specialty.get(key))
        for key in ("level", "grade", "dept", "major")
    }
    if not all(values.values()):
        ctx.log("专业配置不完整，本次运行不预计算课程时间冲突")
        return []
    cache_path = (
        Path(ctx.datadir) / "courses"
        / f"{course_key(**values)}.json"
    )
    if not cache_path.is_file():
        ctx.log("课程列表尚未缓存，本次运行不预计算课程时间冲突")
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        ctx.log(f"课程列表缓存读取失败，本次运行不预计算冲突: {exc!r}")
        return []
    courses = data.get("courses") if isinstance(data, dict) else None
    if not isinstance(courses, dict):
        ctx.log("课程列表缓存格式无效，本次运行不预计算冲突")
        return []

    selected: list[dict] = []
    for item in plan:
        code = item["code"]
        item["schedules"] = {}
        course = courses.get(code)
        if not isinstance(course, list) or len(course) < 4:
            ctx.log(f"课程 {code} 不在缓存中，本次运行不预计算该课程冲突")
            continue
        class_nums = list(item.get("class_nums") or [])
        if not class_nums:
            class_nums = [
                str(row[0] or "").strip()
                for row in course[3] if str(row[0] or "").strip()
            ]
        alternatives: list[dict] = []
        for class_num in class_nums:
            class_row = next(
                (row for row in course[3]
                 if str(row[0] or "").strip() == class_num),
                None,
            )
            if class_row is None:
                ctx.log(
                    f"课程 {code} 班级 {class_num} 不在缓存中，"
                    "本次运行不预计算该班级冲突")
                continue
            schedules = class_row[2] if len(class_row) > 2 else []
            segments = [
                segment
                for schedule in schedules
                for segment in _schedule_segments(schedule)
            ]
            alternatives.append({
                "class_num": class_num,
                "segments": segments,
            })
        if not alternatives:
            ctx.log(f"课程 {code} 没有可用排课信息，本次运行不预计算冲突")
            continue
        item["schedules"] = {
            alternative["class_num"]: alternative["segments"]
            for alternative in alternatives
        }
        selected.append({
            "code": code,
            "name": str(course[0] or code),
            "alternatives": alternatives,
        })

    def feasible(index: int, chosen: list[list]) -> bool:
        if index >= len(selected):
            return True
        for alternative in selected[index]["alternatives"]:
            if all(
                not _segments_overlap(alternative["segments"], previous)
                for previous in chosen
            ):
                chosen.append(alternative["segments"])
                if feasible(index + 1, chosen):
                    return True
                chosen.pop()
        return False

    if feasible(0, []):
        return []
    impossible: list[str] = []
    for index, left in enumerate(selected):
        for right in selected[index + 1:]:
            has_compatible_pair = any(
                not _segments_overlap(a["segments"], b["segments"])
                for a in left["alternatives"]
                for b in right["alternatives"]
            )
            if not has_compatible_pair:
                impossible.append(f"{left['name']} 与 {right['name']}")
    return impossible or [
        "、".join(course["name"] for course in selected)
        + " 无法组成互不冲突的课程组合"
    ]


def main() -> None:
    ctx = PluginContext()

    # 抓取任务(主程序 API 注入 task): 与正常抢课共用进程槽位, 天然互斥
    task = str(ctx.cfg("task") or "").strip()
    if task == "catalog":
        run_catalog(ctx)
        return
    if task == "courses":
        run_specialty_courses(ctx)
        return

    try:
        interval = float(ctx.cfg("poll_interval", 3))
    except (TypeError, ValueError):
        interval = 3.0
    interval = min(60.0, max(2.0, interval))

    plan = _normalize_course_plan(
        ctx.cfg("course_order"), ctx.cfg("courses"))
    if not plan:
        ctx.log("未配置任何课程,退出(请先在配置界面选择课程)")
        return
    conflicts = _annotate_plan_conflicts(ctx, plan)
    if conflicts:
        ctx.log(
            "检测到时间冲突,按已选顺序运行;成功选课后将跳过冲突课程: "
            + "; ".join(conflicts[:5]))
    ordered = " -> ".join(
        f"{item['code']}[{','.join(item['class_nums']) or '不限'}]"
        for item in plan)
    ctx.log(f"持续轮询课程顺序={ordered}, 间隔={interval}s")
    run_polling(ctx, plan, interval)


if __name__ == "__main__":
    main()
