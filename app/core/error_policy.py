"""Classify user-action failures that should never consume GlitchTip quota."""
from __future__ import annotations

import re

_EXPECTED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "账号",
        re.compile(
            r"未选择账号|请选择账号|请先添加账号|尚未添加账号|没有可用账号|"
            r"账号(?:信息)?(?:为空|不完整|缺失)|账号不能为空|无账号|"
            r"(?:未|请先?|需要).*(?:选择|配置|添加).*账号|账号.*未选择",
            re.IGNORECASE,
        ),
    ),
    (
        "登录凭据",
        re.compile(
            r"用户名或密码|密码错误|登录失败|认证失败|凭据|"
            r"学号不能为空|密码不能为空|请先登录|尚未登录|登录已失效|"
            r"http\s*(?:401|403)",
            re.IGNORECASE,
        ),
    ),
    (
        "配置",
        re.compile(
            r"请先?选择|请至少(?:选择|启用|勾选)|未选择|请填写|请输入|请设置|"
            r"不能为空|必填|未配置|配置(?:项)?(?:为空|缺失|不完整|无效|错误)|"
            r"参数(?:缺失|不完整|无效|错误)|课程.*(?:未选择|不能为空)|"
            r"课程.*时间冲突|所选课程.*冲突",
            re.IGNORECASE,
        ),
    ),
    (
        "浏览器",
        re.compile(
            r"webdriver|msedgedriver|浏览器.*(?:未安装|无法启动|不支持)|"
            r"(?:edge|驱动).*(?:未下载|未安装|未检测到|不匹配|不兼容|版本过低)|"
            r"请先安装.*edge|需要更高版本|请先更新|版本(?:过低|不兼容)|当前平台不支持",
            re.IGNORECASE,
        ),
    ),
    (
        "网络",
        re.compile(
            r"无网络|网络(?:连接|不可用|异常|错误|中断)|无法连接|连接(?:失败|超时|重置)|"
            r"failed to fetch|networkerror|offline|timed?\s*out|超时|"
            r"getaddrinfo|dns|err_(?:network|internet|connection|name)|"
            r"connection\s+(?:refused|reset|aborted)|无法获取(?:更新|课程|插件|资源|数据)|"
            r"请求失败|无法下载|下载失败|上传失败|server unavailable|"
            r"http\s*(?:429|500|502|503|504)",
            re.IGNORECASE,
        ),
    ),
    (
        "缓存或前置步骤",
        re.compile(
            r"尚未(?:检测到|获取|缓存|加载|打开)|未缓存|尚未缓存|"
            r"请先(?:获取|刷新|初始化|进入)|专业目录尚未|课程列表尚未|"
            r"没有可选课程|没有需要处理|没有任务|没有找到可执行|尚未安装",
            re.IGNORECASE,
        ),
    ),
    (
        "文件或权限",
        re.compile(
            r"文件不存在|找不到.*文件|路径(?:无效|不存在)|invalid zip|"
            r"不是有效的\s*zip|permission denied|access is denied|拒绝访问|"
            r"只读|磁盘空间不足|无法写入|文件被占用",
            re.IGNORECASE,
        ),
    ),
    (
        "任务状态",
        re.compile(
            r"已在运行|正在运行|任务已停止|已取消|停止中|未在运行|重复运行|"
            r"插件正在运行|请先停止|插件不存在|插件未安装|未安装插件|插件已安装",
            re.IGNORECASE,
        ),
    ),
    (
        "输入格式",
        re.compile(
            r"格式(?:错误|不正确)|不支持的.*格式|超出(?:允许)?范围|"
            r"无效的.*(?:配置|参数|输入)|invalid configuration|invalid argument",
            re.IGNORECASE,
        ),
    ),
)


def classify_user_error(*parts: object) -> dict | None:
    """Return a category for expected user-action failures, else None."""
    text = "\n".join(str(part or "") for part in parts)
    if not text.strip():
        return None
    for category, pattern in _EXPECTED_PATTERNS:
        if pattern.search(text):
            return {"category": category}
    return None


def friendly_user_message(value: object) -> str:
    """Remove a Python exception class prefix from a user-facing hint."""
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(
        r"^[A-Za-z_][\w.]*(?:Error|Exception):\s*",
        "",
        text,
    ).strip()
