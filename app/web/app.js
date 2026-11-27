/* CrowKit｜小鸦工具箱前端 — Vue 3 + Element Plus (本地 vendor) */
(function () {
  "use strict";

  const { createApp, reactive, ref, computed, watch, nextTick, onMounted } = Vue;
  const El = ElementPlus;
  const Icons = ElementPlusIconsVue;
  // 简单任务不需要独立运行页，启动成功后仍留在配置页。
  const PLUGINS_WITHOUT_RUN_TAB = new Set(["campus-network-auth"]);

  function pluginHasRunTab(pluginId) {
    return !PLUGINS_WITHOUT_RUN_TAB.has(pluginId);
  }

  // ---------------- API 工具 ----------------

  async function api(path, options) {
    const res = await fetch("/api" + path, options);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const data = await res.json();
        if (data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
      } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function post(path, body) {
    return api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  // ---------------- 状态 ----------------

  const state = reactive({
    page: "dashboard",            // dashboard | plugins | accounts | settings | detail
    detailId: null,
    plugins: [],
    catalog: [],
    catalogLoading: false,
    catalogError: "",
    catalogStale: false,
    catalogUpdatedAt: "",
    pluginAction: "",
    pluginDownload: {},           // pluginId -> {percent, downloaded, total}
    promptedPluginUpdates: false,
    promptedAppUpdate: false,
    globalConfig: null,
    specialtyCatalog: { levels: [] },
    specialtyCatalogLoading: false,
    specialtyCatalogError: "",
    appUpdate: null,
    appUpdatePrompt: false,
    wsConnected: false,
    themeMode: localStorage.getItem("crowkit-theme") || "system",
    progress: {},                 // pluginId -> {done,total,workers,...}
    runIds: {},                   // pluginId -> 当前/最近一次运行 ID
    lastStatus: {},               // pluginId -> 最近一次运行状态文本
    stoppingPlugins: {},          // pluginId -> 用户已请求停止
    edgeStatus: null,
    resources: null,
    edgeUpdating: false,
    detailConfig: null,
    detailSchema: [],
    savingConfig: false,
    savingGlobal: false,
    installing: false,
    uiHtml: null,                 // 自定义配置界面 HTML(插件 config_ui 提供)
    detailTab: "config",          // config | run
    errorStack: [],               // 最多三个手动关闭的错误卡片
  });

  let errorSequence = 0;
  const errorBundleTasks = new Map();

  function redactErrorMessage(value) {
    return String(value == null ? "未知错误" : value)
      .replace(
        /((?:password|passwd|pwd|campus_password|api[_-]?key|apikey|token|secret|cookie)\s*[:=]\s*)[^\s,;"']+/gi,
        "$1******")
      .replace(/([?&](?:api_?key|token|secret|password)=)[^&#\s]+/gi, "$1******");
  }

  function closeError(id) {
    state.errorStack = state.errorStack.filter((item) => item.id !== id);
    errorBundleTasks.delete(id);
  }

  function isExpectedError(message) {
    const text = String(message || "");
    return [
      /未选择账号|请选择账号|请先添加账号|尚未添加账号|没有可用账号|账号不能为空|账号.*不完整|(?:未|请先?|需要).*(?:选择|配置|添加).*账号|账号.*未选择/,
      /用户名或密码|密码错误|登录失败|认证失败|凭据|学号不能为空|密码不能为空|请先登录|尚未登录|登录已失效|http\s*(?:401|403)/i,
      /请先?选择|请至少(?:选择|启用|勾选)|未选择|请填写|请输入|请设置|不能为空|必填|未配置|配置(?:项)?(?:为空|缺失|不完整|无效|错误)|参数(?:缺失|不完整|无效|错误)|课程.*时间冲突|所选课程.*冲突/,
      /webdriver|msedgedriver|浏览器.*(?:未安装|无法启动|不支持)|(?:edge|驱动).*(?:未下载|未安装|未检测到|不匹配|不兼容|版本过低)|请先安装.*edge|需要更高版本|请先更新|当前平台不支持/i,
      /无网络|网络(?:连接|不可用|异常|错误|中断)|无法连接|连接(?:失败|超时|重置)|failed to fetch|networkerror|offline|timed?\s*out|超时|getaddrinfo|dns|err_(?:network|internet|connection|name)|无法获取(?:更新|课程|插件|资源|数据)|请求失败|无法下载|下载失败|上传失败|http\s*(?:429|500|502|503|504)/i,
      /尚未(?:检测到|获取|缓存|加载|打开)|未缓存|尚未缓存|请先(?:获取|刷新|初始化|进入)|专业目录尚未|课程列表尚未|没有可选课程|没有需要处理|没有任务|尚未安装/,
      /文件不存在|找不到.*文件|路径(?:无效|不存在)|invalid zip|不是有效的\s*zip|permission denied|access is denied|拒绝访问|只读|磁盘空间不足|无法写入|文件被占用/i,
      /已在运行|正在运行|任务已停止|已取消|停止中|未在运行|重复运行|插件正在运行|请先停止|插件不存在|插件未安装|未安装插件|插件已安装/,
      /格式(?:错误|不正确)|不支持的.*格式|超出(?:允许)?范围|无效的.*(?:配置|参数|输入)|invalid configuration|invalid argument/i,
    ].some((pattern) => pattern.test(text));
  }

  function showError(message, context, details) {
    const safeMessage = redactErrorMessage(message);
    const meta = details || {};
    const plugin = meta.plugin || state.detailId || "";
    const reportable = meta.expected === true ? false : !isExpectedError(safeMessage);
    const item = {
      id: ++errorSequence,
      message: safeMessage,
      context: redactErrorMessage(context || ""),
      plugin,
      runId: meta.runId || state.runIds[plugin] || "",
      time: new Date().toLocaleString("zh-CN", { hour12: false }),
      reportable,
      bundlePath: "",
      actionDescription: "",
      feedbackState: reportable ? "idle" : "not-needed",
      feedbackMessage: "",
      feedbackDownloadUrl: "",
    };
    state.errorStack.push(item);
    if (state.errorStack.length > 3) state.errorStack.shift();
    if (reportable) {
      const bundleTask = post("/system/error-report", {
        message: item.message,
        context: item.context,
        plugin: item.plugin,
        run_id: item.runId,
        source: "ui",
      }).then((result) => {
        const current = state.errorStack.find((entry) => entry.id === item.id);
        if (!current) return;
        current.bundlePath = result.path || "";
        if (result.expected) {
          current.reportable = false;
          current.feedbackState = "not-needed";
          current.context = "这是操作提示，不会提交到 GlitchTip";
          if (result.detail) current.message = result.detail;
        }
      }).catch(() => {});
      errorBundleTasks.set(item.id, bundleTask);
    }
  }

  async function openLogsForError(item) {
    try {
      const pending = item && errorBundleTasks.get(item.id);
      if (pending) await pending;
      let bundlePath = item && item.bundlePath;
      if (item && !bundlePath) {
        const result = await post("/system/error-report", {
          message: item.message,
          context: item.context,
          plugin: item.plugin,
          run_id: item.runId,
          source: "ui",
        });
        bundlePath = result.path || "";
        const current = state.errorStack.find((entry) => entry.id === item.id);
        if (current) current.bundlePath = bundlePath;
      }
      await post("/system/open-logs", bundlePath ? { path: bundlePath } : undefined);
    } catch (e) {
      El.ElNotification.warning({
        title: "无法打开日志目录",
        message: "请从全局设置中打开日志文件夹。",
        duration: 6000,
      });
    }
  }

  async function submitErrorFeedback(item) {
    const description = String(item.actionDescription || "").trim();
    if (!description) {
      item.feedbackMessage = "请先填写触发错误前的操作。";
      return;
    }
    item.feedbackState = "submitting";
    item.feedbackMessage = "正在检查版本并上传诊断信息…";
    item.feedbackDownloadUrl = "";
    try {
      const result = await post("/system/error-report/submit", {
        message: item.message,
        context: item.context,
        plugin: item.plugin,
        run_id: item.runId,
        bundle_path: item.bundlePath,
        source: "ui",
        action_description: description,
      });
      if (result.path) item.bundlePath = result.path;
      if (result.status === "submitted") {
        item.feedbackState = "submitted";
      } else {
        item.feedbackState = "blocked";
        item.feedbackDownloadUrl = result.download_url || "";
      }
      item.feedbackMessage = result.message || "";
    } catch (e) {
      item.feedbackState = "failed";
      item.feedbackMessage = "反馈上传失败：" + e.message;
    }
  }

  // 插件通过 Z.message("error", ...) 触发的错误统一进入诊断卡片。
  El.ElMessage.error = (message) => {
    const text = message && typeof message === "object"
      ? (message.message || JSON.stringify(message))
      : message;
    showError(text);
  };

  window.addEventListener("error", (event) => {
    if (!event.error) return;
    showError(
      "界面异常: " + (event.error.message || event.message || "未知错误"),
      "脚本错误"
    );
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    const message = reason && reason.message ? reason.message : String(reason || "");
    if (message) showError("未处理的异步异常: " + message, "Promise rejection");
  });

  // ---------------- WebSocket ----------------

  let ws = null;
  let wsTimer = null;

  function connectWS() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    ws = new WebSocket(proto + location.host + "/api/ws");
    ws.onopen = () => { state.wsConnected = true; };
    ws.onclose = () => {
      state.wsConnected = false;
      clearTimeout(wsTimer);
      wsTimer = setTimeout(connectWS, 2500);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === "hello") {
        (msg.history || []).forEach(handleEvent);
      } else {
        handleEvent(msg);
      }
    };
  }

  function handleEvent(msg) {
    const pid = msg.plugin;
    if (msg.type === "app_update") {
      state.appUpdate = Object.assign({}, state.appUpdate || {}, msg);
      promptAppUpdateIfNeeded();
      return;
    }
    if (msg.type === "plugin_catalog_updated") {
      loadPlugins();
      loadCatalog(true);
      const updated = Array.isArray(msg.updated) ? msg.updated : [];
      if (updated.length) {
        El.ElMessage.success("已自动更新 " + updated.length + " 个插件");
      }
      return;
    }
    if (msg.type === "plugin_catalog_error") {
      state.catalogError = msg.message || "随包插件同步失败";
      return;
    }
    if (!pid) return;
    if (msg.type === "log") {
      // 普通用户只看运行流程；完整日志由主程序写入 logs/plugins/。
      return;
    } else if (msg.type === "progress") {
      if (state.stoppingPlugins[pid]) return;
      const fields = Object.assign({}, msg);
      delete fields.type; delete fields.plugin; delete fields.__event__;
      state.progress[pid] = Object.assign({}, state.progress[pid] || {}, fields);
    } else if (msg.type === "status") {
      if (state.stoppingPlugins[pid]) return;
      state.lastStatus[pid] = msg.text || "";
      const fields = Object.assign({}, msg);
      delete fields.type; delete fields.plugin; delete fields.__event__;
      state.progress[pid] = Object.assign({}, state.progress[pid] || {}, fields);
    } else if (msg.type === "plugin_status") {
      updatePluginStatus(pid, msg.status);
      if (msg.status === "running") {
        if (msg.run_id) state.runIds[pid] = msg.run_id;
        delete state.stoppingPlugins[pid];
        delete state.progress[pid];
        state.lastStatus[pid] = "正在启动…";
        if (state.page === "detail" && state.detailId === pid
            && pluginHasRunTab(pid)) {
          state.detailTab = "run";
        }
      } else if (msg.status === "crashed") {
        const info = state.progress[pid];
        if (info && Array.isArray(info.steps)) {
          info.steps = info.steps.map((step) =>
            step.status === "active"
              ? Object.assign({}, step, { status: msg.expected ? "pending" : "error" })
              : step);
        }
        state.lastStatus[pid] = msg.expected
          ? "操作未完成，请检查配置或网络后重试"
          : "运行异常退出，请检查配置或网络后重试";
        const failure = msg.expected && msg.user_message
          ? msg.user_message
          : msg.error_message
            ? "插件未完成运行：" + msg.error_message
            : "插件运行异常退出（退出码 " + (msg.exit_code ?? "未知") + "）";
        showError(failure, "插件：" + pid, {
          plugin: pid,
          runId: msg.run_id || state.runIds[pid] || "",
          expected: msg.expected === true,
        });
      } else if (msg.status === "stopped") {
        const previous = state.progress[pid] || {};
        state.progress[pid] = {
          stage: "stopped",
          done: 0,
          total: 0,
          overall_percent: 0,
          steps: Array.isArray(previous.steps)
            ? previous.steps.map((step) =>
                Object.assign({}, step, { status: "pending" }))
            : [],
        };
        state.lastStatus[pid] = "已停止";
        delete state.stoppingPlugins[pid];
      }
    } else if (msg.type === "plugin_installed" || msg.type === "plugin_uninstalled") {
      loadPlugins();
      if (state.page === "plugins") loadCatalog(false);
    } else if (msg.type === "plugin_download") {
      state.pluginDownload[pid] = {
        percent: msg.percent,
        downloaded: msg.downloaded,
        total: msg.total,
      };
    }
  }

  function promptAppUpdateIfNeeded() {
    const info = state.appUpdate || {};
    if (!info.available || !info.download_url || state.promptedAppUpdate) return;
    state.promptedAppUpdate = true;
    state.appUpdatePrompt = true;
  }

  function updatePluginStatus(pid, status) {
    const p = state.plugins.find((x) => x.id === pid);
    if (p) {
      p.status = status;
      p.running = status === "running" || status === "starting" || status === "stopping";
    }
  }

  // ---------------- 主题 ----------------

  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme() {
    const mode = state.themeMode;
    const dark = mode === "dark" || (mode === "system" && media.matches);
    document.documentElement.classList.toggle("dark", dark);
  }

  media.addEventListener("change", () => { if (state.themeMode === "system") applyTheme(); });

  function setThemeMode(mode) {
    state.themeMode = mode;
    localStorage.setItem("crowkit-theme", mode);
    applyTheme();
  }

  // ---------------- 数据加载 ----------------

  async function loadPlugins() {
    const data = await api("/plugins");
    state.plugins = data.plugins;
    for (const p of state.plugins) {
      if (p.run_id) state.runIds[p.id] = p.run_id;
      if (p.progress && !state.stoppingPlugins[p.id]) {
        state.progress[p.id] = p.progress;
      }
    }
  }

  async function loadCatalog(force) {
    state.catalogLoading = true;
    state.catalogError = "";
    try {
      const data = await api("/plugins/catalog" + (force ? "?refresh=true" : ""));
      state.catalog = data.plugins || [];
      state.catalogStale = data.stale === true;
      state.catalogUpdatedAt = data.updated_at || "";
      if (data.error) state.catalogError = data.error;
    } catch (e) {
      state.catalogError = e.message;
    } finally {
      state.catalogLoading = false;
    }
  }

  async function loadAppUpdate(check) {
    try {
      state.appUpdate = check
        ? await api("/system/app-update/check", { method: "POST" })
        : await api("/system/app-update");
      promptAppUpdateIfNeeded();
    } catch (e) {
      state.appUpdate = {
        status: "error",
        current_version: state.appUpdate ? state.appUpdate.current_version : "",
        message: e.message,
      };
    }
  }

  async function loadGlobal() {
    const data = await api("/global");
    state.globalConfig = data.config;
  }

  async function loadSpecialtyCatalog() {
    state.specialtyCatalogLoading = true;
    state.specialtyCatalogError = "";
    try {
      state.specialtyCatalog = await api("/system/specialty-catalog");
    } catch (e) {
      state.specialtyCatalog = { levels: [] };
      state.specialtyCatalogError = e.message;
    } finally {
      state.specialtyCatalogLoading = false;
    }
  }

  let detailRequest = 0;

  async function openDetail(id) {
    const p = state.plugins.find((x) => x.id === id);
    if (!p) return;
    const requestId = ++detailRequest;
    state.detailId = id;
    state.page = "detail";
    state.detailTab = p.running && pluginHasRunTab(id) ? "run" : "config";
    state.detailSchema = p.config_schema || [];
    state.detailConfig = null;
    state.uiHtml = null;
    const data = await api("/plugins/" + id + "/config");
    if (requestId !== detailRequest || state.page !== "detail") return;
    if (p.config_ui) {
      // 自定义配置界面插件: 配置对象原样交给界面脚本, 不做编辑模型加工
      state.detailConfig = data.config;
      try {
        const ui = await api("/plugins/" + id + "/ui");
        if (requestId !== detailRequest || state.page !== "detail") return;
        state.uiHtml = ui.html;
      } catch (e) {
        // 界面加载失败时回退到 schema 简易表单, 需要编辑模型
        state.uiHtml = null;
        state.detailConfig = buildEditorModel(state.detailSchema, data.config);
        El.ElMessage.warning("自定义配置界面加载失败, 已回退简易表单: " + e.message);
      }
    } else {
      state.detailConfig = buildEditorModel(state.detailSchema, data.config);
    }
  }

  // ---------------- 配置表单辅助 ----------------

  function coursesToRows(courses) {
    return Object.entries(courses || {}).map(([code, classes]) => ({
      code,
      classes: (classes || []).join(", "),
    }));
  }

  function rowsToCourses(rows) {
    const out = {};
    (rows || []).forEach((r) => {
      const code = (r.code || "").trim();
      if (!code) return;
      const classes = (r.classes || "").split(/[,，\s]+/).filter(Boolean);
      out[code] = classes;
    });
    return out;
  }

  function buildEditorModel(schema, config) {
    // 为 course-table / string-list 生成便于编辑的中间结构
    const model = JSON.parse(JSON.stringify(config || {}));
    schema.forEach((f) => {
      if (f.type === "course-table") {
        model.__rows__ = model.__rows__ || coursesToRows(model[f.key]);
      }
      if (f.type === "string-list") {
        model.__text__ = model.__text__ || {};
        model.__text__[f.key] = (model[f.key] || []).join("\n");
      }
    });
    return model;
  }

  function serializeEditorModel(schema, model) {
    const out = JSON.parse(JSON.stringify(model));
    schema.forEach((f) => {
      if (f.type === "course-table") {
        out[f.key] = rowsToCourses(out.__rows__ || []);
      }
      if (f.type === "string-list") {
        const text = (out.__text__ && out.__text__[f.key]) || "";
        out[f.key] = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      }
    });
    delete out.__rows__;
    delete out.__text__;
    return out;
  }

  // ---------------- Vue 应用 ----------------

  const app = createApp({
    setup() {
      const detailPlugin = computed(() =>
        state.plugins.find((x) => x.id === state.detailId) || null);

      const runningCount = computed(() => state.plugins.filter((p) => p.running).length);

      const detailModel = computed({
        get: () => state.detailConfig,
        set: (v) => { state.detailConfig = v; },
      });

      onMounted(async () => {
        applyTheme();
        connectWS();
        await loadPlugins();
        await loadGlobal();
        await loadSpecialtyCatalog();
        await loadCatalog(false);
        await loadAppUpdate(false);
        await loadEdgeStatus();
        await promptMissingDriver();
        await loadResources();
      });

      const statusText = (p) => ({
        running: "运行中", stopped: "已停止", exited: "已结束",
        crashed: "异常退出", stopping: "停止中…", starting: "启动中…",
      }[p.status] || "已停止");

      const statusTagType = (p) =>
        p.running ? "success" : (p.status === "crashed" ? "danger" : "info");

      const pluginVersionLabel = (p) => {
        if (!p.installed) return "— → v" + (p.latest ? p.latest.version : "?");
        const installed = "v" + (p.installed_version || "?");
        if (!p.latest || !p.update_available) return installed;
        return installed + " → v" + p.latest.version;
      };

      function accountList() {
        return (state.globalConfig && state.globalConfig.accounts) || [];
      }

      function accountLabel(account) {
        if (!account) return "";
        return (account.nickname || "").trim() || account.username || "未命名账号";
      }

      function specialtyChildren(path) {
        let nodes = (state.specialtyCatalog && state.specialtyCatalog.levels) || [];
        for (const value of path) {
          const hit = nodes.find((item) => String(item.v) === String(value || ""));
          if (!hit) return [];
          nodes = hit.grades || hit.depts || hit.majors || [];
        }
        return nodes;
      }

      function accountLevelOptions() {
        return (state.specialtyCatalog && state.specialtyCatalog.levels) || [];
      }

      function accountGradeOptions(account) {
        return account && account.education_level
          ? specialtyChildren([account.education_level]) : [];
      }

      function accountDepartmentOptions(account) {
        return account && account.grade
          ? specialtyChildren([account.education_level, account.grade]) : [];
      }

      function accountMajorOptions(account) {
        return account && account.department
          ? specialtyChildren([
            account.education_level, account.grade, account.department,
          ]) : [];
      }

      function updateAccountSpecialty(account, field) {
        if (field === "education_level") {
          account.grade = "";
          account.department = "";
          account.major = "";
        } else if (field === "grade") {
          account.department = "";
          account.major = "";
        } else if (field === "department") {
          account.major = "";
        }
      }

      function selectedAccountId(pid) {
        const plugin = state.plugins.find((item) => item.id === pid);
        const selected = plugin && plugin.selected_account_id;
        if (selected && accountList().some((item) => item.id === selected)) {
          return selected;
        }
        return accountList().length ? accountList()[0].id : "";
      }

      async function changePluginAccount(pid, accountId) {
        if (accountId === "__add__") {
          state.page = "accounts";
          return;
        }
        try {
          await api("/plugins/" + pid + "/account", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ account_id: accountId }),
          });
          const plugin = state.plugins.find((item) => item.id === pid);
          if (plugin) plugin.selected_account_id = accountId;
          window.dispatchEvent(new CustomEvent("crowkit-account-changed", {
            detail: { pluginId: pid, accountId },
          }));
          El.ElMessage.success("已切换插件账号");
        } catch (e) {
          El.ElMessage.error(e.message);
          await loadPlugins();
        }
      }

      async function refresh() {
        await loadPlugins();
        if (state.page === "plugins") await loadCatalog(false);
      }

      async function loadEdgeStatus() {
        try {
          state.edgeStatus = await api("/system/edge");
        } catch (e) {
          state.edgeStatus = {
            edge_version: "",
            driver_version: "",
            compatible: false,
            edge_path: "",
            driver_path: "",
          };
        }
      }

      async function loadResources() {
        try {
          state.resources = await api("/system/resources");
        } catch (e) {
          state.resources = null;
        }
      }

      async function promptMissingDriver() {
        if (!state.edgeStatus || state.edgeStatus.driver_version) return;
        try {
          await El.ElMessageBox.confirm(
            "尚未检测到 Microsoft EdgeDriver。程序可以按当前 Edge 版本自动下载并安装驱动，是否现在下载？",
            "需要下载 EdgeDriver",
            { type: "info", confirmButtonText: "自动下载", cancelButtonText: "稍后" }
          );
        } catch (e) {
          return;
        }
        await updateEdgeDriver();
      }

      async function updateEdgeDriver() {
        state.edgeUpdating = true;
        try {
          const result = await api("/system/edge/driver", {
            method: "POST",
          });
          state.edgeStatus = result.status;
          El.ElMessage.success("Microsoft EdgeDriver 已更新");
        } catch (e) {
          El.ElMessage.error("EdgeDriver 更新失败: " + e.message);
          await loadEdgeStatus();
        } finally {
          state.edgeUpdating = false;
        }
      }

      async function checkEdgeDriver() {
        state.edgeUpdating = true;
        try {
          const result = await api("/system/edge/check", { method: "POST" });
          state.edgeStatus = result.status;
          El.ElMessage.success(
            result.updated ? "EdgeDriver 已更新" : "EdgeDriver 无需更新");
        } catch (e) {
          El.ElMessage.error("检查 EdgeDriver 失败: " + e.message);
          await loadEdgeStatus();
        } finally {
          state.edgeUpdating = false;
        }
      }

      async function openLogsFolder() {
        try {
          await post("/system/open-logs");
        } catch (e) {
          El.ElMessage.error(e.message);
        }
      }

      function addAccount() {
        if (!state.globalConfig) return;
        if (!Array.isArray(state.globalConfig.accounts)) {
          state.globalConfig.accounts = [];
        }
        const id = window.crypto && crypto.randomUUID
          ? crypto.randomUUID().replace(/-/g, "")
          : String(Date.now()) + Math.random().toString(16).slice(2);
        state.globalConfig.accounts.push({
          id,
          username: "",
          password: "",
          campus_password: "",
          nickname: "",
          education_level: "",
          grade: "",
          department: "",
          major: "",
        });
      }

      async function removeAccount(account) {
        try {
          await El.ElMessageBox.confirm(
            "确定删除账号「" + accountLabel(account) + "」吗？",
            "删除账号",
            { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
          );
        } catch (e) {
          return;
        }
        state.globalConfig.accounts = accountList().filter(
          (item) => item.id !== account.id);
        await saveGlobal();
        await loadPlugins();
      }

      async function startPlugin(id) {
        try {
          const saved = await saveConfigBeforeStart(id);
          if (!saved) return;
          if (id === "bdata-autoplay") {
            const data = await api("/plugins/" + id + "/config");
            const cfg = data.config || {};
            const fullAuto = cfg.full_auto === true
              && (cfg.auto_answer === true || cfg.comment_reply === true);
            if (fullAuto) {
              try {
                await El.ElMessageBox.confirm(
                  "当前 小鸦刷课已开启全自动模式。模型生成的师道云测验答案、课程评论和回复将不再经过人工核验，确认后可能直接提交。请确认你已检查模型、提示词和操作范围。",
                  "危险：全自动提交",
                  {
                    type: "warning",
                    confirmButtonText: "我已了解，继续启动",
                    cancelButtonText: "取消启动",
                    closeOnClickModal: false,
                    closeOnPressEscape: false,
                  }
                );
              } catch (e) {
                El.ElMessage.info("已取消启动");
                return;
              }
            }
          }
          delete state.progress[id];
          delete state.stoppingPlugins[id];
          state.lastStatus[id] = "正在启动…";
          const accountId = selectedAccountId(id);
          await post("/plugins/" + id + "/start", {
            account_id: accountId || undefined,
          });
          El.ElMessage.success("已启动");
          await refresh();
          if (state.page === "detail" && state.detailId === id
              && pluginHasRunTab(id)) {
            state.detailTab = "run";
          }
        } catch (e) {
          state.lastStatus[id] = "启动失败";
          El.ElMessage.error(e.message);
          await refresh();
        }
      }

      async function stopPlugin(id) {
        state.stoppingPlugins[id] = true;
        updatePluginStatus(id, "stopping");
        resetPluginProgress(id, "正在停止…");
        El.ElMessage.info("停止指令已发送");
        try {
          await post("/plugins/" + id + "/stop");
        } catch (e) {
          delete state.stoppingPlugins[id];
          delete state.progress[id];
          El.ElMessage.error(e.message);
          await refresh();
        }
      }

      async function saveDetailConfig(silent) {
        if (!state.detailConfig) return;
        state.savingConfig = true;
        try {
          const config = serializeEditorModel(state.detailSchema, state.detailConfig);
          await api("/plugins/" + state.detailId + "/config", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config }),
          });
          if (silent !== true) El.ElMessage.success("配置已保存");
          return true;
        } catch (e) {
          if (silent !== true) El.ElMessage.error(e.message);
          return false;
        }
        finally { state.savingConfig = false; }
      }

      async function saveConfigBeforeStart(pluginId) {
        if (state.page !== "detail" || state.detailId !== pluginId) return true;
        if (state.uiHtml) {
          const saver = window.BnuToolBoxPluginSave
            || (pluginId === "bdata-autoplay" && window.BnuToolBoxBnuSave)
            || (pluginId === "campus-network-auth" && window.BnuToolBoxCampusSave);
          if (typeof saver !== "function") return true;
          try {
            return (await saver()) !== false;
          } catch (e) {
            El.ElMessage.error("配置保存失败: " + e.message);
            return false;
          }
        }
        return (await saveDetailConfig(true)) !== false;
      }

      async function saveGlobal() {
        state.savingGlobal = true;
        try {
          await api("/global", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ config: state.globalConfig }),
          });
          El.ElMessage.success("全局配置已保存");
        } catch (e) { El.ElMessage.error(e.message); }
        finally { state.savingGlobal = false; }
      }

      async function uninstallPlugin(p) {
        try {
          await El.ElMessageBox.confirm(
            "确定删除插件「" + p.name + "」?其配置数据将保留。",
            "删除确认", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" }
          );
        } catch (e) { return; }
        try {
          await api("/plugins/" + p.id, { method: "DELETE" });
          El.ElMessage.success("已删除");
          await refresh();
        } catch (e) { El.ElMessage.error(e.message); }
      }

      async function installCatalogPlugin(p, update) {
        const verb = update ? "更新" : "安装";
        try {
          await El.ElMessageBox.confirm(
            "确定" + verb + "插件「" + p.name + "」吗？",
            verb + "插件",
            { type: "info", confirmButtonText: verb, cancelButtonText: "取消" }
          );
        } catch (e) { return; }
        state.pluginAction = p.id;
        delete state.pluginDownload[p.id];
        try {
          const result = await post(
            "/plugins/catalog/" + encodeURIComponent(p.id) + "/install",
            { update: !!update }
          );
          El.ElMessage.success(
            "已" + verb + " " + result.name + " v" + result.version);
          await loadPlugins();
          await loadCatalog(true);
        } catch (e) {
          El.ElMessage.error(e.message);
        } finally {
          state.pluginAction = "";
          delete state.pluginDownload[p.id];
        }
      }

      async function promptPluginUpdates(updates) {
        try {
          await El.ElMessageBox.confirm(
            "发现 " + updates.length + " 个插件可更新：" +
            updates.map((p) => p.name + " v" + p.latest.version).join("、") +
            "。是否立即安装？",
            "插件更新",
            { type: "info", confirmButtonText: "全部更新", cancelButtonText: "稍后" }
          );
        } catch (e) { return; }
        await updateAllPlugins();
      }

      async function updateAllPlugins() {
        const updates = state.catalog.filter((p) => p.update_available);
        if (!updates.length) return;
        state.pluginAction = "all";
        let failed = 0;
        for (const p of updates) {
          try {
            await post(
              "/plugins/catalog/" + encodeURIComponent(p.id) + "/install",
              { update: true }
            );
          } catch (e) {
            failed += 1;
          }
        }
        state.pluginAction = "";
        for (const p of updates) delete state.pluginDownload[p.id];
        await loadPlugins();
        await loadCatalog(true);
        if (failed) El.ElMessage.warning(failed + " 个插件更新失败");
        else El.ElMessage.success("插件已全部更新");
      }

      async function checkAppUpdate() {
        state.pluginAction = "app-update-check";
        await loadAppUpdate(true);
        state.pluginAction = "";
        const info = state.appUpdate || {};
        if (info.status === "error") El.ElMessage.error(info.message);
        else if (!info.available) {
          El.ElMessage.success(info.message || "当前已是最新版本");
        }
      }

      async function downloadAppUpdate() {
        state.pluginAction = "app-update-download";
        try {
          state.appUpdate = Object.assign(
            {}, state.appUpdate || {},
            await post("/system/app-update/download"));
          state.appUpdatePrompt = true;
        } catch (e) {
          El.ElMessage.error(e.message);
        } finally {
          state.pluginAction = "";
        }
      }

      function triggerInstall() {
        const input = document.getElementById("zip-input");
        input.value = "";
        input.click();
      }

      async function onZipChosen(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) return;
        state.installing = true;
        try {
          const fd = new FormData();
          fd.append("file", file);
          const res = await api("/plugins/install", { method: "POST", body: fd });
          El.ElMessage.success("插件安装成功: " + res.name);
          await refresh();
        } catch (e) { El.ElMessage.error(e.message); }
        finally { state.installing = false; }
      }

      function progressInfo(pid) {
        return state.progress[pid] || null;
      }

      function statusDone(pid) {
        const info = state.progress[pid];
        return !!(info && (info.done === true || info.stage === "finish"));
      }

      function fallbackSteps(pid, stage) {
        if (pid === "bdata-autoplay") {
          return stage === "cache"
            ? [
                { key: "login", label: "登录平台", status: "pending" },
                { key: "course-list", label: "获取课程列表", status: "pending" },
                { key: "details", label: "获取课程详情", status: "pending" },
                { key: "spoc-details", label: "获取 SPOC 课程详情",
                  status: "pending", parent: "details" },
                { key: "bdata-details", label: "获取师道云课程与任务详情",
                  status: "pending", parent: "details" },
              ]
            : [
                { key: "prepare", label: "准备任务", status: "pending" },
                { key: "login", label: "登录平台", status: "pending" },
                { key: "study", label: "视频学习", status: "pending" },
                { key: "qa", label: "答题与互动", status: "pending" },
              ];
        }
        if (pid === "course-grab") {
          return stage === "catalog"
            ? [
                { key: "login", label: "登录教务系统", status: "pending" },
                { key: "catalog", label: "抓取专业目录", status: "pending" },
                { key: "save", label: "保存目录缓存", status: "pending" },
              ]
            : stage === "courses"
              ? [
                  { key: "login", label: "登录教务系统", status: "pending" },
                  { key: "query", label: "查询课程", status: "pending" },
                  { key: "pages", label: "读取课程分页", status: "pending" },
                  { key: "save", label: "保存课程缓存", status: "pending" },
                ]
              : [
                  { key: "config", label: "读取课程配置", status: "pending" },
                  { key: "login", label: "登录教务系统", status: "pending" },
                  { key: "poll", label: "轮询课程余量", status: "pending" },
                  { key: "submit", label: "提交选课", status: "pending" },
                ];
        }
        if (pid === "campus-network-auth") {
          return [
            { key: "network", label: "检测网络", status: "pending" },
            { key: "auth", label: "请求认证", status: "pending" },
            { key: "verify", label: "确认连接", status: "pending" },
          ];
        }
        return [
          { key: "prepare", label: "准备", status: "pending" },
          { key: "run", label: "运行", status: "pending" },
        ];
      }

      function resetPluginProgress(pid, text) {
        const old = state.progress[pid] || {};
        const steps = fallbackSteps(pid, old.stage || "");
        state.progress[pid] = {
          stage: "stopped",
          done: 0,
          total: 0,
          overall_percent: 0,
          steps,
        };
        state.lastStatus[pid] = text;
      }

      function runSteps(pid) {
        const info = state.progress[pid] || {};
        if (Array.isArray(info.steps) && info.steps.length) {
          return info.steps.map((step, index) => ({
            key: String(step.key || index),
            label: step.label || "步骤 " + (index + 1),
            status: ["done", "active", "error"].includes(step.status)
              ? step.status : "pending",
            parent: step.parent ? String(step.parent) : "",
          }));
        }
        const steps = fallbackSteps(pid, info.stage || "");
        if (info.stage === "stopped") {
          return steps.map((step) => ({ ...step, status: "pending" }));
        }
        const done = statusDone(pid);
        const total = Number(info.total) || 0;
        const count = Number(info.done) || 0;
        let activeIndex = 0;
        if (done) activeIndex = steps.length;
        else if (pid === "bdata-autoplay" && info.stage === "cache") {
          activeIndex = total > 0 && count >= total ? 2 : 1;
        } else if (pid === "bdata-autoplay" && info.workers) {
          activeIndex = 2;
        } else if (pid === "course-grab" && info.stage === "catalog") {
          activeIndex = total > 0 && count >= total ? 2 : 1;
        } else if (pid === "course-grab" && info.stage === "courses") {
          activeIndex = total > 0 && count >= total ? 3 : 2;
        } else if (pid === "course-grab" && info.round) {
          activeIndex = 2;
        } else if (pid === "campus-network-auth" && info.total > 0) {
          activeIndex = count >= total ? 3 : 1;
        }
        return steps.map((step, index) => ({
          ...step,
          status: index < activeIndex
            ? "done"
            : index === activeIndex
              ? "active"
              : "pending",
        }));
      }

      function runMainSteps(pid) {
        return runSteps(pid).filter((step) => !step.parent);
      }

      function runChildSteps(pid, parent) {
        return runSteps(pid).filter((step) => step.parent === parent);
      }

      function runMetrics(pid) {
        const info = state.progress[pid] || {};
        let percent = 0;
        if (info.overall_percent !== undefined
            && info.overall_percent !== null
            && Number.isFinite(Number(info.overall_percent))) {
          percent = Number(info.overall_percent);
        } else if (Number(info.total) > 0) {
          percent = Number(info.done || 0) / Number(info.total) * 100;
        } else if (Number(info.qa_total) > 0) {
          percent = Number(info.qa_done || 0) / Number(info.qa_total) * 100;
        } else if (statusDone(pid)) {
          percent = 100;
        }
        const eta = info.eta_s !== undefined && info.eta_s !== null
          ? info.eta_s : info.eta_seconds;
        return {
          percent: Math.max(0, Math.min(100, Number(percent) || 0)),
          etaS: eta !== undefined && eta !== null && isFinite(Number(eta))
            ? Number(eta) : null,
          watchedS: info.watched_seconds !== undefined
            && info.watched_seconds !== null
            && Number.isFinite(Number(info.watched_seconds))
            ? Number(info.watched_seconds) : null,
          totalS: info.total_seconds !== undefined
            && info.total_seconds !== null
            && Number.isFinite(Number(info.total_seconds))
            ? Number(info.total_seconds) : null,
          qaDone: Number(info.qa_done) || 0,
          qaTotal: Number(info.qa_total) || 0,
          qaTitle: info.qa_title || "",
          showEta: info.stage === "study",
        };
      }

      function workerList(pid) {
        const info = state.progress[pid];
        if (!info || !info.workers) return [];
        return Object.entries(info.workers).map(([id, w]) => ({
          id,
          status: w.status || "-",
          course: w.course || "",
          title: w.title || w.url || "",
          percent: typeof w.percent === "number" ? w.percent : null,
          etaS: (w.eta_s === null || w.eta_s === undefined) ? null : w.eta_s,
          totalS: (w.total_s === null || w.total_s === undefined) ? null : w.total_s,
          replays: w.replays || 0,
          watchedS: w.watched_s === undefined ? null : w.watched_s,
        }));
      }

      function workerTagType(s) {
        if (s === "已完成") return "success";
        if (s === "出错" || s === "崩溃") return "danger";
        if (s === "空闲" || s === "已关闭") return "info";
        if (s === "播放中" || s === "重播中") return "";
        return "warning";
      }

      function fmtDur(s) {
        if (s === null || s === undefined || !isFinite(s)) return "-";
        s = Math.max(0, Math.round(s));
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
        return h ? h + ":" + mm + ":" + ss : m + ":" + ss;
      }

      function fmtBytes(value) {
        const bytes = Number(value);
        if (!Number.isFinite(bytes) || bytes < 0) return "—";
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
        if (bytes < 1024 * 1024 * 1024) {
          return (bytes / 1024 / 1024).toFixed(1) + " MB";
        }
        return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
      }

      function fmtSpeed(value) {
        const rate = Number(value);
        return Number.isFinite(rate) && rate > 0
          ? fmtBytes(rate) + "/s" : "—";
      }

      function fmtEta(s) {
        if (s === null || s === undefined || !isFinite(s)) return "-";
        s = Math.round(s);
        if (s < 60) return s + " 秒";
        const m = Math.round(s / 60);
        if (m < 60) return m + " 分";
        return Math.floor(m / 60) + " 时 " + (m % 60) + " 分";
      }

      function fmtEtaClock(s) {
        if (s === null || s === undefined || !isFinite(s)) return "-";
        const d = new Date(Date.now() + s * 1000);
        return String(d.getHours()).padStart(2, "0") + ":" +
               String(d.getMinutes()).padStart(2, "0");
      }

      function addCourseRow() {
        if (!state.detailConfig) return;
        if (!state.detailConfig.__rows__) state.detailConfig.__rows__ = [];
        state.detailConfig.__rows__.push({ code: "", classes: "" });
      }

      function removeCourseRow(idx) {
        if (!state.detailConfig || !state.detailConfig.__rows__) return;
        state.detailConfig.__rows__.splice(idx, 1);
      }

      // ---------------- 自定义配置界面桥(config_ui 插件) ----------------
      // 宿主向界面脚本注入 window.BnuToolBoxConfigUI:
      //   getConfig()  -> Promise<config 对象(密码为 ****** 打码)>
      //   saveConfig(obj) -> Promise 保存(****** 字段由后端保持原值)
      //   api(path, options) -> fetch("/api/"+path) 并按 content-type 解析
      //   post(path, body) -> 便捷 POST JSON
      //   message(type, text) -> 顶部消息(type: success/warning/error/info)
      // 界面 HTML 由 /api/plugins/<id>/ui 下发(已剥离 <style>, 风格用宿主 crowkit-* 类)。

      function mountCustomUI(el) {
        if (!el) return;
        const pid = state.detailId || "";
        delete window.BnuToolBoxPluginSave;
        delete window.BnuToolBoxBnuSave;
        delete window.BnuToolBoxCampusSave;
        // 先注入桥再重建脚本节点: 界面脚本执行时 window.BnuToolBoxConfigUI 必须已存在
        window.BnuToolBoxConfigUI = {
          getConfig: async () => {
            const data = await api("/plugins/" + pid + "/config");
            state.detailConfig = data.config;
            return data.config;
          },
          saveConfig: async (cfg) => {
            await api("/plugins/" + pid + "/config", {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ config: cfg }),
            });
            state.detailConfig = cfg;
          },
          api: async (path, options) => {
            const res = await fetch("/api/" + String(path).replace(/^\//, ""), options);
            if (!res.ok) {
              let detail = res.statusText;
              try {
                const d = await res.json();
                if (d.detail) detail = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
              } catch (e) { /* ignore */ }
              throw new Error(detail);
            }
            const ct = res.headers.get("content-type") || "";
            return ct.includes("application/json") ? res.json() : res.text();
          },
          post: (path, body) => window.BnuToolBoxConfigUI.api(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
          }),
          getSelectedAccount: async () => {
            const data = await api("/plugins/" + pid + "/selected-account");
            return data.account || null;
          },
          showRun: () => {
            if (state.page === "detail" && state.detailId === pid
                && pluginHasRunTab(pid)) {
              state.detailTab = "run";
            }
          },
          confirm: async (message, title, options) => {
            try {
              await El.ElMessageBox.confirm(message, title || "请确认", {
                type: "warning",
                confirmButtonText: "确认",
                cancelButtonText: "取消",
                ...(options || {}),
              });
              return true;
            } catch (e) {
              return false;
            }
          },
          message: (type, text) => {
            const m = El.ElMessage;
            (m[type] || m.info).call(m, text);
          },
        };
        let autoSaveTimer = null;
        const autoSave = () => {
          if (autoSaveTimer) clearTimeout(autoSaveTimer);
          autoSaveTimer = setTimeout(async () => {
            const saver = window.BnuToolBoxPluginSave;
            if (typeof saver !== "function") return;
            try {
              await saver();
            } catch (e) {
              // 输入过程中短暂的不完整配置不应打断用户操作。
            }
          }, 160);
        };
        if (!el.dataset.autosaveBound) {
          el.dataset.autosaveBound = "1";
          el.addEventListener("focusout", autoSave);
          el.addEventListener("change", autoSave);
        }
        el.innerHTML = state.uiHtml || "";
        // innerHTML 注入的 <script> 不执行, 需重建节点
        el.querySelectorAll("script").forEach((old) => {
          const s = document.createElement("script");
          s.textContent = old.textContent;
          old.replaceWith(s);
        });
      }

      watch(() => state.uiHtml, () => {
        if (state.page === "detail" && state.detailId) {
          nextTick(() => mountCustomUI(document.getElementById("plugin-ui")));
        }
      });

      watch(() => state.detailId, () => {
        state.uiHtml = null;
        delete window.BnuToolBoxPluginSave;
        delete window.BnuToolBoxBnuSave;
        delete window.BnuToolBoxCampusSave;
      });

      watch(() => state.page, (page) => {
        if (page === "settings") loadEdgeStatus();
        if (page === "plugins") loadCatalog(false);
        if (page === "settings") loadAppUpdate(false);
      });

      return {
        state, detailPlugin, runningCount, detailModel, statusDone,
        statusText, statusTagType, progressInfo, workerList,
        runSteps, runMainSteps, runChildSteps, runMetrics,
        workerTagType, fmtDur, fmtEta, fmtEtaClock, fmtBytes, fmtSpeed,
        startPlugin, stopPlugin, saveDetailConfig, saveGlobal,
        pluginHasRunTab,
        uninstallPlugin, triggerInstall, onZipChosen,
        installCatalogPlugin, updateAllPlugins, checkAppUpdate,
        downloadAppUpdate,
        pluginVersionLabel,
        addCourseRow, removeCourseRow, setThemeMode, refresh,
        loadEdgeStatus, updateEdgeDriver, checkEdgeDriver, openLogsFolder,
        accountLabel, selectedAccountId, changePluginAccount,
        accountLevelOptions, accountGradeOptions, accountDepartmentOptions,
        accountMajorOptions, updateAccountSpecialty,
        addAccount, removeAccount, Icons,
        closeError, openLogsForError, submitErrorFeedback,
      };
    },

    template: `
<div class="layout">
  <div class="error-modal-layer" v-if="state.errorStack.length" role="presentation">
    <div class="error-modal-backdrop"></div>
    <div class="error-modal-stack" role="alertdialog" aria-label="错误提示">
      <section
        class="error-modal-card"
        :class="{'is-expected': !item.reportable}"
        v-for="item in state.errorStack"
        :key="item.id">
        <div class="error-modal-title">
          <strong>{{ item.reportable ? "运行出错" : "操作提示" }}</strong>
          <button type="button" aria-label="关闭错误提示" @click="closeError(item.id)">×</button>
        </div>
        <p>{{ item.message }}</p>
        <div class="error-modal-meta">
          {{ item.time }} ·
          <template v-if="!item.reportable">{{ item.context || "请检查相关配置后重试" }}</template>
          <template v-else>本次运行日志已保存在本地</template>
        </div>
        <div class="error-feedback" v-if="item.reportable">
          <label>请描述出错前执行了什么操作</label>
          <textarea
            v-model="item.actionDescription"
            maxlength="2000"
            :disabled="['submitting','submitted','blocked'].includes(item.feedbackState)"
            placeholder="例如：获取课程详情后在配置页点击启动"></textarea>
          <div class="error-feedback-message" v-if="item.feedbackMessage">
            {{ item.feedbackMessage }}
          </div>
          <a
            v-if="item.feedbackDownloadUrl"
            class="main-update-link"
            :href="item.feedbackDownloadUrl"
            target="_blank"
            rel="noopener noreferrer">下载主程序更新</a>
        </div>
        <div class="error-modal-actions">
          <template v-if="item.reportable">
            <el-button size="small" @click="openLogsForError(item)">打开日志文件夹</el-button>
            <el-button
              size="small"
              type="primary"
              :loading="item.feedbackState==='submitting'"
              :disabled="!item.actionDescription.trim() || ['submitted','blocked'].includes(item.feedbackState)"
              @click="submitErrorFeedback(item)">反馈错误</el-button>
            <el-button size="small" @click="closeError(item.id)">关闭</el-button>
          </template>
          <el-button v-else size="small" @click="closeError(item.id)">知道了</el-button>
        </div>
      </section>
    </div>
  </div>

  <div class="main-update-layer" v-if="state.appUpdatePrompt && state.appUpdate" role="presentation">
    <div class="error-modal-backdrop"></div>
    <section class="main-update-card" role="dialog" aria-modal="true" aria-labelledby="main-update-title">
      <span class="main-update-kicker">主程序更新</span>
      <h2 id="main-update-title">发现新版本 v{{ state.appUpdate.latest_version }}</h2>
      <p>点击“下载新版本”后将自动启动安装程序，CrowKit 会随后退出。安装仍需你手动完成。</p>
      <div
        class="main-update-progress"
        v-if="['downloading','downloaded','installing','error'].includes(state.appUpdate.status)">
        <el-progress
          :percentage="Number(state.appUpdate.percent) || 0"
          :stroke-width="10"
          :show-text="false" />
        <div class="main-update-metrics">
          <span>进度 {{ Number(state.appUpdate.percent || 0).toFixed(1) }}%</span>
          <span>总大小 {{ fmtBytes(state.appUpdate.total_bytes || state.appUpdate.installer_size) }}</span>
          <span>已下载 {{ fmtBytes(state.appUpdate.downloaded_bytes || 0) }}</span>
          <span>速度 {{ fmtSpeed(state.appUpdate.speed_bps) }}</span>
          <span>剩余 {{ fmtEta(state.appUpdate.eta_seconds) }}</span>
        </div>
        <p class="main-update-status">{{ state.appUpdate.message }}</p>
      </div>
      <div class="main-update-actions">
        <el-button
          :disabled="['downloading','downloaded','installing'].includes(state.appUpdate.status)"
          @click="state.appUpdatePrompt=false">取消</el-button>
        <el-button
          v-if="!['downloading','downloaded','installing'].includes(state.appUpdate.status)"
          type="primary"
          :loading="state.pluginAction==='app-update-download'"
          @click="downloadAppUpdate">
          {{ state.appUpdate.status==='error' ? '重新下载' : '下载新版本' }}
        </el-button>
      </div>
      <a
        v-if="state.appUpdate.release_url"
        class="main-update-notes"
        :href="state.appUpdate.release_url"
        target="_blank"
        rel="noopener noreferrer">查看更新说明</a>
    </section>
  </div>

  <input type="file" id="zip-input" accept=".zip" style="display:none" @change="onZipChosen">

  <!-- 侧边栏 -->
  <aside class="sidebar">
    <div class="brand">
      <img class="logo" src="/assets/crowkit.svg" alt="">
      <div>CrowKit<small>小鸦工具箱</small></div>
    </div>
    <nav class="nav">
      <div class="nav-item" :class="{active: state.page==='dashboard'}" @click="state.page='dashboard'">
        <el-icon><odometer /></el-icon> 仪表盘
      </div>
      <div class="nav-item" :class="{active: state.page==='plugins'}" @click="state.page='plugins'">
        <el-icon><box /></el-icon> 插件管理
      </div>
      <div class="nav-item" :class="{active: state.page==='accounts'}" @click="state.page='accounts'">
        <el-icon><user /></el-icon> 账号管理
      </div>
      <div class="nav-item" :class="{active: state.page==='settings'}" @click="state.page='settings'">
        <el-icon><setting /></el-icon> 全局设置
      </div>
    </nav>
    <div class="sidebar-foot">
      <span>{{ runningCount }} 个运行中</span>
      <el-radio-group :model-value="state.themeMode" size="small" @update:model-value="setThemeMode">
        <el-radio-button value="light">浅</el-radio-button>
        <el-radio-button value="system">随</el-radio-button>
        <el-radio-button value="dark">深</el-radio-button>
      </el-radio-group>
    </div>
  </aside>

  <!-- 主内容 -->
  <main class="content">

    <!-- 仪表盘 -->
    <template v-if="state.page==='dashboard'">
      <div class="page-header">
        <h1>仪表盘</h1>
        <p>已安装 {{ state.plugins.length }} 个插件</p>
      </div>
      <div class="card-grid">
        <div class="plugin-card" v-for="p in state.plugins" :key="p.id">
          <div class="head">
            <div class="title">
              <div class="icon" style="background:linear-gradient(135deg,var(--crowkit-accent),var(--crowkit-accent-2))">
                <el-icon><magic-stick /></el-icon>
              </div>
              <span>{{ p.name }}</span>
            </div>
            <el-tooltip :content="statusText(p)" placement="top">
              <span class="running-dot" v-if="p.running"></span>
              <span class="running-dot" style="background:var(--crowkit-border);animation:none" v-else></span>
            </el-tooltip>
          </div>
          <div class="desc">{{ p.description }}</div>
          <div class="meta-line">
            <span>v{{ p.version }}</span>
            <span>{{ statusText(p) }}</span>
          </div>
          <div class="actions">
            <el-button v-if="!p.running" type="primary" @click="startPlugin(p.id)">
              <el-icon style="margin-right:4px"><video-play /></el-icon>启动
            </el-button>
            <el-button v-else type="danger" plain @click="stopPlugin(p.id)">
              <el-icon style="margin-right:4px"><video-pause /></el-icon>停止
            </el-button>
            <el-button @click="openDetailById(p.id)">打开</el-button>
          </div>
        </div>
      </div>
    </template>

    <!-- 插件管理 -->
    <template v-if="state.page==='plugins'">
      <div class="page-header">
        <h1>插件管理</h1>
        <p>官方插件随安装程序分发，也可使用 zip 包安装其他插件</p>
      </div>
      <div class="panel">
        <h2><el-icon><upload /></el-icon> 本地安装</h2>
        <el-button :loading="state.installing" @click="triggerInstall">
          <el-icon style="margin-right:4px"><upload /></el-icon>上传插件 zip 包
        </el-button>
        <span class="form-hint" style="margin-left:12px">
          zip 内需包含 plugin.json 与入口 py 文件(可含一层目录)
        </span>
      </div>
      <div class="panel">
        <div class="catalog-heading">
          <h2><el-icon><box /></el-icon> 随包插件</h2>
          <div class="catalog-actions">
            <span class="form-hint" v-if="state.catalogUpdatedAt">
              扫描于 {{ state.catalogUpdatedAt }}
            </span>
            <el-button size="small" :loading="state.catalogLoading" @click="loadCatalog(true)">
              重新扫描
            </el-button>
            <el-button
              v-if="state.catalog.some(p => p.update_available)"
              size="small"
              type="primary"
              :loading="state.pluginAction==='all'"
              @click="updateAllPlugins">
              同步全部
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="state.catalogError"
          :title="state.catalogError"
          :type="state.catalogStale ? 'warning' : 'error'"
          :closable="false"
          show-icon
          style="margin-bottom:12px" />
        <el-table v-loading="state.catalogLoading" :data="state.catalog" style="width:100%">
          <el-table-column label="插件" min-width="230">
            <template #default="scope">
              <div class="catalog-name">
                <strong>{{ scope.row.name }}</strong>
                <el-tag v-if="scope.row.bundled" size="small" type="success" effect="plain">随包</el-tag>
                <el-tag v-if="scope.row.local_only" size="small" type="info" effect="plain">本地</el-tag>
                <el-tag v-if="scope.row.update_available" size="small" type="warning" effect="plain">可更新</el-tag>
              </div>
              <div class="form-hint">{{ scope.row.description }}</div>
              <div
                class="plugin-download"
                v-if="state.pluginDownload[scope.row.id]">
                <span>
                  正在同步
                  {{ state.pluginDownload[scope.row.id].percent !== null
                    ? state.pluginDownload[scope.row.id].percent + "%" : "…" }}
                </span>
                <el-progress
                  :percentage="state.pluginDownload[scope.row.id].percent || 0"
                  :indeterminate="state.pluginDownload[scope.row.id].percent === null"
                  :show-text="false"
                  :stroke-width="6" />
              </div>
            </template>
          </el-table-column>
          <el-table-column label="版本" width="120">
            <template #default="scope">{{ pluginVersionLabel(scope.row) }}</template>
          </el-table-column>
          <el-table-column label="依赖" width="130">
            <template #default="scope">
              <span v-if="scope.row.dependency">宿主 {{ scope.row.dependency }}+</span>
              <span v-else class="form-hint">—</span>
              <el-tag v-if="scope.row.installed && !scope.row.compatible" size="small" type="danger">不兼容</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="安装状态" width="120">
            <template #default="scope">
              <el-tag v-if="!scope.row.installed" size="small" type="info">未安装</el-tag>
              <el-tag v-else :type="statusTagType(scope.row)" size="small">{{ statusText(scope.row) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="360">
            <template #default="scope">
              <el-button
                v-if="!scope.row.installed"
                size="small"
                type="primary"
                :disabled="!scope.row.compatible || !scope.row.latest"
                :loading="state.pluginAction===scope.row.id"
                @click="installCatalogPlugin(scope.row, false)">安装</el-button>
              <template v-else>
                <el-button
                  v-if="scope.row.update_available"
                  size="small"
                  type="primary"
                  :loading="state.pluginAction===scope.row.id"
                  @click="installCatalogPlugin(scope.row, true)">更新</el-button>
                <el-button v-if="!scope.row.running" size="small" @click="startPlugin(scope.row.id)">启动</el-button>
                <el-button v-else size="small" type="danger" plain @click="stopPlugin(scope.row.id)">停止</el-button>
                <el-button size="small" @click="openDetailById(scope.row.id)">详情</el-button>
                <el-button v-if="!scope.row.bundled" size="small" type="danger" plain @click="uninstallPlugin(scope.row)">删除</el-button>
              </template>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </template>

    <!-- 账号管理 -->
    <template v-if="state.page==='accounts' && state.globalConfig">
      <div class="page-header">
        <h1>账号管理</h1>
        <p>统一保存北师大身份账号；每个插件可以独立选择使用哪个账号。</p>
      </div>
      <el-alert
        v-if="state.specialtyCatalogError"
        :title="'专业目录加载失败：' + state.specialtyCatalogError"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom:12px" />
      <div class="account-list">
        <div
          class="panel account-card"
          v-for="(account, index) in (state.globalConfig.accounts || [])"
          :key="account.id">
          <div class="account-card-head">
            <strong>{{ accountLabel(account) }}</strong>
            <el-button type="danger" plain size="small" @click="removeAccount(account)">
              删除
            </el-button>
          </div>
          <el-form label-width="110px" style="max-width:620px">
            <el-form-item label="学号">
              <el-input v-model="account.username" placeholder="统一身份认证学号" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="account.password" type="password" show-password
                placeholder="统一身份认证密码（已加密存储）" />
            </el-form-item>
            <el-form-item label="校园网密码">
              <el-input v-model="account.campus_password" type="password" show-password
                placeholder="可留空（已加密存储）" />
            </el-form-item>
            <el-form-item label="昵称">
              <el-input v-model="account.nickname" placeholder="留空时显示学号" />
            </el-form-item>
            <el-form-item label="培养层次">
              <el-select
                v-model="account.education_level"
                filterable
                clearable
                style="width:100%"
                placeholder="请选择"
                @change="updateAccountSpecialty(account, 'education_level')">
                <el-option value="" label="请选择" />
                <el-option
                  v-for="item in accountLevelOptions()"
                  :key="item.v"
                  :value="item.v"
                  :label="item.t" />
              </el-select>
            </el-form-item>
            <el-form-item label="年级">
              <el-select
                v-model="account.grade"
                filterable
                clearable
                style="width:100%"
                placeholder="请选择"
                :disabled="!account.education_level"
                @change="updateAccountSpecialty(account, 'grade')">
                <el-option value="" label="请选择" />
                <el-option
                  v-for="item in accountGradeOptions(account)"
                  :key="item.v"
                  :value="item.v"
                  :label="item.t" />
              </el-select>
            </el-form-item>
            <el-form-item label="院(系)/部">
              <el-select
                v-model="account.department"
                filterable
                clearable
                style="width:100%"
                placeholder="请选择"
                :disabled="!account.grade"
                @change="updateAccountSpecialty(account, 'department')">
                <el-option value="" label="请选择" />
                <el-option
                  v-for="item in accountDepartmentOptions(account)"
                  :key="item.v"
                  :value="item.v"
                  :label="item.t" />
              </el-select>
            </el-form-item>
            <el-form-item label="专业">
              <el-select
                v-model="account.major"
                filterable
                clearable
                style="width:100%"
                placeholder="请选择"
                :disabled="!account.department"
                @change="updateAccountSpecialty(account, 'major')">
                <el-option value="" label="请选择" />
                <el-option
                  v-for="item in accountMajorOptions(account)"
                  :key="item.v"
                  :value="item.v"
                  :label="item.t" />
              </el-select>
            </el-form-item>
          </el-form>
        </div>
      </div>
      <div v-if="!(state.globalConfig.accounts || []).length" class="panel empty-state">
        尚未添加账号。添加后即可为各插件选择使用。
      </div>
      <div class="account-actions">
        <el-button @click="addAccount">
          <el-icon style="margin-right:4px"><plus /></el-icon>添加账号
        </el-button>
        <el-button type="primary" :loading="state.savingGlobal" @click="saveGlobal">
          保存账号
        </el-button>
      </div>
    </template>

    <!-- 全局设置 -->
    <template v-if="state.page==='settings' && state.globalConfig">
      <div class="page-header">
        <h1>全局设置</h1>
        <p>配置默认参数、更新策略和日志位置。账号请前往“账号管理”。</p>
      </div>
      <div class="panel">
        <h2><el-icon><tools /></el-icon> 通用参数</h2>
        <el-form label-width="160px" style="max-width:620px">
          <el-form-item label="运行平台">
            <span>Windows 11 · Microsoft Edge</span>
          </el-form-item>
          <el-form-item label="默认无头模式">
            <el-switch v-model="state.globalConfig.general.headless_default" />
          </el-form-item>
          <el-form-item label="系统建议并发">
            <span v-if="state.resources">
              {{ state.resources.recommended_workers }} 个浏览器
              <span class="form-hint">
                （{{ state.resources.physical_cores }} 物理核心 /
                {{ state.resources.total_memory_gb }} GB 内存）
              </span>
            </span>
            <span v-else class="form-hint">正在检测硬件资源</span>
            <div class="form-hint" v-if="state.resources">
              {{ state.resources.formula }}。可在 小鸦刷课详情页按需修改。
            </div>
          </el-form-item>
          <el-form-item label="默认播放倍速">
            <el-input-number v-model="state.globalConfig.general.default_speedup" :min="1" :max="16" :step="0.5" />
          </el-form-item>
          <el-form-item label="运行日志">
            <el-button size="small" @click="openLogsFolder">
              <el-icon style="margin-right:4px"><folder-opened /></el-icon>打开日志文件夹
            </el-button>
            <div class="form-hint">
              日志超过 1 MB 时自动清理最旧文件；运行日志不会占用安装目录。
            </div>
          </el-form-item>
        </el-form>
      </div>
      <div class="panel">
        <h2><el-icon><refresh /></el-icon> 更新设置</h2>
        <el-form label-width="170px" class="update-settings-form">
          <el-form-item label="主程序版本">
            <el-button size="small" :loading="state.pluginAction==='app-update-check'"
              @click="checkAppUpdate">检查更新</el-button>
            <span class="update-current">
              当前 v{{ (state.appUpdate && state.appUpdate.current_version) || "—" }}
            </span>
            <el-button
              v-if="state.appUpdate && state.appUpdate.available"
              size="small"
              type="primary"
              @click="state.appUpdatePrompt=true">
              下载 v{{ state.appUpdate.latest_version }}
            </el-button>
          </el-form-item>
          <el-form-item label="EdgeDriver 更新策略">
            <el-select v-model="state.globalConfig.general.edge_driver_update_mode" style="width:260px">
              <el-option value="exact" label="版本不匹配时更新" />
              <el-option value="compatible" label="版本不兼容时更新" />
              <el-option value="prompt" label="不兼容时提示更新" />
            </el-select>
            <el-button size="small" :loading="state.edgeUpdating"
              @click="checkEdgeDriver">检查更新</el-button>
            <span class="update-current">
              Edge {{ (state.edgeStatus && state.edgeStatus.edge_version) || "未检测到" }}
              · Driver {{ (state.edgeStatus && state.edgeStatus.driver_version) || "未下载" }}
            </span>
          </el-form-item>
        </el-form>
        <div class="form-hint update-message">
          {{ (state.appUpdate && state.appUpdate.message) || "尚未检查主程序更新" }}
        </div>
      </div>
      <el-button type="primary" size="large" :loading="state.savingGlobal" @click="saveGlobal">保存全部设置</el-button>
    </template>

    <!-- 插件详情 -->
    <template v-if="state.page==='detail' && detailPlugin">
      <div class="page-header detail-header">
        <el-button circle aria-label="返回仪表盘" @click="state.page='dashboard'">
          <el-icon><back /></el-icon>
        </el-button>
        <div>
          <h1>{{ detailPlugin.name }} <el-tag size="small" effect="plain">v{{ detailPlugin.version }}</el-tag></h1>
          <p>{{ detailPlugin.description }}</p>
        </div>
        <div class="detail-actions">
          <el-select
            :model-value="selectedAccountId(detailPlugin.id)"
            style="width:180px"
            placeholder="选择账号"
            :disabled="detailPlugin.running"
            @change="(id) => changePluginAccount(detailPlugin.id, id)">
            <el-option
              v-for="account in (state.globalConfig && state.globalConfig.accounts) || []"
              :key="account.id"
              :value="account.id"
              :label="accountLabel(account)" />
            <el-option value="__add__" label="＋ 添加账户" />
          </el-select>
          <el-button v-if="!detailPlugin.running" type="primary" @click="startPlugin(detailPlugin.id)">
            <el-icon style="margin-right:4px"><video-play /></el-icon>启动
          </el-button>
          <el-button v-else type="danger" plain @click="stopPlugin(detailPlugin.id)">
            <el-icon style="margin-right:4px"><video-pause /></el-icon>停止
          </el-button>
        </div>
      </div>

      <div
        class="detail-tabs"
        v-if="pluginHasRunTab(detailPlugin.id)"
        role="tablist"
        aria-label="插件页面">
        <button
          type="button"
          role="tab"
          :aria-selected="state.detailTab==='config'"
          :class="{active: state.detailTab==='config'}"
          @click="state.detailTab='config'">配置</button>
        <button
          type="button"
          role="tab"
          :aria-selected="state.detailTab==='run'"
          :class="{active: state.detailTab==='run'}"
          @click="state.detailTab='run'">运行</button>
      </div>

      <div
        v-if="pluginHasRunTab(detailPlugin.id)"
        v-show="state.detailTab==='run'"
        role="tabpanel">
        <div
          class="run-overview"
          v-if="progressInfo(detailPlugin.id)">
          <div
            class="sticky-run-summary"
            :class="{'has-eta': runMetrics(detailPlugin.id).showEta}">
            <div class="run-summary-copy">
              <div class="run-summary-label">总进度</div>
              <strong>{{ Math.round(runMetrics(detailPlugin.id).percent) }}%</strong>
              <span class="run-summary-status" aria-live="polite">
                {{ state.lastStatus[detailPlugin.id] || statusText(detailPlugin) }}
              </span>
            </div>
            <div class="run-eta" v-if="runMetrics(detailPlugin.id).showEta">
              <span>剩余时间</span>
              <strong v-if="statusDone(detailPlugin.id)">已完成</strong>
              <strong v-else-if="runMetrics(detailPlugin.id).etaS !== null">
                {{ fmtEta(runMetrics(detailPlugin.id).etaS) }}
              </strong>
              <strong v-else>估算中</strong>
            </div>
            <el-progress
              class="run-total-progress"
              :percentage="runMetrics(detailPlugin.id).percent"
              :stroke-width="12"
              :show-text="false"
              :status="statusDone(detailPlugin.id) ? 'success' : undefined" />
            <div class="run-time-line">
              <span v-if="!statusDone(detailPlugin.id) && runMetrics(detailPlugin.id).watchedS !== null">
                服务器已记录 {{ fmtDur(runMetrics(detailPlugin.id).watchedS) }}
                / {{ fmtDur(runMetrics(detailPlugin.id).totalS) }}
              </span>
              <span v-if="runMetrics(detailPlugin.id).qaTotal">
                答题互动 {{ runMetrics(detailPlugin.id).qaDone }}
                / {{ runMetrics(detailPlugin.id).qaTotal }}
              </span>
            </div>
          </div>

          <div class="run-section-heading">
            <h2>任务流程</h2>
            <span class="form-hint">完成后自动更新</span>
          </div>
          <ol class="run-flow" :class="{ 'run-flow-many': runMainSteps(detailPlugin.id).length > 5 }" aria-label="任务步骤">
            <li
              v-for="(step, index) in runMainSteps(detailPlugin.id)"
              :key="step.key"
              class="run-step"
              :class="'is-' + step.status">
              <span class="run-step-marker" aria-hidden="true">
                <el-icon v-if="step.status==='done'"><check /></el-icon>
                <el-icon v-else-if="step.status==='active'" class="is-loading"><loading /></el-icon>
                <el-icon v-else-if="step.status==='error'"><close /></el-icon>
                <span v-else>{{ index + 1 }}</span>
              </span>
              <span class="run-step-copy">
                <strong>{{ step.label }}</strong>
                <small>
                  {{ step.status==='done' ? '已完成'
                    : step.status==='active' ? '进行中'
                    : step.status==='error' ? '异常' : '待完成' }}
                </small>
              </span>
              <ol
                class="run-branches"
                v-if="runChildSteps(detailPlugin.id, step.key).length"
                :aria-label="step.label + '的分支步骤'">
                <li
                  v-for="child in runChildSteps(detailPlugin.id, step.key)"
                  :key="child.key"
                  class="run-branch"
                  :class="'is-' + child.status">
                  <span class="run-branch-marker" aria-hidden="true">
                    <el-icon v-if="child.status==='done'"><check /></el-icon>
                    <el-icon v-else-if="child.status==='active'" class="is-loading"><loading /></el-icon>
                    <el-icon v-else-if="child.status==='error'"><close /></el-icon>
                    <span v-else>·</span>
                  </span>
                  <span>{{ child.label }}</span>
                  <small>
                    {{ child.status==='done' ? '已完成'
                      : child.status==='active' ? '进行中'
                      : child.status==='error' ? '异常' : '待完成' }}
                  </small>
                </li>
              </ol>
            </li>
          </ol>

          <div class="inline-progress-row" v-if="runMetrics(detailPlugin.id).qaTotal">
            <div class="inline-progress-label">
              <strong>答题与互动</strong>
              <span>{{ runMetrics(detailPlugin.id).qaTitle || "处理中" }}</span>
            </div>
            <el-progress
              :percentage="Math.min(100,
                runMetrics(detailPlugin.id).qaDone /
                runMetrics(detailPlugin.id).qaTotal * 100)"
              :stroke-width="9" />
          </div>

          <template v-if="workerList(detailPlugin.id).length">
            <div class="run-section-heading worker-heading">
              <h2>并行进程</h2>
              <span class="form-hint">
                {{ workerList(detailPlugin.id).length }} 个浏览器进程
              </span>
            </div>
            <div class="worker-panels">
              <div class="worker-panel" v-for="w in workerList(detailPlugin.id)" :key="w.id">
                <div class="wp-head">
                  <el-tag size="small" :type="workerTagType(w.status)">进程 #{{ w.id }}</el-tag>
                  <span class="wp-status">{{ w.status }}</span>
                  <span class="wp-title" :title="(w.course ? w.course + ' · ' : '') + w.title">
                    {{ w.course ? w.course + " · " : "" }}{{ w.title || "—" }}
                  </span>
                </div>
                <el-progress
                  v-if="w.percent !== null"
                  :percentage="Math.min(100, Math.round(w.percent * 10) / 10)"
                  :stroke-width="9"
                  :status="w.status === '已完成' ? 'success' : undefined" />
                <div class="wp-meta">
                  <span v-if="w.percent !== null">服务器进度 {{ w.percent }}%</span>
                  <span v-if="w.watchedS !== null">已记录 {{ fmtDur(w.watchedS) }}</span>
                  <span v-if="w.totalS !== null">视频时长 {{ fmtDur(w.totalS) }}</span>
                  <span v-if="w.etaS !== null">剩余 {{ fmtEta(w.etaS) }}</span>
                  <span v-if="w.etaS !== null">预计 {{ fmtEtaClock(w.etaS) }} 完成</span>
                  <span v-if="w.replays">重播 {{ w.replays }} 次</span>
                </div>
              </div>
            </div>
          </template>

          <div
            class="review-link"
            v-if="(state.lastStatus[detailPlugin.id] || '').includes('核验')">
            <span>当前有模型生成内容等待人工核验。</span>
            <el-button type="warning" @click="state.detailTab='config'">前往核验</el-button>
          </div>
        </div>
        <div class="panel empty-state" v-else>
          <p v-if="detailPlugin.running">
            <el-icon class="is-loading"><loading /></el-icon>
            正在初始化任务流程…
          </p>
          <p v-else>暂无运行数据。点击右上角“启动”后，这里会显示流程与实时进度。</p>
        </div>
      </div>

      <div
        v-show="!pluginHasRunTab(detailPlugin.id) || state.detailTab==='config'"
        role="tabpanel">
        <div class="panel">
          <h2><el-icon><setting /></el-icon> 插件配置</h2>
          <!-- 自定义配置界面(插件 config_ui 提供; 不含 CSS, 风格由宿主 crowkit-* 类统一) -->
          <div v-if="state.uiHtml" id="plugin-ui"></div>
          <template v-else-if="state.detailConfig">
            <el-form
              label-width="190px"
              style="max-width:720px"
              @focusout="saveDetailConfig(true)"
              @change="saveDetailConfig(true)">
              <template v-for="f in state.detailSchema" :key="f.key">
                <el-form-item v-if="f.type==='specialty'" :label="f.label">
                  <span class="form-hint">{{ f.hint || "请在插件自定义配置界面中选择" }}</span>
                </el-form-item>
                <el-form-item v-else-if="f.type==='course-table'" :label="f.label">
                  <div style="width:100%">
                    <div class="form-hint" style="margin-bottom:8px" v-if="f.hint">{{ f.hint }}</div>
                    <div class="course-row" v-for="(row, idx) in (state.detailConfig.__rows__ || [])" :key="idx">
                      <el-input class="course-code" v-model="row.code" placeholder="课程代码" />
                      <el-input class="course-classes" v-model="row.classes" placeholder="候选班级号，以逗号分隔（如 09, 10, 11）" />
                      <el-button type="danger" plain circle size="small" aria-label="删除课程" @click="removeCourseRow(idx)">
                        <el-icon><delete /></el-icon>
                      </el-button>
                    </div>
                    <el-button size="small" @click="addCourseRow"><el-icon><plus /></el-icon>添加课程</el-button>
                  </div>
                </el-form-item>
                <el-form-item v-else-if="f.type==='string-list'" :label="f.label">
                  <div style="width:100%">
                    <el-input
                      v-model="state.detailConfig.__text__[f.key]"
                      type="textarea" :rows="8"
                      placeholder="每行填写一个链接…" />
                    <div class="form-hint" style="margin-top:4px" v-if="f.hint">{{ f.hint }}</div>
                  </div>
                </el-form-item>
                <el-form-item v-else-if="f.type==='choice'" :label="f.label">
                  <div style="width:100%">
                    <el-select v-model="state.detailConfig[f.key]" style="width:100%">
                      <el-option
                        v-for="(c, i) in (f.choices || [])" :key="c"
                        :value="c"
                        :label="(f.choice_labels && f.choice_labels[i]) || c" />
                    </el-select>
                    <div class="form-hint" style="margin-top:4px" v-if="f.hint">{{ f.hint }}</div>
                  </div>
                </el-form-item>
                <el-form-item v-else-if="f.type==='number'" :label="f.label">
                  <div style="width:100%">
                    <el-input-number
                      v-model="state.detailConfig[f.key]"
                      :min="f.min !== undefined ? f.min : -Infinity"
                      :max="f.max !== undefined ? f.max : Infinity"
                      :step="f.step || 1" />
                    <div class="form-hint" style="margin-top:4px" v-if="f.hint">{{ f.hint }}</div>
                  </div>
                </el-form-item>
                <el-form-item v-else-if="f.type==='bool'" :label="f.label">
                  <div style="width:100%">
                    <el-switch v-model="state.detailConfig[f.key]" />
                    <div class="form-hint" style="margin-top:4px" v-if="f.hint">{{ f.hint }}</div>
                  </div>
                </el-form-item>
                <el-form-item v-else :label="f.label">
                  <div style="width:100%">
                    <el-input
                      v-model="state.detailConfig[f.key]"
                      :type="f.type==='password' ? 'password' : 'text'"
                      :show-password="f.type==='password'"
                      :placeholder="f.type==='password' ? '已加密存储' : ''" />
                    <div class="form-hint" style="margin-top:4px" v-if="f.hint">{{ f.hint }}</div>
                  </div>
                </el-form-item>
              </template>
            </el-form>
          </template>
          <div v-else class="empty-state"><el-icon class="is-loading"><loading /></el-icon> 配置加载中…</div>
        </div>
      </div>
    </template>

  </main>
</div>
`,
    methods: {
      openDetailById(id) { openDetail(id); },
    },
  });

  app.use(El, { locale: ElementPlusLocaleZhCn });
  for (const [name, comp] of Object.entries(Icons)) {
    app.component(name, comp);
  }
  app.mount("#app");
})();
