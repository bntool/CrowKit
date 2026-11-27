# CrowKit｜小鸦工具箱

CrowKit 是面向校园学习与网络场景的 Windows 11 桌面工具箱。安装程序内置
Python 运行时和官方插件，无需单独配置 Python 环境。

## 功能

- **自动评教**：按账号缓存评教地址，使用多个无头浏览器并行完成期末学生评教。
- **教务抢课**：浏览专业课程、选择课程与班级，按时段筛选并持续轮询余量。
- **课程助手**：管理师道云与 SPOC 课程任务，支持视频学习、测验和互动。
- **校园网认证**：自动连接校园网，支持用户级开机自启。
- **统一账号管理**：统一身份认证账号由主程序加密保存并供插件选择。

## 快速开始

1. 打开 [Releases](https://github.com/bntool/CrowKit/releases/latest) 下载
   `CrowKit-Setup-*.exe`。
2. 运行安装程序，按向导完成安装。
3. 从开始菜单或桌面快捷方式启动“小鸦工具箱”。
4. 在“账号管理”中添加统一身份认证账号。
5. 打开所需插件，选择账号并完成配置后运行。

CrowKit 当前支持 Windows 11 x64 和 Microsoft Edge。首次运行浏览器插件时，
程序会检测并安装匹配的 EdgeDriver。

## 插件分发

官方插件源码位于本仓库的 `plugins/` 目录，并随安装程序一起分发。升级主程序时，
随包插件会同步到当前用户目录，不需要单独下载或访问插件仓库。

也可以从“插件管理”上传 zip 包安装第三方插件。zip 中需包含 `plugin.json`、
入口 Python 文件和可选的 `config_ui` HTML。

## 数据目录

程序文件默认安装到：

```text
C:\Program Files\CrowKit
```

账号、插件配置、缓存和日志保存在：

```text
%LOCALAPPDATA%\CrowKit
```

卸载程序默认保留该目录。插件日志位于 `logs/plugins`，插件缓存位于
`plugins/<plugin-id>`。

## 从源码运行

开发环境需要 Python 3.13：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app\main.py
```

也可以运行 `start.bat`。本地服务默认监听随机的本机端口。

## 构建安装程序

需要 Windows、Python 3.13 和 Inno Setup 6：

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

构建结果位于 `dist\installer\CrowKit-Setup-<version>.exe`。构建过程会嵌入
Python 运行时、`plugins/` 中的官方插件和 GlitchTip 上报配置。

## 项目结构

```text
app/               主程序、本地 API 与 Web 界面
plugins/           随安装包分发的官方插件源码
packaging/         Windows 安装程序与图标生成脚本
release-notes/     GitHub Release 说明
```

插件开发约定见 [PLUGIN_DEVELOPMENT.md](PLUGIN_DEVELOPMENT.md)。

## 许可

本项目使用 WTFPL 许可证。第三方组件许可见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
