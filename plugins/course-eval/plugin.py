"""自动评教插件：登录北师大评教系统并完成期末学生评教。

流程：
1. 打开评教首页，必要时通过统一身份认证登录；
2. 进入“期末评教”课程列表，记录全部待评课程的评教页面地址和当前账号 Cookie；
3. 逐门打开已记录的评教地址，对问卷中的全部量表题选择“完全符合”；
4. 提交后最多等待 60 秒确认结果，失败时最多重试 3 次；
5. 回到列表确认课程已不再处于待评教状态。

运行界面的任务流程只展示课程节点，每个节点对应一门课程。
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from _sdk.sdk import PluginContext, find, find_strict, make_driver  # noqa: E402
from selenium.common.exceptions import (  # noqa: E402
    NoAlertPresentException,
    NoSuchWindowException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

PJXT_HOME = "https://ss.bnu.edu.cn/www/dd/vue/spa/pjxt#/"
LIST_URL = "https://ss.bnu.edu.cn/www/dd/vue/spa/pjxt#/xspj?type=qmpj"
CARD_SELECTOR = ".pjxt-com-xspjList"
SUBMIT_CONFIRM_TIMEOUT = 60.0
MAX_RETRIES = 3
DEFAULT_WORKERS = 5
MAX_WORKERS = 10
SUCCESS_MARKERS = (
    "提交成功",
    "保存成功",
    "评教成功",
    "操作成功",
    "已提交",
    "已完成",
    "评教完成",
)
ERROR_MARKERS = (
    "用户名或密码错误",
    "账号或密码错误",
    "密码错误",
    "用户不存在",
)
ALREADY_EVALUATED_MARKERS = (
    "该课程已评教",
    "已评教",
    "不能重复评教",
    "请勿重复提交",
    "评教已结束",
    "评教已完成",
)
VALIDATION_MARKERS = ("不能为空", "请选择", "请输入", "必填项")
_CONTEXT: dict[str, object] = {}


class KnownError(RuntimeError):
    """可以直接展示给用户的常见错误，不上报 GlitchTip。"""


class StructuralError(RuntimeError):
    """页面结构变化或缺少关键元素，属于需要上报的未知错误。"""


def _set_context(**fields) -> None:
    _CONTEXT.update({key: value for key, value in fields.items() if value})


def _is_expected_error(exc: BaseException) -> bool:
    try:
        from app.core.error_policy import classify_user_error

        return bool(classify_user_error(str(exc), type(exc).__name__))
    except Exception:
        return False


def _safe_path(filename: str) -> str:
    return re.sub(
        r"(?i)^[A-Z]:\\Users\\[^\\]+", r"C:\\Users\\<user>", str(filename)
    )


def _report_unknown(ctx: PluginContext, exc: BaseException) -> None:
    """把未归类的异常上报到 GlitchTip，避免把账号/密码/Cookie 写进去。"""
    try:
        from app.core.glitchtip import send_event
    except Exception:
        ctx.log("GlitchTip 模块不可用，未知错误未上报")
        return

    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        frames.append({
            "filename": _safe_path(frame.filename),
            "lineno": frame.lineno,
            "function": frame.name,
            "context_line": frame.line or "",
        })
    context = "; ".join(
        f"{key}={value}" for key, value in _CONTEXT.items()
        if value not in (None, "", 0)
    )
    try:
        uploaded = send_event(
            f"{type(exc).__name__}: {exc}",
            context=context,
            plugin="course-eval",
            source="plugin",
            action_description="自动评教运行失败",
            plugin_info={"id": "course-eval", "version": "1.3.0"},
            python_traceback={
                "type": type(exc).__name__,
                "value": str(exc),
                "stacktrace": {"frames": frames},
            },
        )
        ctx.log(
            "未知错误已上报 GlitchTip"
            if uploaded else
            "未知错误已记录，但 GlitchTip 上报未成功"
        )
    except Exception as report_exc:
        ctx.log(f"GlitchTip 上报失败：{type(report_exc).__name__}")


def _progress(ctx: PluginContext, stage: str, *, steps: list,
              done: int = 0, total: int = 0,
              title: str = "", percent: int = 0,
              workers: dict | None = None) -> None:
    payload = {
        "stage": stage,
        "done": done,
        "total": total,
        "title": title,
        "overall_percent": max(0, min(100, int(percent))),
        "steps": steps,
    }
    if workers is not None:
        payload["workers"] = workers
    ctx.progress(**payload)


def _body_text(driver) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except WebDriverException:
        return ""


def _open(ctx: PluginContext, driver, url: str) -> None:
    try:
        driver.get(url)
    except TimeoutException:
        ctx.log(f"页面资源加载较慢，继续等待目标元素：{url}")


def _wait_ready(driver, timeout: float = 8) -> None:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState")
            in ("interactive", "complete")
        )
    except TimeoutException:
        pass


def _is_cas(driver) -> bool:
    return "cas.bnu.edu.cn" in driver.current_url


def _click(driver, element) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.08)
    try:
        driver.execute_script("arguments[0].click();", element)
    except UnexpectedAlertPresentException:
        raise
    except WebDriverException:
        element.click()


def _login(ctx: PluginContext, driver) -> None:
    _open(ctx, driver, PJXT_HOME)
    _wait_ready(driver)
    try:
        WebDriverWait(driver, 8).until(
            lambda d: _is_cas(d) or d.find_elements(By.ID, "app"))
    except TimeoutException:
        raise KnownError("评教系统页面加载超时，请检查校园网连接") from None

    if not _is_cas(driver):
        ctx.log("评教系统已登录")
        return

    ctx.log("正在登录北师大统一身份认证…")
    user_input = find(
        driver,
        '//input[contains(@placeholder,"学工号") '
        'or contains(@placeholder,"学号") '
        'or contains(@placeholder,"用户名")]',
        timeout=10,
    )
    if user_input is None:
        raise KnownError("统一身份认证页面未找到账号输入框")
    password_input = find(
        driver,
        '//input[@type="password" and contains(@placeholder,"密码")]',
        timeout=6,
    )
    if password_input is None:
        raise KnownError("统一身份认证页面未找到密码输入框")

    user_input.clear()
    user_input.send_keys(ctx.username)
    password_input.clear()
    password_input.send_keys(ctx.password)

    clicked = False
    for element in driver.find_elements(
            By.XPATH,
            '//a[contains(@class,"login-btn") and '
            'normalize-space(.)="登录"]'):
        try:
            if element.is_displayed():
                _click(driver, element)
                clicked = True
                break
        except (StaleElementReferenceException, WebDriverException):
            continue
    if not clicked:
        password_input.send_keys(Keys.RETURN)

    deadline = time.time() + 30
    while time.time() < deadline:
        page_text = _body_text(driver)
        for marker in ERROR_MARKERS:
            if marker in page_text:
                raise KnownError(f"统一身份认证失败：{marker}") from None
        if not _is_cas(driver) and "ss.bnu.edu.cn" in driver.current_url:
            ctx.log("统一身份认证登录成功")
            return
        time.sleep(0.25)
    raise KnownError("统一身份认证登录超时，请检查全局账号密码") from None


def _wait_cards(driver, timeout: float = 10) -> None:
    def ready(d):
        if d.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
            return True
        text = _body_text(d)
        return any(marker in text for marker in (
            "暂无数据", "暂无评教", "没有待评", "已全部评教"))

    WebDriverWait(driver, timeout).until(ready)


def _load_all_cards(driver) -> list:
    """滚动列表触发懒加载，返回当前已渲染的课程卡片。"""
    last = -1
    cards = []
    for _ in range(10):
        cards = driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)
        if len(cards) == last:
            break
        last = len(cards)
        try:
            driver.execute_script(
                "const list=document.querySelector('.van-list');"
                "if(list){list.scrollTop=list.scrollHeight;}"
                "window.scrollTo(0, document.body.scrollHeight);"
            )
        except WebDriverException:
            pass
        time.sleep(0.25)
    return driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR)


def _first_text(element, selector: str) -> str:
    try:
        found = element.find_elements(By.CSS_SELECTOR, selector)
        if found:
            return (found[0].text or "").strip()
    except (StaleElementReferenceException, WebDriverException):
        pass
    return ""


def _card_info(card) -> dict:
    title = _first_text(card, ".tit-kcmc")
    tag = _first_text(card, ".van-tag")
    fields: dict[str, str] = {}
    for row in card.find_elements(By.CSS_SELECTOR, ".in-col"):
        label = _first_text(row, ".in-label").rstrip("：:")
        value = _first_text(row, ".in-text")
        if label and value:
            fields[label] = value
    teacher = fields.get("教师姓名", "")
    class_num = fields.get("上课班号", "")
    year = fields.get("学年", "")
    term = fields.get("学期", "")
    label = " / ".join(
        part for part in (title, teacher, class_num) if part)
    short_label = " · ".join(
        part for part in (title, teacher) if part) or label
    return {
        "title": title,
        "teacher": teacher,
        "class_num": class_num,
        "year": year,
        "term": term,
        "tag": tag,
        "label": label or title or "未知课程",
        "short_label": short_label or title or "未知课程",
    }


def _entry_page(ctx: PluginContext, driver) -> None:
    """确保当前位于期末评教课程列表。"""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            try:
                current = driver.current_url
            except WebDriverException:
                current = ""
            if "#/xspj" in current:
                # 同一 hash 路由下 driver.get 可能不会真正刷新 SPA，
                # 使用 refresh 确保重新请求课程列表接口。
                try:
                    driver.refresh()
                except WebDriverException:
                    _open(ctx, driver, LIST_URL)
            else:
                _open(ctx, driver, LIST_URL)
            _wait_ready(driver)
            if _is_cas(driver):
                ctx.log("评教登录状态已失效，正在重新登录…")
                _login(ctx, driver)
                _open(ctx, driver, LIST_URL)
                _wait_ready(driver)
            if driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
                return
            entry = find(
                driver,
                '//button[contains(normalize-space(.),"期末评教")]',
                timeout=5,
            )
            if entry is not None:
                ctx.log("进入期末评教")
                _click(driver, entry)
            _wait_cards(driver, timeout=8)
            if not driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
                time.sleep(0.4)
            return
        except TimeoutException as exc:
            last_exc = exc
            if attempt < 2:
                ctx.log("课程列表加载超时，正在重试…")
                time.sleep(0.4)
    raise KnownError("评教课程列表加载失败，请检查校园网连接") from last_exc


def _pending_cards(driver) -> list[tuple[object, dict]]:
    result = []
    for card in driver.find_elements(By.CSS_SELECTOR, CARD_SELECTOR):
        try:
            info = _card_info(card)
            if "待评教" not in info["tag"]:
                continue
            if not card.find_elements(
                    By.CSS_SELECTOR, ".xspjList-botton-box"):
                continue
            result.append((card, info))
        except (StaleElementReferenceException, WebDriverException):
            continue
    return result


def _course_pending(pending: list[tuple[object, dict]], course: dict) -> bool:
    for _card, info in pending:
        if (info["title"] == course["title"]
                and info["teacher"] == course["teacher"]
                and info["class_num"] == course["class_num"]):
            return True
    return False


def _open_course(ctx: PluginContext, driver, button,
                 list_handle: str, timeout: float = 8) -> bool:
    """点击“立即评价”；返回是否打开了新标签页。"""
    before = set(driver.window_handles)
    _click(driver, button)
    deadline = time.time() + timeout
    while time.time() < deadline:
        handles = driver.window_handles
        new_handles = [handle for handle in handles if handle not in before]
        if new_handles:
            driver.switch_to.window(new_handles[-1])
            return True
        try:
            if (driver.current_window_handle == list_handle
                    and "/core/uform/" in driver.current_url):
                return False
        except NoSuchWindowException:
            break
        time.sleep(0.15)
    raise RuntimeError("点击“立即评价”后未打开评教问卷页面")


def _capture_form_url(driver, timeout: float = 8) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = driver.current_url
        except WebDriverException:
            url = ""
        if "/core/uform/" in url:
            return url
        if _is_cas(driver):
            raise KnownError("评教登录状态已失效，需要重新登录")
        time.sleep(0.1)
    raise RuntimeError("未能获取课程评教页面地址")


def _close_extra_tabs(driver, list_handle: str) -> None:
    for handle in list(driver.window_handles):
        if handle == list_handle:
            continue
        try:
            driver.switch_to.window(handle)
            driver.close()
        except WebDriverException:
            pass
    try:
        if list_handle in driver.window_handles:
            driver.switch_to.window(list_handle)
    except WebDriverException:
        pass


def _close_form(ctx: PluginContext, driver, form_handle: str,
                list_handle: str, opened_new: bool) -> None:
    if opened_new:
        try:
            if form_handle in driver.window_handles:
                driver.switch_to.window(form_handle)
                try:
                    driver.close()
                except UnexpectedAlertPresentException:
                    _handle_dialogs(driver)
                    driver.close()
                except WebDriverException:
                    pass
        finally:
            if list_handle in driver.window_handles:
                driver.switch_to.window(list_handle)
    else:
        _close_extra_tabs(driver, list_handle)
        _open(ctx, driver, LIST_URL)


def _capture_course_url(ctx: PluginContext, driver, list_handle: str,
                        index: int) -> tuple[dict, str]:
    last_exc: Exception | None = None
    info: dict = {}
    for attempt in range(2):
        pending = _pending_cards(driver)
        if index >= len(pending):
            raise RuntimeError("记录课程地址时课程列表发生变化")
        card, info = pending[index]
        buttons = card.find_elements(
            By.CSS_SELECTOR, ".xspjList-botton-box")
        if not buttons:
            raise StructuralError(
                f"课程 {info['label']} 未找到“立即评价”按钮")
        form_handle: str | None = None
        opened_new = False
        try:
            opened_new = _open_course(
                ctx, driver, buttons[0], list_handle, timeout=8)
            form_handle = driver.current_window_handle
            url = _capture_form_url(driver, timeout=8)
            return info, url
        except KnownError:
            raise
        except Exception as exc:
            last_exc = exc
            ctx.log(f"记录课程地址失败（{attempt + 1}/2）：{exc}")
        finally:
            if form_handle is not None:
                _close_form(
                    ctx, driver, form_handle, list_handle, opened_new)
            else:
                _close_extra_tabs(driver, list_handle)
        _entry_page(ctx, driver)
        _load_all_cards(driver)
    raise RuntimeError(
        f"无法记录课程 {info.get('label') or ''} 的评教页面地址"
    ) from last_exc


def _discover_courses(ctx: PluginContext, driver,
                      list_handle: str) -> list[dict]:
    total = len(_pending_cards(driver))
    if total == 0:
        return []

    courses: list[dict] = []
    index = 0
    relogins = 0
    while index < total:
        try:
            info, url = _capture_course_url(
                ctx, driver, list_handle, index)
        except KnownError as exc:
            relogins += 1
            if relogins > 2:
                raise KnownError(
                    "记录课程地址时登录状态反复失效，请重新运行插件") from exc
            ctx.log("登录状态已失效，重新登录后继续记录课程地址…")
            _login(ctx, driver)
            _entry_page(ctx, driver)
            _load_all_cards(driver)
            continue
        courses.append({
            "title": info["title"],
            "teacher": info["teacher"],
            "class_num": info["class_num"],
            "year": info["year"],
            "term": info["term"],
            "label": info["label"],
            "short_label": info["short_label"],
            "url": url,
        })
        index += 1
        ctx.log(f"已记录课程地址 {index}/{total}：{info['label']}")
        ctx.status(f"已记录 {index}/{total} 门课程地址", stage="discover")
        _progress(
            ctx, "discover",
            steps=[{
                "key": "discover",
                "label": "记录课程评教地址",
                "status": "active",
            }],
            done=index, total=total,
            title=info["label"], percent=0,
        )

    labels = Counter(course["short_label"] for course in courses)
    for course in courses:
        if labels[course["short_label"]] > 1:
            course["step_label"] = (
                f"{course['short_label']}（{course['class_num']}）")
        else:
            course["step_label"] = course["short_label"]
    return courses


def _session_path(ctx: PluginContext) -> Path:
    return Path(ctx.datadir) / "session.json"


def _account_identity(ctx: PluginContext) -> tuple[str, str]:
    account = ctx.cfg("account", {})
    if not isinstance(account, dict):
        account = {}
    account_id = str(account.get("id") or "").strip()
    username = str(ctx.username or account.get("username") or "").strip()
    return account_id, username


def _cache_account_info(ctx: PluginContext) -> dict:
    account = ctx.cfg("account", {})
    if not isinstance(account, dict):
        account = {}
    _account_id, username = _account_identity(ctx)
    return {
        "id": str(account.get("id") or ""),
        "username": username,
        "nickname": str(account.get("nickname") or username),
    }


def _empty_cache() -> dict:
    return {"version": 2, "accounts": {}}


def _read_cache(ctx: PluginContext) -> dict:
    path = _session_path(ctx)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_cache()
    except (OSError, json.JSONDecodeError) as exc:
        ctx.log(f"读取本地课程缓存失败，将重新抓取：{exc}")
        return _empty_cache()
    if not isinstance(data, dict):
        return _empty_cache()
    if isinstance(data.get("accounts"), dict):
        return data
    # 兼容旧版单账号 session.json。
    old_account = str(data.get("account") or "").strip()
    if not old_account:
        return _empty_cache()
    entry = {
        "account": {"username": old_account},
        "saved_at": str(data.get("saved_at") or ""),
        "cookies": data.get("cookies") or [],
        "courses": data.get("courses") or [],
    }
    return {"version": 2, "accounts": {old_account: entry}}


def _write_cache(ctx: PluginContext, data: dict) -> bool:
    path = _session_path(ctx)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        return True
    except (OSError, TypeError, ValueError) as exc:
        ctx.log(f"保存本地课程缓存失败：{exc}")
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _cache_entry(ctx: PluginContext, data: dict,
                 create: bool = False) -> dict | None:
    account_id, username = _account_identity(ctx)
    for key in (account_id, username):
        if not key:
            continue
        entry = data["accounts"].get(key)
        if isinstance(entry, dict):
            if account_id and key != account_id:
                # 旧缓存按用户名存储，逐步迁移到账号 id。
                data["accounts"][account_id] = data["accounts"].pop(key)
                entry = data["accounts"][account_id]
            entry["account"] = _cache_account_info(ctx)
            return entry
    if not create:
        return None
    key = account_id or username
    if not key:
        return None
    entry = {
        "account": _cache_account_info(ctx),
        "saved_at": "",
        "cookies_saved_at": "",
        "cookies": [],
        "courses": [],
    }
    data["accounts"][key] = entry
    return entry


def _course_key(course: dict) -> tuple[str, str, str]:
    return (
        str(course.get("title") or ""),
        str(course.get("teacher") or ""),
        str(course.get("class_num") or ""),
    )


def _normalize_cached_course(course: dict) -> dict | None:
    if not isinstance(course, dict):
        return None
    url = str(course.get("url") or "").strip()
    if not url:
        return None
    title = str(course.get("title") or "")
    teacher = str(course.get("teacher") or "")
    class_num = str(course.get("class_num") or "")
    label = str(course.get("label") or " / ".join(
        part for part in (title, teacher, class_num) if part))
    short_label = str(course.get("short_label") or " · ".join(
        part for part in (title, teacher) if part) or label)
    return {
        "title": title,
        "teacher": teacher,
        "class_num": class_num,
        "year": str(course.get("year") or ""),
        "term": str(course.get("term") or ""),
        "label": label or title or "未知课程",
        "short_label": short_label or title or "未知课程",
        "step_label": str(course.get("step_label") or short_label or title),
        "url": url,
    }


def _cached_courses(ctx: PluginContext) -> list[dict]:
    data = _read_cache(ctx)
    entry = _cache_entry(ctx, data, create=False)
    if entry is None:
        return []
    raw = entry.get("courses")
    if not isinstance(raw, list):
        return []
    courses = []
    for item in raw:
        normalized = _normalize_cached_course(item)
        if normalized:
            courses.append(normalized)
    if len(courses) != len(raw):
        entry["courses"] = courses
        entry["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_cache(ctx, data)
    return courses


def _update_cookies(ctx: PluginContext, driver) -> None:
    data = _read_cache(ctx)
    entry = _cache_entry(ctx, data, create=True)
    if entry is None:
        return
    try:
        entry["cookies"] = driver.get_cookies()
    except WebDriverException as exc:
        ctx.log(f"记录当前账号 Cookie 失败：{exc}")
        return
    entry["cookies_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if _write_cache(ctx, data):
        ctx.log("已记录当前账号 Cookie")


def _save_discovered(ctx: PluginContext, driver,
                     courses: list[dict]) -> None:
    data = _read_cache(ctx)
    entry = _cache_entry(ctx, data, create=True)
    if entry is None:
        raise KnownError("无法确定当前账号，课程地址缓存保存失败")
    entry["account"] = _cache_account_info(ctx)
    entry["courses"] = courses
    entry["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        entry["cookies"] = driver.get_cookies()
        entry["cookies_saved_at"] = entry["saved_at"]
    except WebDriverException:
        pass
    if _write_cache(ctx, data):
        ctx.log(
            f"已保存 {len(courses)} 门课程评教地址到本地缓存")


def _remove_cached_courses(ctx: PluginContext,
                           removed: list[dict]) -> None:
    if not removed:
        return
    keys = {_course_key(course) for course in removed}
    data = _read_cache(ctx)
    entry = _cache_entry(ctx, data, create=False)
    if entry is None:
        return
    courses = entry.get("courses")
    if not isinstance(courses, list):
        return
    before = len(courses)
    entry["courses"] = [
        course for course in courses
        if _course_key(course) not in keys
    ]
    if len(entry["courses"]) != before:
        entry["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_cache(ctx, data)
        ctx.log(
            f"已从本地缓存移除 {before - len(entry['courses'])} 门课程，"
            f"剩余 {len(entry['courses'])} 门")


def _remove_cached_course(ctx: PluginContext, course: dict) -> None:
    data = _read_cache(ctx)
    entry = _cache_entry(ctx, data, create=False)
    if entry is None:
        return
    courses = entry.get("courses")
    if not isinstance(courses, list):
        return
    key = _course_key(course)
    new_courses = []
    removed = False
    for item in courses:
        if not removed and _course_key(item) == key:
            removed = True
            continue
        new_courses.append(item)
    if not removed:
        return
    entry["courses"] = new_courses
    entry["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if _write_cache(ctx, data):
        ctx.log(
            f"已从本地缓存移除课程：{course.get('label') or key[0]}"
            f"（剩余 {len(new_courses)} 门）")


def _wait_form(driver, timeout: float = 8) -> None:
    WebDriverWait(driver, timeout).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, ".dd-elements")
        or d.find_elements(By.CSS_SELECTOR, ".dd-element")
    )


def _page_says_already_evaluated(driver) -> bool:
    text = _body_text(driver)
    return any(marker in text for marker in ALREADY_EVALUATED_MARKERS)


def _form_container(driver):
    containers = driver.find_elements(By.CSS_SELECTOR, ".dd-elements")
    if containers:
        return containers[0]
    return find_strict(driver, "/html/body/div/div/div[2]", timeout=5)


def _scale_questions(container) -> list:
    return [
        element for element in container.find_elements(
            By.XPATH, "./div[contains(@class,'dd-element')]")
        if element.find_elements(
            By.CSS_SELECTOR, ".mini-radiobuttonlist")
    ]


def _select_full_match(driver, question, index: int) -> None:
    items = question.find_elements(
        By.CSS_SELECTOR, ".mini-radiobuttonlist-item")
    target = None
    for item in items:
        try:
            if (item.text or "").strip() == "完全符合":
                labels = item.find_elements(By.TAG_NAME, "label")
                target = labels[0] if labels else item
                break
        except StaleElementReferenceException:
            continue
    if target is None:
        raise StructuralError(
            f"第 {index} 道量表题未找到“完全符合”选项")

    radio = question.find_elements(
        By.CSS_SELECTOR, "input[type='radio']")
    selected = False
    for element in radio:
        try:
            if element.is_selected():
                selected = True
                break
        except WebDriverException:
            continue
    if not selected:
        _click(driver, target)
        time.sleep(0.05)
    hidden = question.find_elements(By.CSS_SELECTOR, "input[type='hidden']")
    if not hidden or (hidden[0].get_attribute("value") or "") != "5":
        try:
            item = target.find_element(By.XPATH, "..")
            _click(driver, item)
            time.sleep(0.05)
        except WebDriverException:
            pass
    if not hidden or (hidden[0].get_attribute("value") or "") != "5":
        raise StructuralError(
            f"第 {index} 道量表题选择“完全符合”失败")


def _fill_form(driver) -> int:
    container = _form_container(driver)
    questions = _scale_questions(container)
    if not questions:
        raise StructuralError("评教问卷中未找到量表题")

    for index, question in enumerate(questions, start=1):
        _select_full_match(driver, question, index)
    time.sleep(0.1)

    missing = []
    for index, question in enumerate(questions, start=1):
        hidden = question.find_elements(By.CSS_SELECTOR, "input[type='hidden']")
        if not hidden or (hidden[0].get_attribute("value") or "") != "5":
            missing.append(index)
    if missing:
        raise StructuralError(
            "以下量表题未成功选择“完全符合”："
            + "、".join(str(index) for index in missing))
    return len(questions)


def _has_validation_error(driver) -> bool:
    try:
        for element in driver.find_elements(
                By.CSS_SELECTOR, ".mini-errorIcon"):
            if element.is_displayed():
                return True
    except WebDriverException:
        pass
    text = _body_text(driver)
    return any(marker in text for marker in VALIDATION_MARKERS)


def _handle_dialogs(driver) -> bool:
    try:
        alert = driver.switch_to.alert
        alert.accept()
        return True
    except (NoAlertPresentException, NoSuchWindowException):
        pass
    except WebDriverException:
        pass

    confirm_xpaths = (
        '//div[contains(@class,"mini-messagebox")]'
        '//a[normalize-space(.)="确定" or normalize-space(.)="是"]',
        '//div[contains(@class,"van-dialog")]'
        '//button[normalize-space(.)="确定"]',
        '//div[contains(@class,"el-message-box")]'
        '//button[normalize-space(.)="确定"]',
    )
    for xpath in confirm_xpaths:
        for element in driver.find_elements(By.XPATH, xpath):
            try:
                if element.is_displayed():
                    _click(driver, element)
                    return True
            except (StaleElementReferenceException, WebDriverException):
                continue
    return False


def _submit_form(driver) -> tuple[bool, str]:
    submit = find(
        driver,
        '/html/body/div/div/div[3]/a[contains(normalize-space(.),"提交")]',
        timeout=5,
    )
    if submit is None:
        submit = find(
            driver,
            '//a[contains(@class,"dd-formbutton-save")]',
            timeout=3,
        )
    if submit is None:
        raise StructuralError("评教问卷页面未找到提交按钮")

    try:
        _click(driver, submit)
    except UnexpectedAlertPresentException:
        _handle_dialogs(driver)

    deadline = time.time() + SUBMIT_CONFIRM_TIMEOUT
    while time.time() < deadline:
        _handle_dialogs(driver)
        try:
            if driver.current_window_handle not in driver.window_handles:
                return True, ""
        except NoSuchWindowException:
            return True, ""
        except WebDriverException:
            pass
        try:
            if "/core/uform/create/" not in driver.current_url:
                return True, ""
        except WebDriverException:
            pass
        text = _body_text(driver)
        if any(marker in text for marker in SUCCESS_MARKERS):
            return True, ""
        if _has_validation_error(driver):
            return False, "validation"
        time.sleep(0.4)
    return False, "timeout"


def _course_steps(courses: list[dict], statuses: list[str]) -> list[dict]:
    return [
        {
            "key": f"course-{index + 1}",
            "label": course.get("step_label") or course["label"],
            "status": statuses[index],
        }
        for index, course in enumerate(courses)
    ]


def _send_course_progress(ctx: PluginContext, courses: list[dict],
                          statuses: list[str], *, current: str = "",
                          done: int = 0, stage: str = "evaluate",
                          workers: dict | None = None) -> None:
    total = len(courses)
    percent = int(done / total * 100) if total else 100
    _progress(
        ctx, stage,
        steps=_course_steps(courses, statuses),
        done=done, total=total,
        title=current or (courses[done]["step_label"] if done < total else ""),
        percent=percent,
        workers=workers,
    )


def _refresh_pending(ctx: PluginContext, driver) -> list[tuple[object, dict]]:
    _entry_page(ctx, driver)
    _load_all_cards(driver)
    return _pending_cards(driver)


def _wait_for_course_done_specific(ctx: PluginContext, driver, course: dict,
                                   timeout: float = 20) -> bool:
    """并行模式下按具体课程确认完成，避免其他课程完成造成误判。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pending = _refresh_pending(ctx, driver)
        except KnownError:
            raise
        except Exception as exc:
            ctx.log(f"刷新课程列表失败，稍后重试：{exc}")
            time.sleep(0.5)
            continue
        if not _course_pending(pending, course):
            if pending:
                return True
            # 列表可能短暂显示空数据，再确认一次。
            time.sleep(0.5)
            try:
                pending = _refresh_pending(ctx, driver)
            except Exception as exc:
                ctx.log(f"刷新课程列表失败，稍后重试：{exc}")
                continue
            if not _course_pending(pending, course):
                return True
        time.sleep(0.6)
    return False


def _worker_count(ctx: PluginContext, total: int) -> int:
    try:
        requested = int(float(ctx.cfg("workers", DEFAULT_WORKERS)))
    except (TypeError, ValueError):
        requested = DEFAULT_WORKERS
    requested = max(1, min(MAX_WORKERS, requested))
    return max(1, min(requested, total)) if total else 0


def _inject_cookies(ctx: PluginContext, driver, cookies: list[dict]) -> None:
    """把主浏览器的会话 Cookie 注入并行浏览器，失败时由工作进程重新登录。"""
    if not cookies:
        return
    try:
        driver.execute_cdp_cmd("Network.enable", {})
    except WebDriverException:
        pass
    failed = 0
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        if not name or not domain:
            continue
        params = {
            "name": name,
            "value": str(cookie.get("value") or ""),
            "domain": domain,
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure", False)),
            "httpOnly": bool(cookie.get("httpOnly", False)),
        }
        expiry = cookie.get("expiry")
        if expiry is not None:
            try:
                params["expires"] = int(float(expiry))
            except (TypeError, ValueError):
                pass
        same_site = str(cookie.get("sameSite") or "")
        if same_site.lower() in ("strict", "lax", "none"):
            params["sameSite"] = same_site.capitalize()
        try:
            result = driver.execute_cdp_cmd("Network.setCookie", params)
            if isinstance(result, dict) and result.get("success") is False:
                failed += 1
        except WebDriverException:
            failed += 1
    if failed:
        ctx.log(
            f"并行浏览器 Cookie 注入失败 {failed} 项，"
            "必要时会自动重新登录")


def _open_form_direct(ctx: PluginContext, driver, url: str) -> None:
    """并行工作进程直接打开评教地址，必要时在原地重新登录。"""
    _open(ctx, driver, url)
    _wait_ready(driver)
    if _is_cas(driver):
        ctx.log("并行评教登录状态已失效，正在重新登录…")
        _login(ctx, driver)
        _open(ctx, driver, url)
        _wait_ready(driver)
    if _is_cas(driver):
        raise KnownError(
            "并行浏览器重新登录后仍被跳转到统一身份认证页面")


def _evaluate_course_worker(ctx: PluginContext, driver,
                            course: dict) -> tuple[bool, str]:
    _open_form_direct(ctx, driver, course["url"])
    _wait_form(driver)
    if _page_says_already_evaluated(driver):
        return True, "already"
    _fill_form(driver)
    return _submit_form(driver)


def _worker_snapshot(states: dict[int, dict]) -> dict:
    return {str(key): dict(value) for key, value in states.items()}


def _active_summary(states: dict[int, dict]) -> str:
    labels = [
        str(state.get("title") or "")
        for state in states.values()
        if state.get("status") == "评教中"
    ]
    labels = [label for label in labels if label]
    if not labels:
        return ""
    if len(labels) <= 3:
        return "并行评教中：" + "、".join(labels)
    return "并行评教中：" + "、".join(labels[:3]) + f" 等 {len(labels)} 门"


def _evaluate_parallel(ctx: PluginContext, driver,
                       courses: list[dict], statuses: list[str],
                       done_count: int, worker_count: int,
                       cookies: list[dict]) -> int:
    total = len(courses)
    index_by_key = {
        _course_key(course): index
        for index, course in enumerate(courses)
    }
    pending_keys = {
        key for key, index in index_by_key.items()
        if statuses[index] == "pending"
    }
    if not pending_keys or worker_count <= 0:
        return done_count
    worker_count = max(1, min(worker_count, len(pending_keys)))

    tasks: queue.Queue = queue.Queue()
    results: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    for key in pending_keys:
        tasks.put({
            "key": key,
            "course": courses[index_by_key[key]],
            "attempt": 0,
        })

    worker_states: dict[int, dict] = {
        worker_id: {"status": "启动中", "title": ""}
        for worker_id in range(1, worker_count + 1)
    }
    active_workers = set(worker_states)
    worker_current: dict[int, dict] = {}
    failures: dict[str, str] = {}
    fatal_error = ""

    def publish(current: str = "") -> None:
        active = _active_summary(worker_states)
        _send_course_progress(
            ctx, courses, statuses,
            current=current or active,
            done=done_count,
            workers=_worker_snapshot(worker_states),
        )
        if active:
            ctx.status(active, stage="evaluate")
        elif current:
            ctx.status(current, stage="evaluate")
        else:
            ctx.status(
                f"已完成 {done_count}/{total} 门课程", stage="evaluate")

    def fail_task(task: dict, error: str,
                  retryable: bool, fatal: bool = False) -> None:
        nonlocal fatal_error
        key = task["key"]
        index = index_by_key[key]
        attempt = int(task.get("attempt") or 0)
        label = task["course"].get("label") or key
        if fatal:
            fatal_error = error
            for pending_key in list(pending_keys):
                pending_index = index_by_key[pending_key]
                statuses[pending_index] = "error"
                failures[pending_key] = error
            pending_keys.clear()
            return
        if retryable and attempt < MAX_RETRIES:
            statuses[index] = "pending"
            tasks.put({
                "key": key,
                "course": task["course"],
                "attempt": attempt + 1,
            })
            ctx.log(
                f"课程“{label}”第 {attempt + 1}/{MAX_RETRIES} 次重试")
            return
        statuses[index] = "error"
        failures[key] = error
        pending_keys.discard(key)
        ctx.log(f"课程“{label}”失败：{error}")

    def worker(worker_id: int) -> None:
        worker_driver = None
        try:
            worker_driver = make_driver(
                ctx, headless=True, page_load_strategy="eager")
            worker_driver.set_page_load_timeout(30)
            _inject_cookies(ctx, worker_driver, cookies)
            results.put({"type": "ready", "worker_id": worker_id})
        except Exception as exc:
            results.put({
                "type": "init_failed",
                "worker_id": worker_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
            if worker_driver is not None:
                try:
                    worker_driver.quit()
                except WebDriverException:
                    pass
            return

        try:
            while not stop_event.is_set():
                try:
                    task = tasks.get(timeout=0.5)
                except queue.Empty:
                    continue
                if task is None:
                    return
                key = task["key"]
                course = task["course"]
                attempt = int(task.get("attempt") or 0)
                results.put({
                    "type": "started",
                    "worker_id": worker_id,
                    "key": key,
                    "course": course,
                    "attempt": attempt,
                })
                submitted = False
                reason = ""
                error = ""
                retryable = True
                fatal = False
                try:
                    submitted, reason = _evaluate_course_worker(
                        ctx, worker_driver, course)
                except KnownError as exc:
                    error = str(exc)
                    retryable = False
                    fatal = True
                except StructuralError as exc:
                    error = str(exc)
                    retryable = False
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    retryable = True
                results.put({
                    "type": "finished",
                    "worker_id": worker_id,
                    "key": key,
                    "course": course,
                    "attempt": attempt,
                    "submitted": submitted,
                    "reason": reason,
                    "error": error,
                    "retryable": retryable,
                    "fatal": fatal,
                })
        except Exception as exc:
            results.put({
                "type": "crashed",
                "worker_id": worker_id,
                "error": f"{type(exc).__name__}: {exc}",
            })
        finally:
            if worker_driver is not None:
                try:
                    worker_driver.quit()
                except WebDriverException:
                    pass
            results.put({"type": "done", "worker_id": worker_id})

    publish()
    ctx.status(
        f"启动 {worker_count} 个无头浏览器并行评教…", stage="evaluate")
    executor = ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="course-eval")
    futures = [
        executor.submit(worker, worker_id)
        for worker_id in range(1, worker_count + 1)
    ]
    try:
        while pending_keys:
            if not active_workers:
                for pending_key in list(pending_keys):
                    pending_index = index_by_key[pending_key]
                    statuses[pending_index] = "error"
                    failures[pending_key] = "没有可用的并行浏览器"
                pending_keys.clear()
                break
            try:
                event = results.get(timeout=0.5)
            except queue.Empty:
                continue
            event_type = str(event.get("type") or "")
            worker_id = int(event.get("worker_id") or 0)
            if worker_id not in worker_states:
                continue

            if event_type == "ready":
                worker_states[worker_id] = {"status": "空闲", "title": ""}
                publish()
                continue
            if event_type == "init_failed":
                active_workers.discard(worker_id)
                worker_states[worker_id] = {
                    "status": "初始化失败",
                    "title": str(event.get("error") or ""),
                }
                ctx.log(
                    f"并行进程 #{worker_id} 初始化失败："
                    f"{event.get('error') or ''}")
                publish()
                continue
            if event_type == "started":
                key = event["key"]
                index = index_by_key[key]
                statuses[index] = "active"
                worker_current[worker_id] = event
                worker_states[worker_id] = {
                    "status": "评教中",
                    "title": event["course"]["label"],
                }
                ctx.log(
                    f"[w{worker_id}] 开始评教："
                    f"{event['course']['label']}")
                publish()
                continue
            if event_type == "finished":
                key = event["key"]
                index = index_by_key[key]
                worker_current.pop(worker_id, None)
                worker_states[worker_id] = {"status": "空闲", "title": ""}
                if event.get("submitted"):
                    verified = _wait_for_course_done_specific(
                        ctx, driver, event["course"], timeout=20)
                    if verified:
                        statuses[index] = "done"
                        done_count += 1
                        pending_keys.discard(key)
                        _remove_cached_course(ctx, event["course"])
                        ctx.log(
                            f"[w{worker_id}] 已完成："
                            f"{event['course']['label']}")
                        publish(f"已完成：{event['course']['label']}")
                    else:
                        fail_task(
                            {
                                "key": key,
                                "course": event["course"],
                                "attempt": event.get("attempt", 0),
                            },
                            "提交后未确认课程完成",
                            True,
                        )
                        publish()
                else:
                    fail_task(
                        {
                            "key": key,
                            "course": event["course"],
                            "attempt": event.get("attempt", 0),
                        },
                        str(
                            event.get("error")
                            or event.get("reason")
                            or "提交失败"),
                        bool(event.get("retryable", True)),
                        bool(event.get("fatal", False)),
                    )
                    publish()
                continue
            if event_type == "crashed":
                active_workers.discard(worker_id)
                worker_states[worker_id] = {
                    "status": "出错",
                    "title": str(event.get("error") or ""),
                }
                task = worker_current.pop(worker_id, None)
                if task:
                    fail_task(
                        task,
                        str(event.get("error") or "并行浏览器崩溃"),
                        True,
                    )
                ctx.log(
                    f"并行进程 #{worker_id} 出错："
                    f"{event.get('error') or ''}")
                publish()
                continue
            if event_type == "done":
                active_workers.discard(worker_id)
                if worker_states.get(worker_id, {}).get("status") not in (
                        "初始化失败", "出错"):
                    worker_states[worker_id] = {
                        "status": "已关闭", "title": ""}
                publish()
                continue
    finally:
        stop_event.set()
        for _ in range(worker_count):
            tasks.put(None)
        for future in futures:
            try:
                future.result(timeout=30)
            except Exception:
                pass
        try:
            executor.shutdown(wait=False)
        except Exception:
            pass
        for state in worker_states.values():
            if state.get("status") not in ("初始化失败", "出错"):
                state["status"] = "已关闭"
                state["title"] = ""

    if failures:
        message = "；".join(
            f"{courses[index_by_key[key]].get('label') or key}: {error}"
            for key, error in failures.items()
        )
        _send_course_progress(
            ctx, courses, statuses,
            current="部分课程评教失败",
            done=done_count,
            workers=_worker_snapshot(worker_states),
        )
        ctx.status(
            f"{len(failures)} 门课程评教失败", stage="evaluate")
        raise RuntimeError(message)

    _send_course_progress(
        ctx, courses, statuses,
        current="全部课程已完成",
        done=done_count,
        workers=_worker_snapshot(worker_states),
    )
    return done_count


def _run(ctx: PluginContext) -> None:
    _CONTEXT.clear()
    if not ctx.username or not ctx.password:
        raise KnownError("未配置统一身份认证账号，请先在账号管理中选择账号")

    driver = make_driver(ctx, headless=True, page_load_strategy="eager")
    try:
        _set_context(stage="login")
        ctx.status("正在登录评教系统…", stage="login")
        _progress(
            ctx, "login",
            steps=[{
                "key": "discover",
                "label": "准备评教",
                "status": "active",
            }],
            title="正在登录评教系统", percent=0,
        )
        _login(ctx, driver)

        ctx.status("登录成功，正在读取课程缓存", stage="discover")
        _progress(
            ctx, "discover",
            steps=[{
                "key": "discover",
                "label": "读取课程缓存",
                "status": "active",
            }],
            title="正在读取课程缓存", percent=0,
        )

        _entry_page(ctx, driver)
        _load_all_cards(driver)
        list_handle = driver.current_window_handle
        pending = _pending_cards(driver)
        courses = _cached_courses(ctx)
        if not pending:
            # 列表刷新时可能短暂显示空列表，再确认一两次。
            for _ in range(2):
                time.sleep(0.5)
                _entry_page(ctx, driver)
                _load_all_cards(driver)
                pending = _pending_cards(driver)
                if pending:
                    break
        _update_cookies(ctx, driver)
        try:
            cookies = driver.get_cookies()
        except WebDriverException:
            cookies = []
        pending_keys = {_course_key(info) for _card, info in pending}
        if courses:
            ctx.log(
                f"读取本地缓存：{len(courses)} 门课程评教地址，跳过抓取")
            total = len(courses)
            statuses = ["pending"] * total
            stale: list[dict] = []
            for index, course in enumerate(courses):
                if _course_key(course) not in pending_keys:
                    statuses[index] = "done"
                    stale.append(course)
            if stale:
                _remove_cached_courses(ctx, stale)
                ctx.log(
                    f"缓存中 {len(stale)} 门课程已完成或不在待评教列表，"
                    "已从本地缓存移除")
            done_count = len(stale)
            _send_course_progress(
                ctx, courses, statuses, current="", done=done_count)
            if done_count == total:
                _progress(
                    ctx, "finish",
                    steps=_course_steps(courses, statuses),
                    done=total, total=total, percent=100,
                )
                ctx.status(
                    f"缓存中的 {total} 门课程均已完成", done=True)
                return
            ctx.status(
                f"读取本地缓存 {total} 门课程，开始评教",
                stage="evaluate")
        else:
            total = len(pending)
            if total == 0:
                ctx.log("本地缓存为空，且没有待评教课程")
                _progress(
                    ctx, "finish",
                    steps=[{
                        "key": "done",
                        "label": "没有待评教课程",
                        "status": "done",
                    }],
                    done=0, total=0, percent=100,
                )
                ctx.status("没有待评教课程", done=True)
                return

            ctx.log(
                f"本地缓存为空，发现 {total} 门待评教课程，"
                "开始记录评教地址")
            _progress(
                ctx, "discover",
                steps=[{
                    "key": "discover",
                    "label": "记录课程评教地址",
                    "status": "active",
                }],
                title="正在记录课程评教地址", percent=0,
            )
            courses = _discover_courses(ctx, driver, list_handle)
            if not courses:
                raise RuntimeError("未能记录任何课程的评教页面地址")
            _save_discovered(ctx, driver, courses)
            statuses = ["pending"] * len(courses)
            done_count = 0
            _send_course_progress(
                ctx, courses, statuses, current="", done=0)
            ctx.status(
                f"已记录 {len(courses)} 门课程，开始评教",
                stage="evaluate")

        worker_count = _worker_count(
            ctx, sum(1 for status in statuses if status == "pending"))
        if worker_count > 0:
            ctx.log(
                f"启动 {worker_count} 个无头浏览器并行评教"
                f"（共 {len(courses)} 门课程）")
        done_count = _evaluate_parallel(
            ctx, driver, courses, statuses,
            done_count, worker_count, cookies)

        _progress(
            ctx, "finish",
            steps=_course_steps(courses, statuses),
            done=len(courses), total=len(courses), percent=100,
        )
        ctx.status(
            f"全部评教完成，共 {len(courses)} 门课程", done=True)
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass


def main() -> None:
    ctx = PluginContext()
    try:
        _run(ctx)
    except KnownError:
        raise
    except Exception as exc:
        if not _is_expected_error(exc):
            _report_unknown(ctx, exc)
        raise


if __name__ == "__main__":
    main()


